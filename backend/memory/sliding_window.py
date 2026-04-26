"""
==============================================================================
backend/memory/sliding_window.py
------------------------------------------------------------------------------
短记忆滑动窗口适配层 · ContextBus 的只读视图 + Prompt 格式化器

定位:
    本模块 *不持有* 任何 Turn 数据。
    所有内容均来自 ContextBus.short_memory_snapshot() 的按需快照。
    职责仅限三类工具:
        1. 结构化切片视图 (按 Agent / 议题 / 时间窗)
        2. Prompt 上下文格式化 (含影子后缀可读化、议题切换锚点、长度裁剪)
        3. 桥接溢出 → ChromaGateway (短记忆 → 长期向量记忆数据流闭环)

设计原则:
    1. 零状态 - 本模块无副本,无锁,无生命周期
    2. 单向依赖 - 仅 ContextBus + ChromaGateway 输入,无反向回调
    3. 纯函数为主 - 切片函数全部纯函数,可单元测试
    4. Token 估算降级 - 不加载 tokenizer,启发式 chars_per_token 即可

⚠️ 工程铁律:
    - 严禁持有 Turn 副本 (按需拉取)
    - 严禁同步阻塞主路径 (Chroma 入队非阻塞)
    - Prompt 超长必须裁剪 (倒序剔除最旧,保留最近 N 条)
    - 影子后缀必须显式标注给 LLM,不能裸文本喂入

==============================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Optional

from loguru import logger

from backend.core.context_bus import (
    ContextBus,
    Topic,
    TruncationReason,
    Turn,
    get_context_bus,
)
from backend.memory.chroma_gateway import (
    ChromaGateway,
    EntryStatus,
    get_chroma_gateway,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: Prompt 字符 budget 默认值 · 14B 模型在 q4_K_M 下大约支持 8K context,
#: 字符与 token 的启发式比例约 1:1.6 (中文混合) · 留 2/3 给历史
DEFAULT_PROMPT_CHAR_BUDGET: Final[int] = 6500

#: 估算用 chars-per-token (中文混合启发式平均值)
_HEURISTIC_CHARS_PER_TOKEN: Final[float] = 1.6

#: 必须保留的最少历史轮次 (即使 budget 紧也不裁掉)
_MUST_KEEP_RECENT_TURNS: Final[int] = 2

#: 议题切换边界系统消息模板
_TOPIC_SWITCH_MARKER_TEMPLATE: Final[str] = (
    "[议题切换:{prev_title!s} → {new_title!s}]"
)

#: 起始议题边界标记
_TOPIC_INITIAL_MARKER_TEMPLATE: Final[str] = "[当前议题:{title!s}]"


# ──────────────────────────────────────────────────────────────────────────────
# 视图: 切片结果 (轻量数据类,非可变状态)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WindowView:
    """
    一次切片的不可变视图 · 仅引用 Turn 对象,不复制内容。

    ⚠️ Turn 本身就是 frozen,因此引用持有完全安全。
    """

    turns: tuple[Turn, ...]
    """按时间顺序 (旧 → 新)"""

    window_size: int
    """ContextBus 窗口容量 (诊断字段)"""

    @property
    def count(self) -> int:
        return len(self.turns)

    def is_empty(self) -> bool:
        return not self.turns

    def total_chars(self) -> int:
        return sum(len(t.text) for t in self.turns)

    def estimated_tokens(self) -> int:
        return int(self.total_chars() / _HEURISTIC_CHARS_PER_TOKEN)

    def agent_ids(self) -> tuple[str, ...]:
        """去重的发言 Agent ID 列表 (按首次出现顺序)。"""
        seen: set[str] = set()
        ordered: list[str] = []
        for t in self.turns:
            if t.agent_id not in seen:
                seen.add(t.agent_id)
                ordered.append(t.agent_id)
        return tuple(ordered)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt 消息封装 (Ollama messages 协议)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """
    标准化 Prompt 消息 · 与 Ollama /api/chat 的 messages 元素对齐。

    role 限定为 "system" / "user" / "assistant"。
    """

    role: str
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


# ──────────────────────────────────────────────────────────────────────────────
# 切片函数 (纯函数,无副作用)
# ──────────────────────────────────────────────────────────────────────────────


def view_full_window(
    bus: Optional[ContextBus] = None,
) -> WindowView:
    """
    取得当前完整短记忆窗口快照 (旧 → 新)。
    """
    bus = bus or get_context_bus()
    turns = bus.short_memory_snapshot()
    return WindowView(
        turns=tuple(turns),
        window_size=bus.short_memory_size(),
    )


def view_last_n(
    n: int,
    bus: Optional[ContextBus] = None,
) -> WindowView:
    """取最近 N 条 Turn 的视图。"""
    if n < 0:
        raise ValueError(f"n 必须 ≥ 0,当前: {n}")
    if n == 0:
        return WindowView(turns=(), window_size=0)
    bus = bus or get_context_bus()
    turns = bus.short_memory_snapshot(last_n=n)
    return WindowView(turns=tuple(turns), window_size=bus.short_memory_size())


def view_by_agent(
    agent_id: str,
    bus: Optional[ContextBus] = None,
) -> WindowView:
    """筛选特定 Agent 在短记忆中的所有发言。"""
    if not agent_id:
        raise ValueError("agent_id 必须非空")
    bus = bus or get_context_bus()
    full = bus.short_memory_snapshot()
    filtered = tuple(t for t in full if t.agent_id == agent_id)
    return WindowView(turns=filtered, window_size=bus.short_memory_size())


def view_by_topic(
    topic_id: str,
    bus: Optional[ContextBus] = None,
) -> WindowView:
    """筛选特定议题层下的所有发言。"""
    if not topic_id:
        raise ValueError("topic_id 必须非空")
    bus = bus or get_context_bus()
    full = bus.short_memory_snapshot()
    filtered = tuple(t for t in full if t.topic_id == topic_id)
    return WindowView(turns=filtered, window_size=bus.short_memory_size())


def view_recent_seconds(
    seconds: float,
    bus: Optional[ContextBus] = None,
) -> WindowView:
    """
    取最近 N 秒内的 Turn (基于 sealed_at_monotonic)。

    供 ESTP 真空期判定 / 全局沸腾窗口分析使用。
    """
    if seconds <= 0:
        raise ValueError(f"seconds 必须 > 0,当前: {seconds}")
    bus = bus or get_context_bus()
    full = bus.short_memory_snapshot()
    if not full:
        return WindowView(turns=(), window_size=bus.short_memory_size())

    # 用最新 Turn 的 sealed_at_monotonic 作为 now (而非真实 time.monotonic)
    # 这样的好处: 无 Turn 时直接 0,不会触发 wall-clock 抖动
    latest_mono = full[-1].sealed_at_monotonic
    threshold = latest_mono - seconds
    filtered = tuple(t for t in full if t.sealed_at_monotonic >= threshold)
    return WindowView(turns=filtered, window_size=bus.short_memory_size())


def filter_truncated_only(view: WindowView) -> WindowView:
    """筛选被截断的 Turn (供故障复盘 / 影子后缀诊断)。"""
    filtered = tuple(t for t in view.turns if t.is_truncated())
    return WindowView(turns=filtered, window_size=view.window_size)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt 格式化
# ──────────────────────────────────────────────────────────────────────────────


def _render_truncation_note(turn: Turn) -> str:
    """把 Turn 的截断原因渲染为 LLM 可读的简短标记 (置于发言前缀)。"""
    if turn.truncation == TruncationReason.NORMAL:
        return ""
    if turn.truncation == TruncationReason.INTERRUPTED:
        who = turn.interrupter_agent_id or "他人"
        return f"(被{who}打断) "
    if turn.truncation == TruncationReason.WATCHDOG_TIMEOUT:
        return "(超时熔断) "
    if turn.truncation == TruncationReason.SYSTEM_ABORTED:
        return "(系统中止) "
    return ""


def _format_turn_as_user_message(turn: Turn) -> PromptMessage:
    """
    将单 Turn 渲染为 user role 的 PromptMessage。

    使用 user role 而非 assistant 是因为:
        - 当前 Agent 视角下,他人发言是输入而非自身历史助手输出
        - 避免 Ollama 把所有人的话当作自身上下文学习
    格式: "[Agent_id]: 内容..."
    """
    note = _render_truncation_note(turn)
    body = turn.text or ""
    content = f"[{turn.agent_id}]:{note}{body}"
    return PromptMessage(role="user", content=content)


def _topic_switch_messages(
    turns: Iterable[Turn],
    topics_by_id: dict[str, Topic],
) -> list[PromptMessage]:
    """
    在 Turn 列表中检测议题边界,返回插入了边界锚点后的消息列表。

    规则:
        - 第一条发言前插入 [当前议题:...]
        - 后续发言 topic_id 与前一条不同时插入 [议题切换:A → B]
    """
    output: list[PromptMessage] = []
    last_topic_id: Optional[str] = None

    for turn in turns:
        cur_id = turn.topic_id
        if cur_id != last_topic_id:
            cur_topic = topics_by_id.get(cur_id)
            cur_title = cur_topic.title if cur_topic else cur_id
            if last_topic_id is None:
                marker = _TOPIC_INITIAL_MARKER_TEMPLATE.format(title=cur_title)
            else:
                prev_topic = topics_by_id.get(last_topic_id)
                prev_title = prev_topic.title if prev_topic else last_topic_id
                marker = _TOPIC_SWITCH_MARKER_TEMPLATE.format(
                    prev_title=prev_title,
                    new_title=cur_title,
                )
            output.append(PromptMessage(role="system", content=marker))
            last_topic_id = cur_id

        output.append(_format_turn_as_user_message(turn))

    return output


def _trim_to_char_budget(
    messages: list[PromptMessage],
    char_budget: int,
    must_keep_recent_count: int,
) -> list[PromptMessage]:
    """
    倒序裁剪以满足字符预算,但必保最近 N 条不被丢弃。

    裁剪策略:
        1. 总字符 ≤ budget → 不裁
        2. 否则从最旧开始丢弃,直到满足或仅剩"必保区"
        3. 仍超出 → 截断"必保区"中最旧那一条的中段 (而非整条丢弃)
    """
    if char_budget <= 0:
        # 极端配置: 直接返回必保区
        return messages[-must_keep_recent_count:] if must_keep_recent_count else []

    total = sum(len(m.content) for m in messages)
    if total <= char_budget:
        return messages

    # 1) 标记必保区
    n = len(messages)
    keep_from = max(0, n - max(must_keep_recent_count, 1))

    # 2) 倒序丢弃 [0, keep_from) 直到满足
    drop_count = 0
    while total > char_budget and drop_count < keep_from:
        total -= len(messages[drop_count].content)
        drop_count += 1

    trimmed = messages[drop_count:]

    if total <= char_budget:
        return trimmed

    # 3) 仍超出 → 截断必保区最旧那一条的中段
    # 仅保留头/尾各 budget/3 字符,中间用 [...省略...] 替代
    if not trimmed:
        return []
    oldest = trimmed[0]
    over_by = total - char_budget
    if over_by <= 0:
        return trimmed
    new_len = max(64, len(oldest.content) - over_by - 32)
    if new_len < len(oldest.content):
        head = oldest.content[: new_len // 2]
        tail = oldest.content[-(new_len // 2) :]
        truncated_content = f"{head}…[省略中段]…{tail}"
        trimmed[0] = PromptMessage(role=oldest.role, content=truncated_content)

    return trimmed


def format_for_llm_prompt(
    *,
    bus: Optional[ContextBus] = None,
    last_n: Optional[int] = None,
    char_budget: int = DEFAULT_PROMPT_CHAR_BUDGET,
    must_keep_recent_turns: int = _MUST_KEEP_RECENT_TURNS,
    extra_system_prefix: Optional[str] = None,
) -> list[PromptMessage]:
    """
    把短记忆窗口格式化为 Ollama messages 列表。

    Args:
        bus: ContextBus (None = 全局单例)
        last_n: 最多取最近 N 条 (None = 全部窗口)
        char_budget: 字符预算 (含锚点)
        must_keep_recent_turns: 必保最近轮次数,即使预算紧
        extra_system_prefix: 额外前置 system 消息 (例: 当前议题概述)

    Returns:
        list[PromptMessage] · 旧 → 新 · 含议题边界锚点 · 已裁剪到 budget
    """
    bus = bus or get_context_bus()

    if last_n is not None:
        view = view_last_n(last_n, bus=bus)
    else:
        view = view_full_window(bus=bus)

    if view.is_empty():
        # 退化: 仅返回 extra_system_prefix (若有)
        if extra_system_prefix:
            return [PromptMessage(role="system", content=extra_system_prefix)]
        return []

    # 构造 topic_id → Topic 的 lookup (来自当前栈)
    topics_by_id: dict[str, Topic] = {
        t.topic_id: t for t in bus.topic_stack_snapshot()
    }

    messages = _topic_switch_messages(view.turns, topics_by_id)

    if extra_system_prefix:
        messages.insert(
            0, PromptMessage(role="system", content=extra_system_prefix)
        )

    return _trim_to_char_budget(
        messages=messages,
        char_budget=char_budget,
        must_keep_recent_count=must_keep_recent_turns,
    )


def format_for_llm_prompt_dict(
    **kwargs: object,
) -> list[dict[str, str]]:
    """
    便利包装: 直接返回 list[{role, content}] (Ollama 协议)。

    供 llm_router.invoke_chat_stream 直接消费。
    """
    msgs = format_for_llm_prompt(**kwargs)  # type: ignore[arg-type]
    return [m.to_dict() for m in msgs]


# ──────────────────────────────────────────────────────────────────────────────
# 桥接: 溢出 Turn → ChromaGateway
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """单次桥接执行报告。"""

    inspected: int
    """从 ContextBus.pop_overflow 取出的条目数"""

    enqueued: int
    """成功入队 ChromaGateway 的条目数"""

    skipped_empty: int
    """因正文为空被跳过的条目数"""

    dropped_overflow: int
    """ChromaGateway 队列满被丢弃的条目数"""

    def to_dict(self) -> dict[str, int]:
        return {
            "inspected": self.inspected,
            "enqueued": self.enqueued,
            "skipped_empty": self.skipped_empty,
            "dropped_overflow": self.dropped_overflow,
        }


def _build_metadata_for_chroma(turn: Turn) -> dict[str, object]:
    """
    从 Turn 抽取 ChromaDB metadata (仅标量值)。

    复杂值在 ChromaGateway.MemoryEntry.to_chroma_kwargs 内会再降级一次,
    此处仅做精简 / 必备字段补齐。
    """
    meta: dict[str, object] = {
        "turn_id": turn.turn_id,
        "agent_id": turn.agent_id,
        "topic_id": turn.topic_id,
        "truncation": turn.truncation.value,
        "is_truncated": turn.is_truncated(),
        "duration_seconds": round(turn.duration_seconds(), 4),
        "sealed_at_wall_unix": round(turn.sealed_at_wall_unix, 6),
    }
    # 透传 Turn 自身 metadata 中的 prefix_target / token_count 等
    # 只取已知 string 类型字段,避免异类污染
    for key in ("prefix_target", "token_count"):
        v = turn.metadata.get(key)
        if isinstance(v, str) and v:
            meta[key] = v
    if turn.interrupter_agent_id:
        meta["interrupter_agent_id"] = turn.interrupter_agent_id
    return meta


def bridge_overflow_to_chroma(
    *,
    bus: Optional[ContextBus] = None,
    gateway: Optional[ChromaGateway] = None,
    max_items: int = 16,
) -> BridgeReport:
    """
    把 ContextBus 的溢出队列条目桥接到 ChromaGateway。

    典型用法 (Summarizer 周期调用):
        report = bridge_overflow_to_chroma(max_items=8)
        if report.dropped_overflow > 0:
            logger.warning(...)

    Args:
        bus: 默认全局
        gateway: 默认全局
        max_items: 单次最多桥接条数 (与 Chroma 攒批 batch 对齐)

    Returns:
        BridgeReport
    """
    if max_items < 1:
        raise ValueError(f"max_items 必须 ≥ 1,当前: {max_items}")

    bus = bus or get_context_bus()
    gw = gateway or get_chroma_gateway()

    overflow = bus.pop_overflow(max_items=max_items)
    inspected = len(overflow)
    enqueued = 0
    skipped_empty = 0
    dropped_overflow = 0

    for turn in overflow:
        if not turn.text or not turn.text.strip():
            skipped_empty += 1
            continue
        try:
            status = gw.enqueue(
                turn_id=turn.turn_id,
                agent_id=turn.agent_id,
                topic_id=turn.topic_id,
                content=turn.text,
                metadata=_build_metadata_for_chroma(turn),
            )
        except Exception as exc:
            # gateway 未运行等异常 → 记录但不抛 (短记忆已弹出,不可逆)
            logger.warning(
                f"[sliding_window] bridge enqueue 异常 (该条丢失) · "
                f"turn={turn.turn_id!r} · {exc!r}"
            )
            dropped_overflow += 1
            continue

        if status == EntryStatus.QUEUED:
            enqueued += 1
        else:
            dropped_overflow += 1

    if inspected > 0:
        logger.debug(
            f"[sliding_window] bridge_overflow_to_chroma · "
            f"inspected={inspected} · enqueued={enqueued} · "
            f"skipped_empty={skipped_empty} · dropped={dropped_overflow}"
        )

    return BridgeReport(
        inspected=inspected,
        enqueued=enqueued,
        skipped_empty=skipped_empty,
        dropped_overflow=dropped_overflow,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 诊断快照
# ──────────────────────────────────────────────────────────────────────────────


def diagnostic_snapshot(
    bus: Optional[ContextBus] = None,
) -> dict[str, object]:
    """
    返回当前窗口的诊断信息 (供 /api/sandbox/snapshot 透出)。
    """
    bus = bus or get_context_bus()
    view = view_full_window(bus=bus)
    return {
        "short_memory_count": view.count,
        "short_memory_window_size": view.window_size,
        "short_memory_total_chars": view.total_chars(),
        "short_memory_estimated_tokens": view.estimated_tokens(),
        "short_memory_active_agents": list(view.agent_ids()),
        "overflow_queue_size": bus.overflow_queue_size(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 常量
    "DEFAULT_PROMPT_CHAR_BUDGET",
    # 数据类
    "WindowView",
    "PromptMessage",
    "BridgeReport",
    # 切片函数
    "view_full_window",
    "view_last_n",
    "view_by_agent",
    "view_by_topic",
    "view_recent_seconds",
    "filter_truncated_only",
    # Prompt 格式化
    "format_for_llm_prompt",
    "format_for_llm_prompt_dict",
    # 桥接
    "bridge_overflow_to_chroma",
    # 诊断
    "diagnostic_snapshot",
]