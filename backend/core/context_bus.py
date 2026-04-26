"""
==============================================================================
backend/core/context_bus.py
------------------------------------------------------------------------------
共享上下文总线 + 影子上下文 + 短记忆滑动窗口

设计原则:
    1. 不可变 Turn 记录 (frozen dataclass) → 杜绝下游误改污染
    2. 影子上下文 (Shadow Context):
       打断时严禁调 LLM 总结残文 → 纯规则追加截断后缀
       这是防"二次抢算力死锁"的工程铁律
    3. 短记忆滑动窗口: 保留最近 N 轮,超出由 Summarizer 压缩归档
    4. 订阅/广播: 新发言事件异步广播,订阅器异常隔离不影响主路径
    5. 议题栈: ISTJ 拉回议题时弹出当前层而非清空,保留上层议题
    6. 关键词索引: 每 Agent 注册敏感词,O(K) 命中检测,零 LLM 调用

⚠️ 工程铁律:
    - 严禁打断时调用 LLM 总结 (会引发二次抢话死锁)
    - 严禁修改已封存 Turn (frozen 阻止)
    - 严禁订阅器阻塞主路径 (gather + return_exceptions)
    - 议题栈最少保留根议题, pop 到空抛错

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Awaitable,
    Callable,
    Final,
    Iterable,
    Optional,
)

from loguru import logger

from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 影子上下文模板 (纯规则,严禁 LLM 参与)
# ──────────────────────────────────────────────────────────────────────────────

#: 被打断时追加的截断后缀 · 工程铁律: 纯规则,绝不调 LLM
SHADOW_SUFFIX_TEMPLATE: Final[str] = "[系统强制截断:被 {interrupter} 夺取麦克风]"

#: 看门狗熔断时追加的后缀
SHADOW_SUFFIX_TIMEOUT: Final[str] = "[系统强制截断:看门狗熔断,推理超时]"

#: 系统紧急中止时的后缀
SHADOW_SUFFIX_ABORTED: Final[str] = "[系统强制截断:会话紧急中止]"

#: 影子上下文的所有合法后缀枚举值集合 · 用于下游过滤判定
_KNOWN_SHADOW_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        SHADOW_SUFFIX_TEMPLATE,
        SHADOW_SUFFIX_TIMEOUT,
        SHADOW_SUFFIX_ABORTED,
    }
)


# ──────────────────────────────────────────────────────────────────────────────
# 枚举: 截断原因
# ──────────────────────────────────────────────────────────────────────────────


class TruncationReason(str, enum.Enum):
    """
    Turn 被截断的原因 · 决定影子后缀的渲染方式。
    """

    NORMAL = "NORMAL"
    """正常说完,无截断。"""

    INTERRUPTED = "INTERRUPTED"
    """被其他 Agent 抢话打断 (仲裁器下发中断令牌)。"""

    WATCHDOG_TIMEOUT = "WATCHDOG_TIMEOUT"
    """看门狗熔断 (超出 LLM_REQUEST_TIMEOUT_SEC)。"""

    SYSTEM_ABORTED = "SYSTEM_ABORTED"
    """系统紧急中止 (会话终止 / lifespan shutdown)。"""


# ──────────────────────────────────────────────────────────────────────────────
# 不可变 Turn 记录
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Turn:
    """
    一轮已封存的发言 · 不可变记录。

    ⚠️ frozen=True 保证任何字段赋值都会抛 FrozenInstanceError,
       下游任何模块都无法污染历史。
    """

    turn_id: str
    agent_id: str
    topic_id: str
    """该发言所属的议题层 ID (议题栈最顶端的 ID,封存时确定)。"""

    text: str
    """发言完整文本 · 已含影子后缀 (若被截断)。"""

    truncation: TruncationReason

    started_at_monotonic: float
    sealed_at_monotonic: float
    sealed_at_wall_unix: float

    interrupter_agent_id: Optional[str] = None
    """打断者 Agent ID · 仅 INTERRUPTED 时有意义。"""

    metadata: dict[str, str] = field(default_factory=dict)
    """诊断元数据 (例: prefix_target, token_count) · 仅字符串值,杜绝复杂结构。"""

    def is_truncated(self) -> bool:
        return self.truncation != TruncationReason.NORMAL

    def duration_seconds(self) -> float:
        return self.sealed_at_monotonic - self.started_at_monotonic

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "topic_id": self.topic_id,
            "text": self.text,
            "truncation": self.truncation.value,
            "interrupter_agent_id": self.interrupter_agent_id,
            "started_at_monotonic": round(self.started_at_monotonic, 6),
            "sealed_at_monotonic": round(self.sealed_at_monotonic, 6),
            "sealed_at_wall_unix": round(self.sealed_at_wall_unix, 6),
            "duration_seconds": round(self.duration_seconds(), 4),
            "is_truncated": self.is_truncated(),
            "metadata": dict(self.metadata),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 议题层
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Topic:
    """
    单层议题 · 不可变。

    议题栈支持嵌套: 主议题 → 子议题 → 派生议题。
    ISTJ "议题拉回"等价于弹出栈顶,回到上层议题,而非清空。
    """

    topic_id: str
    title: str
    description: str
    pushed_at_monotonic: float
    pushed_at_wall_unix: float
    parent_topic_id: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "topic_id": self.topic_id,
            "title": self.title,
            "description": self.description,
            "pushed_at_monotonic": round(self.pushed_at_monotonic, 6),
            "pushed_at_wall_unix": round(self.pushed_at_wall_unix, 6),
            "parent_topic_id": self.parent_topic_id,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进行中发言 (OngoingTurn)
# ──────────────────────────────────────────────────────────────────────────────


class OngoingTurn:
    """
    正在流式生成的发言 · 可变状态。

    生命周期:
        1. ContextBus.start_turn() → 生成 OngoingTurn
        2. flow.append_chunk(token) × N
        3. flow.seal(reason) → 冻结为不可变 Turn,广播至订阅器

    ⚠️ 同一个 OngoingTurn 仅能 seal 一次,二次封存抛 RuntimeError。
    """

    __slots__ = (
        "_turn_id",
        "_agent_id",
        "_topic_id",
        "_started_at_monotonic",
        "_buffer",
        "_token_count",
        "_sealed",
        "_metadata",
    )

    def __init__(
        self,
        turn_id: str,
        agent_id: str,
        topic_id: str,
    ) -> None:
        self._turn_id: Final[str] = turn_id
        self._agent_id: Final[str] = agent_id
        self._topic_id: Final[str] = topic_id
        self._started_at_monotonic: Final[float] = time.monotonic()
        self._buffer: list[str] = []
        self._token_count: int = 0
        self._sealed: bool = False
        self._metadata: dict[str, str] = {}

    @property
    def turn_id(self) -> str:
        return self._turn_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def topic_id(self) -> str:
        return self._topic_id

    @property
    def started_at_monotonic(self) -> float:
        return self._started_at_monotonic

    @property
    def is_sealed(self) -> bool:
        return self._sealed

    @property
    def token_count(self) -> int:
        return self._token_count

    @property
    def current_text(self) -> str:
        """当前累积文本 (无影子后缀)。"""
        return "".join(self._buffer)

    def append_chunk(self, chunk: str) -> None:
        """追加流式 token chunk · 已封存后调用抛错。"""
        if self._sealed:
            raise RuntimeError(
                f"OngoingTurn {self._turn_id!r} 已封存,严禁追加 chunk"
            )
        if not isinstance(chunk, str):
            raise TypeError(
                f"chunk 必须为 str,当前类型: {type(chunk).__name__}"
            )
        if not chunk:
            return  # 空 chunk 静默忽略
        self._buffer.append(chunk)
        self._token_count += 1

    def set_metadata(self, key: str, value: str) -> None:
        """设置诊断元数据 (仅字符串值)。"""
        if self._sealed:
            raise RuntimeError(
                f"OngoingTurn {self._turn_id!r} 已封存,严禁修改 metadata"
            )
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError("metadata key/value 必须为 str")
        self._metadata[key] = value

    def seal_normal(self) -> Turn:
        """正常封存 (无影子后缀)。"""
        return self._seal_internal(
            reason=TruncationReason.NORMAL,
            interrupter_agent_id=None,
        )

    def seal_with_shadow_suffix(
        self,
        reason: TruncationReason,
        interrupter_agent_id: Optional[str] = None,
    ) -> Turn:
        """
        异常封存 · 追加影子后缀 (纯规则,严禁调 LLM)。

        Args:
            reason: 截断原因 · 决定后缀模板
            interrupter_agent_id: 仅 INTERRUPTED 时使用

        Raises:
            ValueError: reason=NORMAL (应使用 seal_normal)
            ValueError: reason=INTERRUPTED 但未提供 interrupter
        """
        if reason == TruncationReason.NORMAL:
            raise ValueError(
                "seal_with_shadow_suffix 不允许 NORMAL · 请使用 seal_normal()"
            )
        if reason == TruncationReason.INTERRUPTED and not interrupter_agent_id:
            raise ValueError(
                "INTERRUPTED 必须提供 interrupter_agent_id"
            )
        return self._seal_internal(
            reason=reason,
            interrupter_agent_id=interrupter_agent_id,
        )

    def _seal_internal(
        self,
        reason: TruncationReason,
        interrupter_agent_id: Optional[str],
    ) -> Turn:
        if self._sealed:
            raise RuntimeError(
                f"OngoingTurn {self._turn_id!r} 已封存,严禁重复 seal"
            )

        text = "".join(self._buffer)

        # 追加影子后缀 (纯规则)
        if reason == TruncationReason.INTERRUPTED:
            assert interrupter_agent_id is not None  # 上面已校验
            suffix = SHADOW_SUFFIX_TEMPLATE.format(
                interrupter=interrupter_agent_id
            )
            text = self._concat_with_suffix(text, suffix)
        elif reason == TruncationReason.WATCHDOG_TIMEOUT:
            text = self._concat_with_suffix(text, SHADOW_SUFFIX_TIMEOUT)
        elif reason == TruncationReason.SYSTEM_ABORTED:
            text = self._concat_with_suffix(text, SHADOW_SUFFIX_ABORTED)
        # NORMAL 路径已在上层拦截,不会进入

        now_mono = time.monotonic()
        now_wall = time.time()

        sealed_metadata = dict(self._metadata)
        sealed_metadata.setdefault("token_count", str(self._token_count))

        sealed = Turn(
            turn_id=self._turn_id,
            agent_id=self._agent_id,
            topic_id=self._topic_id,
            text=text,
            truncation=reason,
            started_at_monotonic=self._started_at_monotonic,
            sealed_at_monotonic=now_mono,
            sealed_at_wall_unix=now_wall,
            interrupter_agent_id=interrupter_agent_id,
            metadata=sealed_metadata,
        )
        self._sealed = True
        return sealed

    @staticmethod
    def _concat_with_suffix(text: str, suffix: str) -> str:
        """智能拼接影子后缀: 处理空文本与尾部空白。"""
        text = text.rstrip()
        if not text:
            return suffix
        # 若文本不以标点结尾,补一个空格分隔
        if text[-1] not in "。!?.!?,, ":
            return f"{text} {suffix}"
        return f"{text}{suffix}"


# ──────────────────────────────────────────────────────────────────────────────
# 事件 / 订阅
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TurnSealedEvent:
    """新 Turn 封存事件 · 广播给订阅器 (Aggro 引擎 / Summarizer / WS 推送)。"""

    turn: Turn
    keyword_hits: dict[str, list[str]]
    """命中的关键词: {命中的 Agent ID: [关键词列表]}"""

    named_targets: tuple[str, ...]
    """该发言中被点名的目标 Agent ID (前缀拦截后从 metadata 读出)。"""

    def to_dict(self) -> dict[str, object]:
        return {
            "turn": self.turn.to_dict(),
            "keyword_hits": {k: list(v) for k, v in self.keyword_hits.items()},
            "named_targets": list(self.named_targets),
        }


@dataclass(frozen=True, slots=True)
class TopicChangedEvent:
    """议题栈变化事件 (push / pop)。"""

    operation: str
    """"push" | "pop" """
    topic: Topic
    stack_depth: int


SealedSubscriber = Callable[[TurnSealedEvent], Awaitable[None]]
TopicSubscriber = Callable[[TopicChangedEvent], Awaitable[None]]


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class ContextBusError(Exception):
    pass


class EmptyTopicStackError(ContextBusError):
    """尝试在空议题栈上 pop。"""


class NoTopicError(ContextBusError):
    """尚未推入任何议题就开始发言。"""


class OngoingTurnConflictError(ContextBusError):
    """同一 Agent 已存在未封存的发言。"""


# ──────────────────────────────────────────────────────────────────────────────
# 主体: ContextBus
# ──────────────────────────────────────────────────────────────────────────────


class ContextBus:
    """
    共享上下文总线。

    核心职责:
        - 议题栈管理 (push / pop / current)
        - 短记忆滑动窗口 (最近 N 轮 Turn)
        - OngoingTurn 生命周期管理
        - 关键词索引 + Agent 别名注册
        - TurnSealed / TopicChanged 事件广播
        - 溢出 Turn 弹出 (供 Summarizer 异步消费)
    """

    def __init__(self, short_memory_window_turns: int) -> None:
        if short_memory_window_turns < 2:
            raise ValueError(
                f"short_memory_window_turns 必须 ≥ 2,当前: {short_memory_window_turns}"
            )

        self._window_size: Final[int] = short_memory_window_turns

        # 短记忆环 (按封存顺序)
        self._short_memory: deque[Turn] = deque(maxlen=short_memory_window_turns)

        # 溢出待归档队列 (Summarizer 异步消费)
        self._overflow_queue: deque[Turn] = deque()

        # 议题栈
        self._topic_stack: list[Topic] = []

        # 进行中 Turn: agent_id -> OngoingTurn
        self._ongoing: dict[str, OngoingTurn] = {}

        # 关键词索引: agent_id -> set[关键词 lowercase]
        self._keyword_index: dict[str, set[str]] = {}

        # Agent 别名 (用于 named_targets 检测): agent_id -> set[别名 lowercase]
        # 至少包含 agent_id 自身
        self._agent_aliases: dict[str, set[str]] = {}

        # 订阅器
        self._sealed_subscribers: list[SealedSubscriber] = []
        self._topic_subscribers: list[TopicSubscriber] = []

        # 协程锁 (议题栈 / ongoing 字典)
        self._lock: asyncio.Lock = asyncio.Lock()

        logger.info(
            f"[ContextBus] 初始化 · short_memory_window={short_memory_window_turns}"
        )

    # ────────────────────────────────────────────────────────────
    # Agent 注册 (别名 + 关键词)
    # ────────────────────────────────────────────────────────────

    def register_agent(
        self,
        agent_id: str,
        aliases: Iterable[str] = (),
        keywords: Iterable[str] = (),
    ) -> None:
        """
        注册 Agent 的别名与关键词。

        - 别名用于 named_targets 检测 (前缀 [TARGET: alias])
          自动包含 agent_id 自身,大小写不敏感
        - 关键词用于 KEYWORD_TRIGGER 刺激源,O(K) 命中
        """
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"agent_id 必须为非空字符串,当前: {agent_id!r}")

        # 别名集合: agent_id + 用户提供别名
        alias_set: set[str] = {agent_id.lower()}
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                alias_set.add(alias.strip().lower())
        self._agent_aliases[agent_id] = alias_set

        # 关键词集合
        kw_set: set[str] = set()
        for kw in keywords:
            if isinstance(kw, str) and kw.strip():
                kw_set.add(kw.strip().lower())
        self._keyword_index[agent_id] = kw_set

        logger.info(
            f"[ContextBus] 注册 Agent {agent_id!r} · "
            f"aliases={len(alias_set)} · keywords={len(kw_set)}"
        )

    # ────────────────────────────────────────────────────────────
    # 议题栈
    # ────────────────────────────────────────────────────────────

    async def push_topic(self, title: str, description: str = "") -> Topic:
        """推入新议题层。"""
        if not title or not isinstance(title, str):
            raise ValueError(f"title 必须为非空字符串,当前: {title!r}")

        async with self._lock:
            now_mono = time.monotonic()
            now_wall = time.time()
            parent_id = (
                self._topic_stack[-1].topic_id if self._topic_stack else None
            )
            topic = Topic(
                topic_id=str(uuid.uuid4()),
                title=title,
                description=description,
                pushed_at_monotonic=now_mono,
                pushed_at_wall_unix=now_wall,
                parent_topic_id=parent_id,
            )
            self._topic_stack.append(topic)
            depth = len(self._topic_stack)
            logger.info(
                f"[ContextBus] push_topic · {topic.title!r} · "
                f"depth={depth} · parent={parent_id}"
            )

        await self._broadcast_topic(
            TopicChangedEvent(operation="push", topic=topic, stack_depth=depth)
        )
        return topic

    async def pop_topic(self) -> Topic:
        """弹出栈顶议题。栈为空时抛 EmptyTopicStackError。"""
        async with self._lock:
            if not self._topic_stack:
                raise EmptyTopicStackError("议题栈为空,无法 pop")
            topic = self._topic_stack.pop()
            depth = len(self._topic_stack)
            logger.info(
                f"[ContextBus] pop_topic · {topic.title!r} · "
                f"remaining_depth={depth}"
            )

        await self._broadcast_topic(
            TopicChangedEvent(operation="pop", topic=topic, stack_depth=depth)
        )
        return topic

    def current_topic(self) -> Optional[Topic]:
        """获取栈顶议题 (无锁,只读)。"""
        return self._topic_stack[-1] if self._topic_stack else None

    def topic_stack_snapshot(self) -> tuple[Topic, ...]:
        """议题栈只读快照 (从底到顶)。"""
        return tuple(self._topic_stack)

    def topic_stack_depth(self) -> int:
        return len(self._topic_stack)

    # ────────────────────────────────────────────────────────────
    # OngoingTurn 生命周期
    # ────────────────────────────────────────────────────────────

    async def start_turn(self, agent_id: str) -> OngoingTurn:
        """
        开启新 Turn · 一个 Agent 同时只能有一个未封存 Turn。

        Raises:
            NoTopicError: 议题栈为空
            OngoingTurnConflictError: 该 Agent 已有未封存 Turn
        """
        async with self._lock:
            current = self.current_topic()
            if current is None:
                raise NoTopicError(
                    "议题栈为空,严禁开启 Turn (必先 push_topic)"
                )

            if agent_id in self._ongoing:
                existing = self._ongoing[agent_id]
                raise OngoingTurnConflictError(
                    f"Agent {agent_id!r} 已存在未封存 Turn "
                    f"(turn_id={existing.turn_id!r}) · 必先 seal 之"
                )

            ongoing = OngoingTurn(
                turn_id=str(uuid.uuid4()),
                agent_id=agent_id,
                topic_id=current.topic_id,
            )
            self._ongoing[agent_id] = ongoing
            logger.debug(
                f"[ContextBus] start_turn · agent={agent_id!r} · "
                f"turn_id={ongoing.turn_id!r} · topic={current.title!r}"
            )
            return ongoing

    def get_ongoing(self, agent_id: str) -> Optional[OngoingTurn]:
        """获取该 Agent 当前未封存 Turn (无则 None)。"""
        return self._ongoing.get(agent_id)

    async def seal_turn(
        self,
        agent_id: str,
        truncation: TruncationReason = TruncationReason.NORMAL,
        interrupter_agent_id: Optional[str] = None,
    ) -> Turn:
        """
        封存 Agent 的进行中 Turn。

        - NORMAL: 调用 seal_normal()
        - 其他: 调用 seal_with_shadow_suffix(reason, interrupter)
        """
        async with self._lock:
            ongoing = self._ongoing.pop(agent_id, None)
            if ongoing is None:
                raise OngoingTurnConflictError(
                    f"Agent {agent_id!r} 无进行中 Turn,无法 seal"
                )

            if truncation == TruncationReason.NORMAL:
                turn = ongoing.seal_normal()
            else:
                turn = ongoing.seal_with_shadow_suffix(
                    reason=truncation,
                    interrupter_agent_id=interrupter_agent_id,
                )

            # 入短记忆窗口 + 溢出处理
            evicted = self._push_to_short_memory(turn)
            if evicted is not None:
                self._overflow_queue.append(evicted)

        # 在锁外计算 keyword_hits / named_targets,降低锁占用
        keyword_hits = self._scan_keywords(turn.text, exclude_agent=agent_id)
        named_targets = self._scan_named_targets(turn)

        event = TurnSealedEvent(
            turn=turn,
            keyword_hits=keyword_hits,
            named_targets=named_targets,
        )

        logger.info(
            f"[ContextBus] seal_turn · agent={agent_id!r} · "
            f"truncation={truncation.value} · "
            f"text_len={len(turn.text)} · "
            f"keyword_hits={sum(len(v) for v in keyword_hits.values())} · "
            f"named={named_targets}"
        )

        await self._broadcast_sealed(event)
        return turn

    async def emergency_seal_all(
        self,
        truncation: TruncationReason = TruncationReason.SYSTEM_ABORTED,
    ) -> list[Turn]:
        """
        紧急封存所有未封存 Turn (lifespan shutdown 路径)。

        ⚠️ 不会抛出异常 · 即使部分 Turn 封存失败也继续处理其他。
        """
        async with self._lock:
            ongoing_items = list(self._ongoing.items())
            self._ongoing.clear()

        sealed: list[Turn] = []
        for agent_id, ongoing in ongoing_items:
            try:
                if truncation == TruncationReason.NORMAL:
                    turn = ongoing.seal_normal()
                else:
                    turn = ongoing.seal_with_shadow_suffix(
                        reason=truncation,
                        interrupter_agent_id=None,
                    )
            except Exception as exc:
                logger.error(
                    f"[ContextBus] emergency_seal · agent={agent_id!r} · "
                    f"封存失败: {exc!r}"
                )
                continue

            async with self._lock:
                evicted = self._push_to_short_memory(turn)
                if evicted is not None:
                    self._overflow_queue.append(evicted)

            sealed.append(turn)

        logger.warning(
            f"[ContextBus] emergency_seal_all · 共封存 {len(sealed)} 条 · "
            f"reason={truncation.value}"
        )
        return sealed

    # ────────────────────────────────────────────────────────────
    # 短记忆窗口
    # ────────────────────────────────────────────────────────────

    def _push_to_short_memory(self, turn: Turn) -> Optional[Turn]:
        """
        ⚠️ 调用方必须已持锁。

        将 Turn 推入短记忆环。若导致溢出,返回被弹出的最旧 Turn (供归档)。
        """
        evicted: Optional[Turn] = None
        if len(self._short_memory) == self._window_size:
            evicted = self._short_memory[0]
        self._short_memory.append(turn)
        return evicted

    def short_memory_snapshot(
        self,
        last_n: Optional[int] = None,
    ) -> list[Turn]:
        """短记忆窗口只读快照 (按时间顺序,旧→新)。"""
        records = list(self._short_memory)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records

    def short_memory_size(self) -> int:
        return len(self._short_memory)

    # ────────────────────────────────────────────────────────────
    # 溢出归档队列 (Summarizer 消费)
    # ────────────────────────────────────────────────────────────

    def has_overflow(self) -> bool:
        return bool(self._overflow_queue)

    def pop_overflow(self, max_items: int = 8) -> list[Turn]:
        """
        Summarizer 在算力空闲时调用,批量取走待归档 Turn。

        Args:
            max_items: 单次最多取出条数 · 防止压垮 Summarizer
        """
        if max_items < 1:
            raise ValueError(f"max_items 必须 ≥ 1,当前: {max_items}")
        out: list[Turn] = []
        while self._overflow_queue and len(out) < max_items:
            out.append(self._overflow_queue.popleft())
        return out

    def overflow_queue_size(self) -> int:
        return len(self._overflow_queue)

    # ────────────────────────────────────────────────────────────
    # 关键词 / 别名扫描
    # ────────────────────────────────────────────────────────────

    def _scan_keywords(
        self,
        text: str,
        exclude_agent: Optional[str] = None,
    ) -> dict[str, list[str]]:
        """
        扫描文本命中哪些 Agent 的关键词。

        排除 exclude_agent (避免发言者命中自己关键词)。

        ⚠️ 简单子串匹配 (case-insensitive),零 LLM 调用。
        """
        if not text:
            return {}
        haystack = text.lower()
        hits: dict[str, list[str]] = {}
        for aid, keywords in self._keyword_index.items():
            if aid == exclude_agent:
                continue
            matched = [kw for kw in keywords if kw in haystack]
            if matched:
                hits[aid] = matched
        return hits

    def _scan_named_targets(self, turn: Turn) -> tuple[str, ...]:
        """
        从 turn.metadata 读取 stream_parser 已解析出的前缀目标。

        若 metadata 中无 'prefix_target' 字段,退化为别名扫描 (兜底)。
        """
        # 主路径: 优先信任 stream_parser 已解析结果
        prefix_target_raw = turn.metadata.get("prefix_target", "").strip()
        if prefix_target_raw:
            # 多目标用逗号分隔
            targets = [t.strip() for t in prefix_target_raw.split(",") if t.strip()]
            valid = [t for t in targets if t in self._agent_aliases]
            return tuple(valid)

        # 兜底: 文本扫描别名命中
        text_low = turn.text.lower()
        named: list[str] = []
        for aid, aliases in self._agent_aliases.items():
            if aid == turn.agent_id:
                continue
            if any(alias in text_low for alias in aliases):
                named.append(aid)
        return tuple(named)

    # ────────────────────────────────────────────────────────────
    # 订阅 / 广播
    # ────────────────────────────────────────────────────────────

    def subscribe_sealed(self, subscriber: SealedSubscriber) -> None:
        """订阅 TurnSealedEvent (例: Aggro 引擎 / WS 推送)。"""
        if not callable(subscriber):
            raise TypeError("sealed subscriber 必须可调用")
        self._sealed_subscribers.append(subscriber)

    def unsubscribe_sealed(self, subscriber: SealedSubscriber) -> bool:
        try:
            self._sealed_subscribers.remove(subscriber)
            return True
        except ValueError:
            return False

    def subscribe_topic(self, subscriber: TopicSubscriber) -> None:
        if not callable(subscriber):
            raise TypeError("topic subscriber 必须可调用")
        self._topic_subscribers.append(subscriber)

    def unsubscribe_topic(self, subscriber: TopicSubscriber) -> bool:
        try:
            self._topic_subscribers.remove(subscriber)
            return True
        except ValueError:
            return False

    async def _broadcast_sealed(self, event: TurnSealedEvent) -> None:
        """异步广播 TurnSealedEvent · 订阅器异常隔离不影响主路径。"""
        if not self._sealed_subscribers:
            return
        results = await asyncio.gather(
            *(sub(event) for sub in self._sealed_subscribers),
            return_exceptions=True,
        )
        for sub, result in zip(self._sealed_subscribers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    f"[ContextBus] sealed subscriber 异常 · "
                    f"sub={sub!r} · err={result!r}"
                )

    async def _broadcast_topic(self, event: TopicChangedEvent) -> None:
        if not self._topic_subscribers:
            return
        results = await asyncio.gather(
            *(sub(event) for sub in self._topic_subscribers),
            return_exceptions=True,
        )
        for sub, result in zip(self._topic_subscribers, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    f"[ContextBus] topic subscriber 异常 · "
                    f"sub={sub!r} · err={result!r}"
                )

    # ────────────────────────────────────────────────────────────
    # 序列化 (供 /api/sandbox/snapshot)
    # ────────────────────────────────────────────────────────────

    def snapshot_dict(self) -> dict[str, object]:
        """完整序列化快照,供 HUD 初始化水合。"""
        return {
            "current_topic": (
                self.current_topic().to_dict() if self.current_topic() else None
            ),
            "topic_stack": [t.to_dict() for t in self._topic_stack],
            "short_memory": [t.to_dict() for t in self._short_memory],
            "short_memory_size": self.short_memory_size(),
            "short_memory_window": self._window_size,
            "overflow_queue_size": self.overflow_queue_size(),
            "ongoing_agents": list(self._ongoing.keys()),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_bus_singleton: Optional[ContextBus] = None


def get_context_bus() -> ContextBus:
    """
    获取全局 ContextBus 单例。

    使用模式:
        from backend.core.context_bus import get_context_bus, TruncationReason
        bus = get_context_bus()
        await bus.push_topic("自由意志是否存在")
        ongoing = await bus.start_turn("INTJ_logician")
        ongoing.append_chunk("从决定论的角度看...")
        turn = await bus.seal_turn("INTJ_logician")
    """
    global _bus_singleton
    if _bus_singleton is None:
        s = get_settings()
        _bus_singleton = ContextBus(
            short_memory_window_turns=s.SHORT_MEMORY_WINDOW_TURNS,
        )
    return _bus_singleton


def reset_bus_for_testing() -> None:
    """[仅供测试] 重置全局 ContextBus 单例。"""
    global _bus_singleton
    _bus_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 影子常量
    "SHADOW_SUFFIX_TEMPLATE",
    "SHADOW_SUFFIX_TIMEOUT",
    "SHADOW_SUFFIX_ABORTED",
    # 枚举
    "TruncationReason",
    # 数据类
    "Turn",
    "Topic",
    "TurnSealedEvent",
    "TopicChangedEvent",
    # 主体
    "OngoingTurn",
    "ContextBus",
    # 订阅器签名
    "SealedSubscriber",
    "TopicSubscriber",
    # 异常
    "ContextBusError",
    "EmptyTopicStackError",
    "NoTopicError",
    "OngoingTurnConflictError",
    # 单例
    "get_context_bus",
    "reset_bus_for_testing",
]