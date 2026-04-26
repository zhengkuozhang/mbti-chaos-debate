"""
==============================================================================
backend/schemas/stream_packet.py
------------------------------------------------------------------------------
WebSocket 双向通信协议契约 (Pydantic v2 · Tagged Union)

定位:
    后端与前端 (frontend/src/types/protocol.d.ts) 严格 1:1 对齐的协议宪法。

设计原则:
    1. 出站 (Server → Client) 包: 8 种 kind,用 Discriminated Union 精确分发
    2. 入站 (Client → Server) 包: 6 种控制指令 kind
    3. Envelope 三段式: {kind, payload, server_at_wall_unix} (出站)
                     {kind, payload, client_at_wall_unix?} (入站)
    4. 载荷强校验: 每种 kind 独立 Payload 模型,缺字段直接拒绝
    5. 辅助工厂函数: make_xxx_packet() 屏蔽样板
    6. 与 core.throttle.PacketKind 字面量对齐 (启动期断言)

⚠️ 工程铁律:
    - 严禁字面量漂移 (启动断言守门)
    - schemas 不反向依赖 core 业务路径 (仅断言用 import)
    - 未知 kind 入站包必须返回明确错误,不可静默接受
    - 所有 Payload 字段命名严格 snake_case,与前端 TS 对齐

==============================================================================
"""

from __future__ import annotations

import enum
import time
from typing import Annotated, Final, Literal, Optional, Union

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)

from backend.schemas.agent_state import (
    AgentStateModel,
    ArbitratorMetricsModel,
    LLMSemaphoreMetricsModel,
    WatchdogMetricsModel,
    _StrictModel,
)


# ──────────────────────────────────────────────────────────────────────────────
# Packet Kind 枚举 (出站 / 入站分离)
# ──────────────────────────────────────────────────────────────────────────────


class OutboundKind(str, enum.Enum):
    """
    服务端 → 客户端推送的包类型。

    字面量值必须与 core.throttle.PacketKind 严格一致 (启动期断言守门)。
    """

    STATE = "STATE"
    TEXT = "TEXT"
    INTERRUPT = "INTERRUPT"
    TURN_SEALED = "TURN_SEALED"
    TOPIC = "TOPIC"
    BYE = "BYE"
    HEARTBEAT = "HEARTBEAT"
    ERROR = "ERROR"


class InboundKind(str, enum.Enum):
    """
    客户端 → 服务端发送的控制指令类型。

    与 backend/api/control.py 路由动作对齐。
    """

    PING = "PING"
    RESUME_SESSION = "RESUME_SESSION"
    PAUSE_SESSION = "PAUSE_SESSION"
    INJECT_TOPIC = "INJECT_TOPIC"
    POP_TOPIC = "POP_TOPIC"
    FORCE_INTERRUPT = "FORCE_INTERRUPT"


# ──────────────────────────────────────────────────────────────────────────────
# 启动期字面量对齐断言
# ──────────────────────────────────────────────────────────────────────────────


def _assert_packet_kind_alignment() -> None:
    """
    校验本模块 OutboundKind 与 core.throttle.PacketKind 字面量严格一致。

    在模块导入末尾执行,任何漂移都会让进程启动失败 (fail-fast)。
    schemas 在运行期路径绝不依赖 core,此断言只在导入时刻执行一次。
    """
    from backend.core.throttle import PacketKind as _CorePacketKind

    core_values = {k.value for k in _CorePacketKind}
    schema_values = {k.value for k in OutboundKind}

    # core.throttle.PacketKind 当前 7 种 (无 ERROR);schemas 多一个 ERROR
    # 因此要求: core ⊆ schemas
    missing_in_schema = core_values - schema_values
    if missing_in_schema:
        raise RuntimeError(
            f"[stream_packet] 字面量漂移 · core 中存在但 schemas 缺失: "
            f"{sorted(missing_in_schema)}"
        )

    # 反向: schemas 多出的 kind 必须是已知扩展 (例: ERROR)
    extra_in_schema = schema_values - core_values
    allowed_extras: set[str] = {"ERROR"}
    unknown_extras = extra_in_schema - allowed_extras
    if unknown_extras:
        raise RuntimeError(
            f"[stream_packet] schemas 多出未声明扩展 kind: "
            f"{sorted(unknown_extras)}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 出站载荷模型 (Outbound Payloads)
# ──────────────────────────────────────────────────────────────────────────────


class StatePayload(_StrictModel):
    """
    STATE 包载荷 · 10Hz 合并状态快照。

    字段全集来自 core.throttle.StateBundle,但通过 schemas 强类型化。
    """

    sequence_id: int = Field(..., ge=0, description="单调递增序号")
    at_monotonic: float = Field(..., ge=0.0)
    at_wall_unix: float = Field(..., gt=0.0)

    fsm: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description="agent_id → FSM 快照字典",
    )
    aggro: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description="agent_id → Aggro 快照字典",
    )
    arbitrator: dict[str, object] = Field(
        default_factory=dict,
        description="仲裁器快照",
    )
    watchdog: dict[str, object] = Field(
        default_factory=dict,
        description="看门狗快照",
    )
    semaphore: dict[str, object] = Field(
        default_factory=dict,
        description="信号量快照",
    )

    # ⚠️ 注意: 此处用 dict 而非具体子模型,是为了与 core.throttle 现有
    # snapshot_dict() 输出零拷贝对齐。如需强类型化,可在路由层显式构造
    # AggregatedStatePayload (见下)。


class AggregatedStatePayload(_StrictModel):
    """
    强类型版 STATE 载荷 · 用于 REST /api/sandbox/snapshot。

    与 StatePayload 的区别:
        - StatePayload 直接包 throttle 的 dict 输出 (高性能, 弱类型)
        - AggregatedStatePayload 完全模型化 (慢, 但供前端 narrow)
    """

    sequence_id: int = Field(..., ge=0)
    at_wall_unix: float = Field(..., gt=0.0)
    agents: list[AgentStateModel] = Field(
        default_factory=list,
        description="8 个 Agent 完整运行时状态",
    )
    arbitrator: ArbitratorMetricsModel = Field(...)
    watchdog: WatchdogMetricsModel = Field(...)
    semaphore: LLMSemaphoreMetricsModel = Field(...)
    current_topic_title: Optional[str] = Field(
        None, max_length=128
    )
    topic_stack_depth: int = Field(..., ge=0)


class TextPayload(_StrictModel):
    """TEXT 包 · 流式 token chunk 实时旁路。"""

    agent_id: Optional[str] = Field(
        None,
        max_length=64,
        description="发言 Agent ID (None = 系统消息)",
    )
    text: str = Field(
        ...,
        min_length=0,
        max_length=8192,
        description="本次 chunk 的纯文本内容",
    )

    @field_validator("text")
    @classmethod
    def _allow_empty_only_with_agent(cls, v: str) -> str:
        # 允许空字符串 (例如收尾刷新),但不允许超长
        return v


class InterruptPayload(_StrictModel):
    """INTERRUPT 包 · 触发前端 Shake 动效。"""

    interrupter_agent_id: str = Field(..., min_length=1, max_length=64)
    interrupted_agent_id: Optional[str] = Field(
        None,
        max_length=64,
        description="被打断者 (可能为 None,例如真空期补位非抢话)",
    )
    at_wall_unix: float = Field(..., gt=0.0)


class TurnSealedTurnSummary(_StrictModel):
    """TURN_SEALED 包内嵌的 Turn 摘要 (避免重复定义大对象)。"""

    turn_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1, max_length=64)
    topic_id: str = Field(..., min_length=1)
    text: str = Field(..., max_length=16384)
    truncation: Literal[
        "NORMAL", "INTERRUPTED", "WATCHDOG_TIMEOUT", "SYSTEM_ABORTED"
    ] = Field(...)
    interrupter_agent_id: Optional[str] = Field(None, max_length=64)
    duration_seconds: float = Field(..., ge=0.0)
    sealed_at_wall_unix: float = Field(..., gt=0.0)
    is_truncated: bool = Field(...)


class TurnSealedPayload(_StrictModel):
    """TURN_SEALED 包 · Turn 封存通知。"""

    turn: TurnSealedTurnSummary = Field(...)
    keyword_hits: dict[str, list[str]] = Field(
        default_factory=dict,
        description="agent_id → 命中的关键词列表",
    )
    named_targets: list[str] = Field(
        default_factory=list,
        description="发言中点名的目标 Agent ID 列表",
    )


class TopicPayload(_StrictModel):
    """TOPIC 包 · 议题栈变化通知。"""

    operation: Literal["push", "pop"] = Field(...)
    topic: dict[str, object] = Field(
        ...,
        description="Topic 字典 (与 ContextBus.Topic.to_dict 对齐)",
    )
    stack_depth: int = Field(..., ge=0)


class ByeReason(str, enum.Enum):
    """BYE 包终止原因。"""

    SERVER_SHUTDOWN = "server_shutdown"
    SESSION_ENDED = "session_ended"
    PROTOCOL_VIOLATION = "protocol_violation"


class ByePayload(_StrictModel):
    reason: ByeReason = Field(...)
    detail: str = Field("", max_length=512)


class HeartbeatPayload(_StrictModel):
    at_wall_unix: float = Field(..., gt=0.0)


class ErrorSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class ErrorPayload(_StrictModel):
    severity: ErrorSeverity = Field(...)
    code: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., max_length=512)
    request_id: Optional[str] = Field(None, max_length=64)


# ──────────────────────────────────────────────────────────────────────────────
# 出站包封装 (Discriminated Union)
# ──────────────────────────────────────────────────────────────────────────────


class _OutboundEnvelopeBase(BaseModel):
    """
    出站包统一信封基类。

    使用非 frozen 子类是因 Pydantic 的 Discriminated Union 在 frozen 类上
    存在边角行为 (尤其与 model_validate 配合时);本层接受可变,但下游
    序列化后即冻结 (网络传输不可变)。
    """

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    server_at_wall_unix: float = Field(
        ...,
        gt=0.0,
        description="服务端发出该包的 Unix 时间戳",
    )


class OutboundStatePacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.STATE] = OutboundKind.STATE
    payload: StatePayload = Field(...)


class OutboundTextPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.TEXT] = OutboundKind.TEXT
    payload: TextPayload = Field(...)


class OutboundInterruptPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.INTERRUPT] = OutboundKind.INTERRUPT
    payload: InterruptPayload = Field(...)


class OutboundTurnSealedPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.TURN_SEALED] = OutboundKind.TURN_SEALED
    payload: TurnSealedPayload = Field(...)


class OutboundTopicPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.TOPIC] = OutboundKind.TOPIC
    payload: TopicPayload = Field(...)


class OutboundByePacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.BYE] = OutboundKind.BYE
    payload: ByePayload = Field(...)


class OutboundHeartbeatPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.HEARTBEAT] = OutboundKind.HEARTBEAT
    payload: HeartbeatPayload = Field(...)


class OutboundErrorPacket(_OutboundEnvelopeBase):
    kind: Literal[OutboundKind.ERROR] = OutboundKind.ERROR
    payload: ErrorPayload = Field(...)


#: Tagged Union: kind 字段作为判别式,Pydantic 自动分发
OutboundPacket = Annotated[
    Union[
        OutboundStatePacket,
        OutboundTextPacket,
        OutboundInterruptPacket,
        OutboundTurnSealedPacket,
        OutboundTopicPacket,
        OutboundByePacket,
        OutboundHeartbeatPacket,
        OutboundErrorPacket,
    ],
    Field(discriminator="kind"),
]

#: TypeAdapter 缓存 (Pydantic v2 推荐: Discriminated Union 通过 TypeAdapter 验证)
_outbound_adapter: TypeAdapter[OutboundPacket] = TypeAdapter(OutboundPacket)


# ──────────────────────────────────────────────────────────────────────────────
# 入站载荷模型
# ──────────────────────────────────────────────────────────────────────────────


class PingPayload(_StrictModel):
    client_at_wall_unix: float = Field(..., gt=0.0)


class ResumeSessionPayload(_StrictModel):
    """空载荷 (用占位字段防止 Pydantic 接受任意 dict)。"""

    confirm: bool = Field(
        True,
        description="占位确认字段 · 必须为 true",
    )

    @field_validator("confirm")
    @classmethod
    def _must_true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("RESUME_SESSION 的 confirm 字段必须为 true")
        return v


class PauseSessionPayload(_StrictModel):
    confirm: bool = Field(True)

    @field_validator("confirm")
    @classmethod
    def _must_true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("PAUSE_SESSION 的 confirm 字段必须为 true")
        return v


class InjectTopicPayload(_StrictModel):
    title: str = Field(..., min_length=1, max_length=128)
    description: str = Field("", max_length=1024)


class PopTopicPayload(_StrictModel):
    confirm: bool = Field(True)

    @field_validator("confirm")
    @classmethod
    def _must_true(cls, v: bool) -> bool:
        if not v:
            raise ValueError("POP_TOPIC 的 confirm 字段必须为 true")
        return v


class ForceInterruptPayload(_StrictModel):
    """运维/调试用 · 强制中断当前持麦 Agent (跳过 Aggro 规则)。"""

    reason: str = Field(..., min_length=1, max_length=128)


# ──────────────────────────────────────────────────────────────────────────────
# 入站包封装
# ──────────────────────────────────────────────────────────────────────────────


class _InboundEnvelopeBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    client_at_wall_unix: Optional[float] = Field(
        None,
        gt=0.0,
        description="(可选) 客户端发出该包的时间戳,用于诊断时延",
    )


class InboundPingPacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.PING] = InboundKind.PING
    payload: PingPayload = Field(...)


class InboundResumePacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.RESUME_SESSION] = InboundKind.RESUME_SESSION
    payload: ResumeSessionPayload = Field(default_factory=ResumeSessionPayload)


class InboundPausePacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.PAUSE_SESSION] = InboundKind.PAUSE_SESSION
    payload: PauseSessionPayload = Field(default_factory=PauseSessionPayload)


class InboundInjectTopicPacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.INJECT_TOPIC] = InboundKind.INJECT_TOPIC
    payload: InjectTopicPayload = Field(...)


class InboundPopTopicPacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.POP_TOPIC] = InboundKind.POP_TOPIC
    payload: PopTopicPayload = Field(default_factory=PopTopicPayload)


class InboundForceInterruptPacket(_InboundEnvelopeBase):
    kind: Literal[InboundKind.FORCE_INTERRUPT] = InboundKind.FORCE_INTERRUPT
    payload: ForceInterruptPayload = Field(...)


InboundPacket = Annotated[
    Union[
        InboundPingPacket,
        InboundResumePacket,
        InboundPausePacket,
        InboundInjectTopicPacket,
        InboundPopTopicPacket,
        InboundForceInterruptPacket,
    ],
    Field(discriminator="kind"),
]

_inbound_adapter: TypeAdapter[InboundPacket] = TypeAdapter(InboundPacket)


# ──────────────────────────────────────────────────────────────────────────────
# 解析异常
# ──────────────────────────────────────────────────────────────────────────────


class PacketParseError(Exception):
    """入站包解析失败基类。"""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        raw_preview: str = "",
    ) -> None:
        super().__init__(message)
        self.code: Final[str] = code
        self.raw_preview: Final[str] = raw_preview


class PacketDecodeError(PacketParseError):
    """JSON 解析失败 / 不是有效 UTF-8。"""


class PacketSchemaError(PacketParseError):
    """JSON 合法但不满足 schema (字段缺失 / 类型错误 / 未知 kind)。"""


# ──────────────────────────────────────────────────────────────────────────────
# 解析与序列化工具
# ──────────────────────────────────────────────────────────────────────────────


_MAX_INBOUND_BYTES: Final[int] = 64 * 1024
"""单帧入站包最大字节数 (与 settings.WS_MAX_MESSAGE_BYTES 匹配,本处硬上限兜底)。"""


def parse_inbound_packet(raw: bytes | str) -> InboundPacket:
    """
    统一入站包解析入口。

    Args:
        raw: WebSocket 收到的原始 bytes 或 str

    Returns:
        InboundPacket (具体子类型由 kind 字段决定)

    Raises:
        PacketDecodeError: JSON 解析失败 / UTF-8 错误
        PacketSchemaError: schema 校验失败 / 未知 kind
    """
    # 大小守门
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8", errors="replace")
    else:
        raw_bytes = raw

    if len(raw_bytes) > _MAX_INBOUND_BYTES:
        raise PacketDecodeError(
            f"入站包过大 ({len(raw_bytes)} > {_MAX_INBOUND_BYTES} bytes)",
            code="PAYLOAD_TOO_LARGE",
            raw_preview=raw_bytes[:128].decode("utf-8", errors="replace"),
        )

    # JSON 解码
    try:
        obj = orjson.loads(raw_bytes)
    except orjson.JSONDecodeError as exc:
        raise PacketDecodeError(
            f"JSON 解析失败: {exc}",
            code="INVALID_JSON",
            raw_preview=raw_bytes[:128].decode("utf-8", errors="replace"),
        ) from exc

    if not isinstance(obj, dict):
        raise PacketSchemaError(
            f"入站包根级必须为 object,实际为 {type(obj).__name__}",
            code="ROOT_NOT_OBJECT",
            raw_preview=str(obj)[:128],
        )

    # Discriminated Union 校验
    try:
        return _inbound_adapter.validate_python(obj)
    except ValidationError as exc:
        # 未知 kind 是高频错误,单独提取
        kind_value = obj.get("kind") if isinstance(obj, dict) else None
        code = (
            "UNKNOWN_KIND"
            if kind_value is None or kind_value not in {k.value for k in InboundKind}
            else "SCHEMA_INVALID"
        )
        raise PacketSchemaError(
            f"schema 校验失败: {exc.errors()[:3]}",  # 仅前 3 条,防日志爆炸
            code=code,
            raw_preview=orjson.dumps(obj)[:256].decode("utf-8", errors="replace"),
        ) from exc


def serialize_outbound_packet(packet: OutboundPacket) -> bytes:
    """
    将出站包序列化为 bytes。

    优先使用 orjson (零拷贝高性能);失败兜底为 ERROR 包。
    """
    try:
        # Pydantic v2 model_dump(mode='json') → 普通 dict → orjson 高速序列化
        return orjson.dumps(packet.model_dump(mode="json"))
    except (TypeError, orjson.JSONEncodeError, ValueError) as exc:
        # 兜底: 构造一个 ERROR 包,保证下游一定能收到合法 JSON
        fallback = make_error_packet(
            severity=ErrorSeverity.CRITICAL,
            code="SERIALIZATION_FAILED",
            message=f"出站包序列化失败: {exc}",
        )
        try:
            return orjson.dumps(fallback.model_dump(mode="json"))
        except Exception:
            # 极端兜底: 最小合法 JSON
            return b'{"kind":"ERROR","payload":{"severity":"CRITICAL","code":"FATAL_SERIALIZE","message":"unrecoverable"},"server_at_wall_unix":0.0}'


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数 (零样板构造)
# ──────────────────────────────────────────────────────────────────────────────


def _now() -> float:
    return round(time.time(), 6)


def make_state_packet(
    *,
    sequence_id: int,
    fsm: dict[str, dict[str, object]],
    aggro: dict[str, dict[str, object]],
    arbitrator: dict[str, object],
    watchdog: dict[str, object],
    semaphore: dict[str, object],
    at_monotonic: float,
) -> OutboundStatePacket:
    return OutboundStatePacket(
        payload=StatePayload(
            sequence_id=sequence_id,
            at_monotonic=at_monotonic,
            at_wall_unix=_now(),
            fsm=fsm,
            aggro=aggro,
            arbitrator=arbitrator,
            watchdog=watchdog,
            semaphore=semaphore,
        ),
        server_at_wall_unix=_now(),
    )


def make_text_packet(
    text: str,
    agent_id: Optional[str] = None,
) -> OutboundTextPacket:
    return OutboundTextPacket(
        payload=TextPayload(agent_id=agent_id, text=text),
        server_at_wall_unix=_now(),
    )


def make_interrupt_packet(
    interrupter_agent_id: str,
    interrupted_agent_id: Optional[str] = None,
) -> OutboundInterruptPacket:
    return OutboundInterruptPacket(
        payload=InterruptPayload(
            interrupter_agent_id=interrupter_agent_id,
            interrupted_agent_id=interrupted_agent_id,
            at_wall_unix=_now(),
        ),
        server_at_wall_unix=_now(),
    )


def make_turn_sealed_packet(
    *,
    turn_summary: TurnSealedTurnSummary,
    keyword_hits: Optional[dict[str, list[str]]] = None,
    named_targets: Optional[list[str]] = None,
) -> OutboundTurnSealedPacket:
    return OutboundTurnSealedPacket(
        payload=TurnSealedPayload(
            turn=turn_summary,
            keyword_hits=keyword_hits or {},
            named_targets=named_targets or [],
        ),
        server_at_wall_unix=_now(),
    )


def make_topic_packet(
    *,
    operation: Literal["push", "pop"],
    topic: dict[str, object],
    stack_depth: int,
) -> OutboundTopicPacket:
    return OutboundTopicPacket(
        payload=TopicPayload(
            operation=operation,
            topic=topic,
            stack_depth=stack_depth,
        ),
        server_at_wall_unix=_now(),
    )


def make_bye_packet(
    reason: ByeReason = ByeReason.SERVER_SHUTDOWN,
    detail: str = "",
) -> OutboundByePacket:
    return OutboundByePacket(
        payload=ByePayload(reason=reason, detail=detail),
        server_at_wall_unix=_now(),
    )


def make_heartbeat_packet() -> OutboundHeartbeatPacket:
    return OutboundHeartbeatPacket(
        payload=HeartbeatPayload(at_wall_unix=_now()),
        server_at_wall_unix=_now(),
    )


def make_error_packet(
    *,
    severity: ErrorSeverity,
    code: str,
    message: str,
    request_id: Optional[str] = None,
) -> OutboundErrorPacket:
    return OutboundErrorPacket(
        payload=ErrorPayload(
            severity=severity,
            code=code,
            message=message,
            request_id=request_id,
        ),
        server_at_wall_unix=_now(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块加载期断言 (字面量对齐)
# ──────────────────────────────────────────────────────────────────────────────

# ⚠️ 此处必须在所有定义之后执行 · 任何漂移都让进程启动失败
_assert_packet_kind_alignment()


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # Kind 枚举
    "OutboundKind",
    "InboundKind",
    # 出站载荷
    "StatePayload",
    "AggregatedStatePayload",
    "TextPayload",
    "InterruptPayload",
    "TurnSealedTurnSummary",
    "TurnSealedPayload",
    "TopicPayload",
    "ByeReason",
    "ByePayload",
    "HeartbeatPayload",
    "ErrorSeverity",
    "ErrorPayload",
    # 出站包封装
    "OutboundStatePacket",
    "OutboundTextPacket",
    "OutboundInterruptPacket",
    "OutboundTurnSealedPacket",
    "OutboundTopicPacket",
    "OutboundByePacket",
    "OutboundHeartbeatPacket",
    "OutboundErrorPacket",
    "OutboundPacket",
    # 入站载荷
    "PingPayload",
    "ResumeSessionPayload",
    "PauseSessionPayload",
    "InjectTopicPayload",
    "PopTopicPayload",
    "ForceInterruptPayload",
    # 入站包封装
    "InboundPingPacket",
    "InboundResumePacket",
    "InboundPausePacket",
    "InboundInjectTopicPacket",
    "InboundPopTopicPacket",
    "InboundForceInterruptPacket",
    "InboundPacket",
    # 异常
    "PacketParseError",
    "PacketDecodeError",
    "PacketSchemaError",
    # 工具
    "parse_inbound_packet",
    "serialize_outbound_packet",
    # 工厂函数
    "make_state_packet",
    "make_text_packet",
    "make_interrupt_packet",
    "make_turn_sealed_packet",
    "make_topic_packet",
    "make_bye_packet",
    "make_heartbeat_packet",
    "make_error_packet",
]