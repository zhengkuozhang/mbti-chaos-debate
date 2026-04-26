"""
==============================================================================
backend/core/fsm.py
------------------------------------------------------------------------------
显式有限状态机 · Agent FSM Registry

算力节约的核心机制:
    Agent 在 [LISTENING] 态严禁调用 LLM!
    被动刺激量改由 aggro_engine.py 的纯规则引擎计算。
    回合结束时批量进入 [UPDATING],原子性切换防止半同步污染。

设计原则:
    1. 状态不可变 (Enum) + 合法转换矩阵 → 非法跃迁直接熔断,
       杜绝运行期出现 "RETRIEVING -> SPEAKING" 这类逻辑漂移。
    2. 每 Agent 独立 FSM 实例,通过 FSMRegistry 单例集中管理。
    3. 进入/离开钩子: on_transition 回调用于触发 WS 状态包推送。
    4. 批量 UPDATING 屏障: batch_transition() 原子切换全部 Agent,
       防止"有的进 UPDATING,有的还 LISTENING"的半同步污染。
    5. 状态停留时长统计 + 历史轨迹环,故障复盘所需。

⚠️ 铁律:
    - 严禁在 IDLE / LISTENING / UPDATING 态调用 LLM
      → assert_can_invoke_llm() 在 llm_router.py 入口处守门
    - 严禁绕过 transition() 手改 _state (字段私有)
    - 严禁跨 Agent 直接读写他人 FSM,只能通过 snapshot_all() 只读视图

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Awaitable,
    Callable,
    Final,
    FrozenSet,
    Iterable,
    Optional,
)

from loguru import logger


# ──────────────────────────────────────────────────────────────────────────────
# 状态枚举
# ──────────────────────────────────────────────────────────────────────────────


class FSMState(str, enum.Enum):
    """
    Agent 显式状态枚举。

    状态语义 (按典型生命周期顺序):
        IDLE        : 初始态/真空态,无任何活动
        LISTENING   : 旁听态,接收他人发言;严禁调 LLM
        RETRIEVING  : 检索态,仅 ISFJ 可进入,异步访问 ChromaDB
        COMPUTING   : 推理态,持有信号量后调用 Ollama 流
        SPEAKING    : 输出态,流式 token 通过 WebSocket 推送
        UPDATING    : 回合结束后的批量 Aggro/记忆更新态

    使用 str 基类的好处:
        - 可直接用作 dict key
        - 序列化为 JSON 时自动转字符串
        - 与前端 TS 的字面量类型字符串对齐
    """

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    RETRIEVING = "RETRIEVING"
    COMPUTING = "COMPUTING"
    SPEAKING = "SPEAKING"
    UPDATING = "UPDATING"


# ──────────────────────────────────────────────────────────────────────────────
# 合法转换矩阵 (单一可信源)
# ──────────────────────────────────────────────────────────────────────────────

#: 状态转换图 (有向图): src_state → frozenset[allowed dst_states]
#: 任何不在此表中的跃迁视为非法,transition() 会抛 IllegalTransitionError
_LEGAL_TRANSITIONS: Final[dict[FSMState, FrozenSet[FSMState]]] = {
    FSMState.IDLE: frozenset(
        {
            FSMState.LISTENING,  # 议题注入,所有 Agent IDLE → LISTENING
            FSMState.UPDATING,   # 系统启动后直接进入批量初始化
        }
    ),
    FSMState.LISTENING: frozenset(
        {
            FSMState.RETRIEVING,  # 仅 ISFJ 走此分支
            FSMState.COMPUTING,   # 获取麦克风后直接推理
            FSMState.UPDATING,    # 回合结束批量切换
            FSMState.IDLE,        # 议题暂停/会话终止
        }
    ),
    FSMState.RETRIEVING: frozenset(
        {
            FSMState.COMPUTING,   # 检索完成进入推理
            FSMState.LISTENING,   # 检索失败,回退旁听
            FSMState.IDLE,        # 紧急中止
        }
    ),
    FSMState.COMPUTING: frozenset(
        {
            FSMState.SPEAKING,    # 拿到首 token 立即转 SPEAKING
            FSMState.LISTENING,   # 推理失败 / 被仲裁拒绝
            FSMState.UPDATING,    # 看门狗熔断后直接进入批量 UPDATING
            FSMState.IDLE,        # 紧急中止
        }
    ),
    FSMState.SPEAKING: frozenset(
        {
            FSMState.LISTENING,   # 正常说完,回归旁听
            FSMState.UPDATING,    # 被仲裁打断 → 影子上下文 → 批量 UPDATING
            FSMState.IDLE,        # 紧急中止
        }
    ),
    FSMState.UPDATING: frozenset(
        {
            FSMState.LISTENING,   # 批量更新完毕,进入下一轮旁听
            FSMState.IDLE,        # 议题结束
        }
    ),
}


#: 哪些状态下"严禁调用 LLM"
#: assert_can_invoke_llm() 会拒绝这些状态下的推理请求
_LLM_FORBIDDEN_STATES: Final[FrozenSet[FSMState]] = frozenset(
    {
        FSMState.IDLE,
        FSMState.LISTENING,
        FSMState.UPDATING,
    }
)


def is_legal_transition(src: FSMState, dst: FSMState) -> bool:
    """
    纯函数判定: src → dst 是否合法。

    此函数不依赖任何 FSM 实例,可在测试 / 文档生成中独立使用。
    """
    return dst in _LEGAL_TRANSITIONS.get(src, frozenset())


def is_llm_forbidden(state: FSMState) -> bool:
    """判定状态下是否禁止调用 LLM。"""
    return state in _LLM_FORBIDDEN_STATES


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class FSMError(Exception):
    """FSM 异常基类。"""


class IllegalTransitionError(FSMError):
    """非法状态跃迁,违反合法转换矩阵。"""

    def __init__(
        self,
        agent_id: str,
        src: FSMState,
        dst: FSMState,
    ) -> None:
        super().__init__(
            f"[FSM] Agent {agent_id!r} 非法跃迁 {src.value} → {dst.value} · "
            f"合法目标: {sorted(s.value for s in _LEGAL_TRANSITIONS.get(src, frozenset()))}"
        )
        self.agent_id = agent_id
        self.src = src
        self.dst = dst


class LLMInvocationForbiddenError(FSMError):
    """在 LLM 禁止状态下尝试发起推理。"""

    def __init__(self, agent_id: str, current_state: FSMState) -> None:
        super().__init__(
            f"[FSM] Agent {agent_id!r} 在 {current_state.value} 态严禁调用 LLM · "
            f"必须先转换至 RETRIEVING / COMPUTING / SPEAKING"
        )
        self.agent_id = agent_id
        self.current_state = current_state


class AgentNotRegisteredError(FSMError):
    """请求的 Agent ID 未在 FSMRegistry 中注册。"""


class AgentAlreadyRegisteredError(FSMError):
    """重复注册同一 Agent ID。"""


# ──────────────────────────────────────────────────────────────────────────────
# 状态转换记录 (历史环)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    """
    单次状态转换的不可变记录。

    供历史轨迹环使用,可用于故障复盘 / 前端时间线渲染。
    """

    agent_id: str
    src: FSMState
    dst: FSMState
    at_monotonic: float
    at_wall_unix: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "src": self.src.value,
            "dst": self.dst.value,
            "at_monotonic": round(self.at_monotonic, 6),
            "at_wall_unix": round(self.at_wall_unix, 6),
            "reason": self.reason,
        }


# ──────────────────────────────────────────────────────────────────────────────
# FSM 快照 (供 HUD 序列化)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class FSMSnapshot:
    """
    单 Agent FSM 的瞬时快照。

    透出至 /api/sandbox/snapshot 与 WS 状态包。
    """

    agent_id: str
    state: FSMState
    entered_at_monotonic: float
    dwell_seconds: float
    transitions_total: int
    illegal_attempts_total: int

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "state": self.state.value,
            "dwell_seconds": round(self.dwell_seconds, 3),
            "transitions_total": self.transitions_total,
            "illegal_attempts_total": self.illegal_attempts_total,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 单 Agent FSM
# ──────────────────────────────────────────────────────────────────────────────


#: 转换钩子签名: 异步回调,接收转换记录,无返回值
TransitionHook = Callable[[TransitionRecord], Awaitable[None]]


class AgentFSM:
    """
    单个 Agent 的有限状态机实例。

    每个 MBTI Agent 持有一个独立 AgentFSM,通过 FSMRegistry 集中管理。
    所有状态变更必须经 transition() 通过合法转换矩阵守门。
    """

    HISTORY_RING_SIZE: Final[int] = 64
    """状态转换历史环的容量,超出后 FIFO 弹出最旧记录"""

    def __init__(
        self,
        agent_id: str,
        initial_state: FSMState = FSMState.IDLE,
    ) -> None:
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"agent_id 必须为非空字符串,当前: {agent_id!r}")

        self._agent_id: Final[str] = agent_id
        self._state: FSMState = initial_state
        self._entered_at_monotonic: float = time.monotonic()
        self._entered_at_wall: float = time.time()

        # 累计指标
        self._transitions_total: int = 0
        self._illegal_attempts_total: int = 0

        # 历史环 (FIFO)
        self._history: deque[TransitionRecord] = deque(
            maxlen=self.HISTORY_RING_SIZE
        )

        # 转换钩子 (异步,可由 throttle 推送 WS 状态包)
        self._on_transition_hooks: list[TransitionHook] = []

        # 协程级独占锁,杜绝同 Agent 并发 transition()
        self._lock: asyncio.Lock = asyncio.Lock()

    # ────────────────────────────────────────────────────────────
    # 公开属性
    # ────────────────────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def state(self) -> FSMState:
        """当前状态 (只读快照)。"""
        return self._state

    @property
    def dwell_seconds(self) -> float:
        """当前状态已停留秒数。"""
        return time.monotonic() - self._entered_at_monotonic

    # ────────────────────────────────────────────────────────────
    # 钩子注册
    # ────────────────────────────────────────────────────────────

    def add_transition_hook(self, hook: TransitionHook) -> None:
        """
        注册转换钩子。每次成功 transition 后异步调用。

        ⚠️ 钩子内部异常会被捕获并记录,不会中断 transition 主流程,
           防止"WS 推送失败导致状态机崩溃"。
        """
        if not callable(hook):
            raise TypeError("transition hook 必须可调用")
        self._on_transition_hooks.append(hook)

    def remove_transition_hook(self, hook: TransitionHook) -> bool:
        """移除已注册钩子,成功返回 True。"""
        try:
            self._on_transition_hooks.remove(hook)
            return True
        except ValueError:
            return False

    # ────────────────────────────────────────────────────────────
    # 核心: 状态跃迁
    # ────────────────────────────────────────────────────────────

    async def transition(
        self,
        dst: FSMState,
        reason: str = "",
    ) -> TransitionRecord:
        """
        将 FSM 从当前状态切换到 dst。

        Args:
            dst: 目标状态 (必须在合法转换矩阵中)
            reason: 转换原因,记入历史环 (例: "got_microphone" / "watchdog_timeout")

        Returns:
            TransitionRecord: 转换记录 (不可变)

        Raises:
            IllegalTransitionError: 非法跃迁
        """
        if not isinstance(dst, FSMState):
            raise TypeError(
                f"dst 必须为 FSMState 枚举,当前类型: {type(dst).__name__}"
            )

        async with self._lock:
            src = self._state

            # 同状态自跃迁 → 视为 no-op,但不算非法 (例: 多次 LISTENING 重置)
            if src == dst:
                logger.debug(
                    f"[FSM:{self._agent_id}] no-op self-transition · "
                    f"state={src.value} · reason={reason!r}"
                )
                # 仍生成记录便于审计
                record = TransitionRecord(
                    agent_id=self._agent_id,
                    src=src,
                    dst=dst,
                    at_monotonic=time.monotonic(),
                    at_wall_unix=time.time(),
                    reason=f"[no-op] {reason}",
                )
                # 自跃迁不更新 entered_at,不计入 transitions_total
                return record

            # 合法性校验
            if not is_legal_transition(src, dst):
                self._illegal_attempts_total += 1
                logger.error(
                    f"[FSM:{self._agent_id}] illegal transition "
                    f"{src.value} → {dst.value} · reason={reason!r}"
                )
                raise IllegalTransitionError(
                    agent_id=self._agent_id,
                    src=src,
                    dst=dst,
                )

            # 执行跃迁
            now_mono = time.monotonic()
            now_wall = time.time()
            record = TransitionRecord(
                agent_id=self._agent_id,
                src=src,
                dst=dst,
                at_monotonic=now_mono,
                at_wall_unix=now_wall,
                reason=reason,
            )

            self._state = dst
            self._entered_at_monotonic = now_mono
            self._entered_at_wall = now_wall
            self._transitions_total += 1
            self._history.append(record)

            logger.info(
                f"[FSM:{self._agent_id}] {src.value} → {dst.value} · "
                f"reason={reason!r} · #{self._transitions_total}"
            )

        # ⚠️ 钩子在锁外触发,避免钩子阻塞导致 transition 排队
        await self._fire_transition_hooks(record)
        return record

    async def _fire_transition_hooks(self, record: TransitionRecord) -> None:
        """异步触发所有注册钩子,单个钩子异常不影响其他钩子。"""
        if not self._on_transition_hooks:
            return

        # 并发触发,gather with return_exceptions=True 防止任一失败拖垮全部
        results = await asyncio.gather(
            *(hook(record) for hook in self._on_transition_hooks),
            return_exceptions=True,
        )
        for hook, result in zip(self._on_transition_hooks, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    f"[FSM:{self._agent_id}] transition hook 异常 · "
                    f"hook={hook!r} · err={result!r}"
                )

    # ────────────────────────────────────────────────────────────
    # 守门: LLM 调用前置断言
    # ────────────────────────────────────────────────────────────

    def assert_can_invoke_llm(self) -> None:
        """
        在 LLM 推理入口前调用,断言当前状态允许调用。

        ⚠️ llm_router.py 必须在 acquire 信号量之前调用此方法,
           否则会出现"LISTENING 态偷偷调 LLM"的算力泄漏。
        """
        if is_llm_forbidden(self._state):
            raise LLMInvocationForbiddenError(
                agent_id=self._agent_id,
                current_state=self._state,
            )

    # ────────────────────────────────────────────────────────────
    # 观测
    # ────────────────────────────────────────────────────────────

    def snapshot(self) -> FSMSnapshot:
        return FSMSnapshot(
            agent_id=self._agent_id,
            state=self._state,
            entered_at_monotonic=self._entered_at_monotonic,
            dwell_seconds=self.dwell_seconds,
            transitions_total=self._transitions_total,
            illegal_attempts_total=self._illegal_attempts_total,
        )

    def history(self, last_n: Optional[int] = None) -> list[TransitionRecord]:
        """
        导出转换历史 (副本),按时间顺序 (旧 → 新)。

        Args:
            last_n: 仅取最近 N 条; None 则全量
        """
        records = list(self._history)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records


# ──────────────────────────────────────────────────────────────────────────────
# Registry: 跨 Agent 协调 + 批量屏障
# ──────────────────────────────────────────────────────────────────────────────


class FSMRegistry:
    """
    所有 Agent FSM 的集中注册表 + 批量协调器。

    职责:
        - 注册 / 注销 Agent FSM
        - 提供只读快照视图 (供 HUD)
        - 批量 transition (UPDATING 屏障)
        - 全局钩子转发 (供 throttle 节流推送 WS 状态包)
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentFSM] = {}

        # 批量操作锁 → 防止 batch_transition 与单 Agent transition 交错
        self._batch_lock: asyncio.Lock = asyncio.Lock()

        # 全局钩子 (适用于所有 Agent 的统一 WS 推送)
        self._global_hooks: list[TransitionHook] = []

    # ────────────────────────────────────────────────────────────
    # 注册管理
    # ────────────────────────────────────────────────────────────

    def register(
        self,
        agent_id: str,
        initial_state: FSMState = FSMState.IDLE,
    ) -> AgentFSM:
        """注册一个新的 Agent FSM,返回实例。重复注册抛错。"""
        if agent_id in self._agents:
            raise AgentAlreadyRegisteredError(
                f"Agent {agent_id!r} 已注册,严禁重复创建 FSM"
            )

        fsm = AgentFSM(agent_id=agent_id, initial_state=initial_state)
        # 自动挂载所有全局钩子
        for hook in self._global_hooks:
            fsm.add_transition_hook(hook)

        self._agents[agent_id] = fsm
        logger.info(
            f"[FSMRegistry] 注册 Agent {agent_id!r} · "
            f"initial={initial_state.value} · total={len(self._agents)}"
        )
        return fsm

    def unregister(self, agent_id: str) -> bool:
        """注销 Agent FSM,成功返回 True。"""
        if agent_id not in self._agents:
            return False
        del self._agents[agent_id]
        logger.info(f"[FSMRegistry] 注销 Agent {agent_id!r}")
        return True

    def get(self, agent_id: str) -> AgentFSM:
        """获取已注册的 FSM。未注册抛 AgentNotRegisteredError。"""
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise AgentNotRegisteredError(
                f"Agent {agent_id!r} 未注册"
            ) from exc

    def all_agent_ids(self) -> tuple[str, ...]:
        """全部已注册 Agent ID (按注册顺序,Python 3.7+ dict 保序)。"""
        return tuple(self._agents.keys())

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    # ────────────────────────────────────────────────────────────
    # 全局钩子
    # ────────────────────────────────────────────────────────────

    def add_global_transition_hook(self, hook: TransitionHook) -> None:
        """
        注册全局钩子 · 自动挂载到所有当前已注册和未来注册的 Agent。

        典型用法: throttle.py 在启动时注册"WS 状态包推送钩子",
        所有 Agent 的状态变更都会被节流后推送给前端。
        """
        if not callable(hook):
            raise TypeError("global hook 必须可调用")
        self._global_hooks.append(hook)
        # 追加到已注册的 Agent
        for fsm in self._agents.values():
            fsm.add_transition_hook(hook)
        logger.debug(
            f"[FSMRegistry] 全局钩子已挂载到 {len(self._agents)} 个 Agent"
        )

    # ────────────────────────────────────────────────────────────
    # 批量 UPDATING 屏障
    # ────────────────────────────────────────────────────────────

    async def batch_transition(
        self,
        dst: FSMState,
        reason: str = "batch_barrier",
        agent_ids: Optional[Iterable[str]] = None,
        skip_illegal: bool = False,
    ) -> dict[str, TransitionRecord]:
        """
        批量原子性切换多个 Agent 到同一目标状态。

        典型场景: 回合结束时所有 Agent → UPDATING,
        Aggro 引擎完成批量更新后,再批量 → LISTENING 进入下轮。

        Args:
            dst: 目标状态
            reason: 转换原因
            agent_ids: 限定的 Agent ID 集合; None = 所有已注册
            skip_illegal: 遇到非法跃迁是否跳过 (True) 还是抛错 (False)

        Returns:
            dict[agent_id, TransitionRecord]: 成功跃迁的记录

        Raises:
            IllegalTransitionError: 当 skip_illegal=False 且任一 Agent 跃迁非法
            AgentNotRegisteredError: agent_ids 中含未注册 ID
        """
        async with self._batch_lock:
            target_ids = (
                tuple(self._agents.keys())
                if agent_ids is None
                else tuple(agent_ids)
            )

            # 预校验所有 ID 已注册 (一次性检查,失败立即抛)
            for aid in target_ids:
                if aid not in self._agents:
                    raise AgentNotRegisteredError(
                        f"batch_transition 引用了未注册 Agent {aid!r}"
                    )

            # 预校验合法性 (skip_illegal=False 时一票否决)
            if not skip_illegal:
                for aid in target_ids:
                    fsm = self._agents[aid]
                    if fsm.state != dst and not is_legal_transition(
                        fsm.state, dst
                    ):
                        raise IllegalTransitionError(
                            agent_id=aid,
                            src=fsm.state,
                            dst=dst,
                        )

            logger.info(
                f"[FSMRegistry] batch_transition · "
                f"target={dst.value} · agents={len(target_ids)} · reason={reason!r}"
            )

            # 实际执行: 并发 transition
            # ⚠️ 这里 gather + return_exceptions 是为了 skip_illegal 路径,
            #    上面已预校验过的合法路径不会到达 except 分支
            tasks = [
                self._agents[aid].transition(dst=dst, reason=reason)
                for aid in target_ids
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            successful: dict[str, TransitionRecord] = {}
            for aid, result in zip(target_ids, results, strict=True):
                if isinstance(result, IllegalTransitionError):
                    if skip_illegal:
                        logger.warning(
                            f"[FSMRegistry] 跳过非法跃迁 · agent={aid!r} · {result}"
                        )
                        continue
                    # 不应该到这里 (上面预校验过) —— 但兜底重抛
                    raise result
                elif isinstance(result, BaseException):
                    logger.error(
                        f"[FSMRegistry] batch_transition 期间异常 · "
                        f"agent={aid!r} · err={result!r}"
                    )
                    if not skip_illegal:
                        raise result
                else:
                    successful[aid] = result

            return successful

    # ────────────────────────────────────────────────────────────
    # 只读快照
    # ────────────────────────────────────────────────────────────

    def snapshot_all(self) -> dict[str, FSMSnapshot]:
        """全量 FSM 快照 (按 agent_id),适合一次性 JSON 序列化。"""
        return {aid: fsm.snapshot() for aid, fsm in self._agents.items()}

    def snapshot_dict(self) -> dict[str, dict[str, object]]:
        """全量快照的 JSON 友好字典视图。"""
        return {aid: snap.to_dict() for aid, snap in self.snapshot_all().items()}

    def count_in_state(self, state: FSMState) -> int:
        """统计当前处于指定状态的 Agent 数。"""
        return sum(1 for fsm in self._agents.values() if fsm.state == state)


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_registry_singleton: Optional[FSMRegistry] = None


def get_fsm_registry() -> FSMRegistry:
    """
    获取全局 FSMRegistry 单例。

    使用模式:
        from backend.core.fsm import get_fsm_registry, FSMState

        registry = get_fsm_registry()
        intj_fsm = registry.register("INTJ_logician")
        await intj_fsm.transition(FSMState.LISTENING, reason="topic_injected")
    """
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = FSMRegistry()
    return _registry_singleton


def reset_registry_for_testing() -> None:
    """
    [仅供测试] 重置全局 Registry。

    ⚠️ 生产代码严禁调用。
    """
    global _registry_singleton
    _registry_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 状态枚举
    "FSMState",
    # 转换守门
    "is_legal_transition",
    "is_llm_forbidden",
    # 主体
    "AgentFSM",
    "FSMRegistry",
    # 数据类
    "TransitionRecord",
    "FSMSnapshot",
    # 异常
    "FSMError",
    "IllegalTransitionError",
    "LLMInvocationForbiddenError",
    "AgentNotRegisteredError",
    "AgentAlreadyRegisteredError",
    # 钩子签名
    "TransitionHook",
    # 单例
    "get_fsm_registry",
    "reset_registry_for_testing",
]