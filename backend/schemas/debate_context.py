"""
==============================================================================
backend/schemas/debate_context.py
------------------------------------------------------------------------------
数据契约层 · 议题 / 回合 / 影子上下文 / 沙盒快照模型 (Pydantic v2)

定位:
    将 core.context_bus 的内部数据类 (Topic / Turn / TruncationReason)
    映射为对外契约,作为:
        1. REST /api/sandbox/snapshot 的 response_model (一次性水合)
        2. POST /api/control 的请求/响应契约
        3. WebSocket TURN_SEALED / TOPIC 包载荷的强类型化基础

设计原则同 agent_state.py / stream_packet.py:
    1. 零业务逻辑 - 仅"形状"
    2. 单向依赖 - schemas 仅导入 core 用于断言对齐
    3. snake_case 命名 - 与前端 protocol.d.ts 严格对齐
    4. 启动期字面量断言 - TruncationReasonModel 对齐 core 枚举
    5. from_internal() 构造器 - 集中维护映射

==============================================================================
"""

from __future__ import annotations

import enum
from typing import Final, Literal, Optional

from pydantic import Field, field_validator

from backend.core.context_bus import (
    Topic as _CoreTopic,
    TruncationReason as _CoreTruncationReason,
    Turn as _CoreTurn,
)
from backend.schemas.agent_state import (
    AgentStateModel,
    ArbitratorMetricsModel,
    LLMSemaphoreMetricsModel,
    WatchdogMetricsModel,
    _StrictModel,
)


# ──────────────────────────────────────────────────────────────────────────────
# Truncation Reason · 与 core 字面量对齐
# ──────────────────────────────────────────────────────────────────────────────


class TruncationReasonModel(str, enum.Enum):
    """
    Turn 截断原因的契约镜像 · 与 core.context_bus.TruncationReason 字面量对齐。

    单独定义而非复用 core 枚举,是为了:
        1. 隔离契约层与内部实现
        2. 让前端 TS 与本枚举形成 1:1 字面量对齐
    """

    NORMAL = "NORMAL"
    INTERRUPTED = "INTERRUPTED"
    WATCHDOG_TIMEOUT = "WATCHDOG_TIMEOUT"
    SYSTEM_ABORTED = "SYSTEM_ABORTED"

    @staticmethod
    def from_core(r: _CoreTruncationReason) -> "TruncationReasonModel":
        return TruncationReasonModel(r.value)


def _assert_truncation_alignment() -> None:
    """启动期断言: 本模型与 core.context_bus.TruncationReason 字面量一致。"""
    core_values = {r.value for r in _CoreTruncationReason}
    schema_values = {r.value for r in TruncationReasonModel}
    if core_values != schema_values:
        raise RuntimeError(
            f"[debate_context] TruncationReason 字面量漂移 · "
            f"core={sorted(core_values)} · schemas={sorted(schema_values)}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Topic 模型
# ──────────────────────────────────────────────────────────────────────────────


class TopicModel(_StrictModel):
    """
    单层议题契约 · core.context_bus.Topic 的对外镜像。

    议题栈支持嵌套:
        主议题 → 子议题 → 派生议题
    parent_topic_id 形成单向链表,根议题 parent_topic_id=None。
    """

    topic_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="议题唯一 ID (UUID4 字符串)",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="议题标题 (例: 自由意志是否存在)",
    )
    description: str = Field(
        "",
        max_length=2048,
        description="议题详细说明 (允许为空)",
    )
    parent_topic_id: Optional[str] = Field(
        None,
        max_length=64,
        description="父议题 ID · None = 根议题",
    )
    pushed_at_wall_unix: float = Field(
        ...,
        gt=0.0,
        description="推入议题栈时的 Unix 时间戳",
    )

    @staticmethod
    def from_internal(t: _CoreTopic) -> "TopicModel":
        return TopicModel(
            topic_id=t.topic_id,
            title=t.title,
            description=t.description,
            parent_topic_id=t.parent_topic_id,
            pushed_at_wall_unix=round(t.pushed_at_wall_unix, 6),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Turn 模型
# ──────────────────────────────────────────────────────────────────────────────


class TurnModel(_StrictModel):
    """
    单轮发言完整契约 · core.context_bus.Turn 的对外镜像。

    含影子后缀检测、持续时长派生指标。
    """

    turn_id: str = Field(..., min_length=1, max_length=64)
    agent_id: str = Field(..., min_length=1, max_length=64)
    topic_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="该发言所属的议题 ID",
    )
    text: str = Field(
        ...,
        max_length=16384,
        description="发言完整文本 (含影子后缀,若被截断)",
    )
    truncation: TruncationReasonModel = Field(...)
    interrupter_agent_id: Optional[str] = Field(
        None,
        max_length=64,
        description="打断者 ID · 仅 INTERRUPTED 时有效",
    )

    started_at_wall_unix: float = Field(..., gt=0.0)
    sealed_at_wall_unix: float = Field(..., gt=0.0)
    duration_seconds: float = Field(
        ...,
        ge=0.0,
        description="发言持续时长 (派生: sealed - started)",
    )

    is_truncated: bool = Field(
        ...,
        description="派生: truncation != NORMAL",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="诊断元数据 (prefix_target / token_count 等),仅字符串值",
    )

    @field_validator("metadata")
    @classmethod
    def _validate_metadata_str_values(cls, v: dict[str, str]) -> dict[str, str]:
        """metadata 限定 dict[str, str],拒绝其他类型混入。"""
        for k, val in v.items():
            if not isinstance(k, str) or not isinstance(val, str):
                raise ValueError(
                    f"metadata 仅允许 str→str 映射,违规: "
                    f"{k!r}({type(k).__name__}) → {val!r}({type(val).__name__})"
                )
        return v

    @staticmethod
    def from_internal(turn: _CoreTurn) -> "TurnModel":
        duration = max(0.0, turn.sealed_at_monotonic - turn.started_at_monotonic)
        # 兜底: started_at_wall_unix 不在 Turn 内,通过 sealed_at_wall_unix - duration 推导
        # (Turn 内部使用 monotonic 计算 duration,wall 时间仅记录 sealed)
        # 这里给 started 一个合理的等效 wall 时间
        started_wall = max(1.0, turn.sealed_at_wall_unix - duration)

        return TurnModel(
            turn_id=turn.turn_id,
            agent_id=turn.agent_id,
            topic_id=turn.topic_id,
            text=turn.text,
            truncation=TruncationReasonModel.from_core(turn.truncation),
            interrupter_agent_id=turn.interrupter_agent_id,
            started_at_wall_unix=round(started_wall, 6),
            sealed_at_wall_unix=round(turn.sealed_at_wall_unix, 6),
            duration_seconds=round(duration, 4),
            is_truncated=turn.is_truncated(),
            metadata=dict(turn.metadata),
        )


# ──────────────────────────────────────────────────────────────────────────────
# 议题栈视图
# ──────────────────────────────────────────────────────────────────────────────


class TopicStackModel(_StrictModel):
    """
    议题栈完整视图。

    stack 顺序: 栈底 → 栈顶 (current 为 stack[-1])
    """

    current: Optional[TopicModel] = Field(
        None,
        description="当前栈顶议题 (None = 栈空)",
    )
    stack: list[TopicModel] = Field(
        default_factory=list,
        description="议题栈 (栈底 → 栈顶)",
        max_length=32,
    )
    depth: int = Field(
        ...,
        ge=0,
        le=32,
        description="栈深度 (= len(stack))",
    )

    @staticmethod
    def from_internal_stack(stack_tuple: tuple[_CoreTopic, ...]) -> "TopicStackModel":
        models = [TopicModel.from_internal(t) for t in stack_tuple]
        return TopicStackModel(
            current=models[-1] if models else None,
            stack=models,
            depth=len(models),
        )


# ──────────────────────────────────────────────────────────────────────────────
# 短记忆 + 溢出统计
# ──────────────────────────────────────────────────────────────────────────────


class ShortMemoryViewModel(_StrictModel):
    """短记忆滑动窗口视图。"""

    turns: list[TurnModel] = Field(
        default_factory=list,
        description="按时间顺序的最近 N 轮发言 (旧→新)",
        max_length=64,
    )
    window_size: int = Field(
        ...,
        ge=2,
        le=64,
        description="窗口容量 (settings.SHORT_MEMORY_WINDOW_TURNS)",
    )
    current_size: int = Field(..., ge=0, le=64)
    overflow_queue_size: int = Field(
        ...,
        ge=0,
        description="待 Summarizer 归档的溢出条数",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 会话元信息
# ──────────────────────────────────────────────────────────────────────────────


class SessionStatus(str, enum.Enum):
    """会话状态枚举。"""

    INITIALIZING = "INITIALIZING"
    """启动中,Agent 注册尚未完成"""

    READY = "READY"
    """就绪,等待议题注入"""

    RUNNING = "RUNNING"
    """运行中,辩论进行"""

    PAUSED = "PAUSED"
    """已暂停 (PAUSE_SESSION)"""

    SHUTTING_DOWN = "SHUTTING_DOWN"
    """正在优雅关闭"""


class SessionInfoModel(_StrictModel):
    """会话运行时元信息。"""

    session_id: str = Field(..., min_length=1, max_length=64)
    status: SessionStatus = Field(...)
    started_at_wall_unix: float = Field(..., gt=0.0)
    uptime_seconds: float = Field(..., ge=0.0)
    total_turns_sealed: int = Field(
        ...,
        ge=0,
        description="累计封存 Turn 数 (含影子截断)",
    )
    total_interrupts: int = Field(
        ...,
        ge=0,
        description="累计抢话次数",
    )
    backend_version: str = Field(
        "1.0.0",
        max_length=32,
        description="后端语义版本",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 辩论上下文聚合视图 (供 WS TURN_SEALED 等)
# ──────────────────────────────────────────────────────────────────────────────


class DebateSnapshotModel(_StrictModel):
    """
    辩论上下文中等粒度聚合视图。

    用于 ContextBus 主动广播至前端的快照;
    REST 全量快照请使用 CompleteSandboxSnapshot。
    """

    topic_stack: TopicStackModel = Field(...)
    short_memory: ShortMemoryViewModel = Field(...)
    ongoing_agent_ids: list[str] = Field(
        default_factory=list,
        description="当前正在生成发言 (未封存 Turn) 的 Agent ID 列表",
        max_length=8,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 全量沙盒快照 (REST /api/sandbox/snapshot 的 response_model)
# ──────────────────────────────────────────────────────────────────────────────


class CompleteSandboxSnapshot(_StrictModel):
    """
    /api/sandbox/snapshot 的 response_model · HUD 初始化水合的全量视图。

    前端首次连接时拉取此对象,完成所有面板的初始填充;
    后续增量通过 WebSocket STATE/TEXT/INTERRUPT/TURN_SEALED/TOPIC 维护。
    """

    session: SessionInfoModel = Field(...)
    debate: DebateSnapshotModel = Field(...)
    agents: list[AgentStateModel] = Field(
        ...,
        description="8 个 Agent 完整运行时状态",
        max_length=16,  # 容忍未来扩展
    )
    arbitrator: ArbitratorMetricsModel = Field(...)
    watchdog: WatchdogMetricsModel = Field(...)
    semaphore: LLMSemaphoreMetricsModel = Field(...)
    server_at_wall_unix: float = Field(..., gt=0.0)


# ──────────────────────────────────────────────────────────────────────────────
# 控制 API (REST) 请求 / 响应契约
# ──────────────────────────────────────────────────────────────────────────────


class InjectTopicRequest(_StrictModel):
    """POST /api/control/topic/inject 请求体。"""

    title: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="议题标题",
    )
    description: str = Field(
        "",
        max_length=2048,
        description="议题详细说明 (可选)",
    )
    auto_start_debate: bool = Field(
        True,
        description="注入议题后是否自动开始辩论 (调度器进入 RUNNING)",
    )


class InjectTopicResponse(_StrictModel):
    """POST /api/control/topic/inject 响应。"""

    accepted: bool = Field(...)
    topic: Optional[TopicModel] = Field(
        None,
        description="成功注入的议题 (拒绝时为 None)",
    )
    message: str = Field("", max_length=512)


class PopTopicResponse(_StrictModel):
    accepted: bool = Field(...)
    popped_topic: Optional[TopicModel] = Field(None)
    remaining_depth: int = Field(..., ge=0)
    message: str = Field("", max_length=512)


class ControlActionKind(str, enum.Enum):
    """非议题相关的控制动作类型。"""

    PAUSE = "PAUSE"
    RESUME = "RESUME"
    FORCE_INTERRUPT = "FORCE_INTERRUPT"
    EMERGENCY_STOP = "EMERGENCY_STOP"


class ControlActionRequest(_StrictModel):
    """通用控制动作请求 (POST /api/control/action)。"""

    action: ControlActionKind = Field(...)
    reason: str = Field(
        "",
        max_length=256,
        description="操作原因 (诊断与审计用)",
    )


class ControlActionResponse(_StrictModel):
    """通用控制动作响应。"""

    accepted: bool = Field(...)
    action: ControlActionKind = Field(...)
    new_status: SessionStatus = Field(...)
    message: str = Field("", max_length=512)
    server_at_wall_unix: float = Field(..., gt=0.0)


# ──────────────────────────────────────────────────────────────────────────────
# 健康检查 (用于 /api/sandbox/snapshot 的轻量探活路径)
# ──────────────────────────────────────────────────────────────────────────────


class HealthCheckResponse(_StrictModel):
    """
    极简健康检查响应 · 不包含敏感配置。

    Docker / nginx healthcheck 与 /api/sandbox/snapshot 的轻量探活
    可共用此模型。
    """

    status: Literal["ok", "degraded", "shutting_down"] = Field(...)
    session_id: str = Field(..., min_length=1, max_length=64)
    uptime_seconds: float = Field(..., ge=0.0)
    backend_version: str = Field("1.0.0", max_length=32)


# ──────────────────────────────────────────────────────────────────────────────
# 模块加载期断言
# ──────────────────────────────────────────────────────────────────────────────

# ⚠️ 任何字面量漂移都让进程启动失败 (fail-fast)
_assert_truncation_alignment()


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 枚举
    "TruncationReasonModel",
    "SessionStatus",
    "ControlActionKind",
    # 数据契约
    "TopicModel",
    "TurnModel",
    "TopicStackModel",
    "ShortMemoryViewModel",
    "SessionInfoModel",
    "DebateSnapshotModel",
    "CompleteSandboxSnapshot",
    # REST 控制 API
    "InjectTopicRequest",
    "InjectTopicResponse",
    "PopTopicResponse",
    "ControlActionRequest",
    "ControlActionResponse",
    # 健康检查
    "HealthCheckResponse",
]