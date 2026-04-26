"""
==============================================================================
backend/schemas/agent_state.py
------------------------------------------------------------------------------
数据契约层 · Agent 运行时状态模型 (Pydantic v2)

定位:
    后端 → 前端的"协议宪法"。
    把 core/*.py 产出的内部状态 (FSMSnapshot / AggroSnapshot / ...) 用
    Pydantic v2 模型固化为对外契约,作为:
        1. FastAPI 路由 response_model
        2. WebSocket 包载荷的运行期校验
        3. 与前端 protocol.d.ts 字段一一对齐的语义锚点

设计原则:
    1. 零业务逻辑 - 仅"形状",任何计算/转换禁止下沉至此
    2. 单向依赖 - schemas 依赖 core,core 严禁依赖 schemas (防环)
    3. 命名严格 snake_case - 与前端 TS 字段对齐
    4. 不可变默认值 - 一律使用 Field(default_factory=...)
    5. 提供 from_internal() 静态方法 - 集中维护"内部模型 → 对外契约"映射

==============================================================================
"""

from __future__ import annotations

import enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from backend.core.aggro_engine import AggroSnapshot
from backend.core.arbitrator import (
    ArbitratorMetrics,
    MicrophoneToken,
    RequestKind,
)
from backend.core.fsm import FSMSnapshot, FSMState
from backend.core.semaphore import SemaphoreMetrics
from backend.core.watchdog import WatchdogMetrics


# ──────────────────────────────────────────────────────────────────────────────
# 模型基类 (统一配置)
# ──────────────────────────────────────────────────────────────────────────────


class _StrictModel(BaseModel):
    """
    所有 schemas 模型的基类。

    配置项:
        - extra="forbid"      多余字段直接拒绝 (协议清洁性)
        - frozen=True          序列化后不可变 (杜绝下游误改)
        - populate_by_name     允许通过别名构造
        - str_strip_whitespace 自动 trim 字符串字段
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 通用枚举 (与前端 TS 字面量对齐)
# ──────────────────────────────────────────────────────────────────────────────


class MBTIType(str, enum.Enum):
    """
    8 个 MBTI 人格代号 · 严格大写,与 agents/*.py 的 agent_id 前缀对齐。

    与前端 colorMap.ts 的人格 → 阵营映射保持一致。
    """

    INTJ = "INTJ"
    ENTP = "ENTP"
    INFP = "INFP"
    ENFP = "ENFP"
    ISTJ = "ISTJ"
    ISFJ = "ISFJ"
    ESTP = "ESTP"
    ISFP = "ISFP"


class Faction(str, enum.Enum):
    """
    四大阵营 · 与前端语义色板严格对齐:
        NT 理性组 → 青色 cyan
        NF 情感组 → 洋红 fuchsia
        SJ 秩序组 → 琥珀 amber
        SP 灵活组 → 翠绿 emerald
    """

    NT = "NT"
    NF = "NF"
    SJ = "SJ"
    SP = "SP"


#: MBTI 类型 → 阵营映射 (单一可信源,前端通过此约定推导色板)
MBTI_FACTION_MAP: dict[MBTIType, Faction] = {
    MBTIType.INTJ: Faction.NT,
    MBTIType.ENTP: Faction.NT,
    MBTIType.INFP: Faction.NF,
    MBTIType.ENFP: Faction.NF,
    MBTIType.ISTJ: Faction.SJ,
    MBTIType.ISFJ: Faction.SJ,
    MBTIType.ESTP: Faction.SP,
    MBTIType.ISFP: Faction.SP,
}


def faction_of(mbti: MBTIType) -> Faction:
    """工具函数: 由 MBTI 类型获取所属阵营。"""
    return MBTI_FACTION_MAP[mbti]


# ──────────────────────────────────────────────────────────────────────────────
# FSM 状态镜像
# ──────────────────────────────────────────────────────────────────────────────


class FSMStateModel(str, enum.Enum):
    """
    FSM 6 态枚举的契约镜像 · 与 core.fsm.FSMState 字面量一致。

    单独定义而非复用 core 枚举,是为了:
        1. 隔离契约层与内部实现 (内部加新态时不污染对外契约)
        2. 让前端 TS 与本枚举形成 1:1 字面量对齐
    """

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    RETRIEVING = "RETRIEVING"
    COMPUTING = "COMPUTING"
    SPEAKING = "SPEAKING"
    UPDATING = "UPDATING"

    @staticmethod
    def from_core(state: FSMState) -> "FSMStateModel":
        """从 core 枚举转换 (字面量同名,直接通过 value 构造)。"""
        return FSMStateModel(state.value)


# ──────────────────────────────────────────────────────────────────────────────
# 单 Agent FSM 状态
# ──────────────────────────────────────────────────────────────────────────────


class AgentFSMStateModel(_StrictModel):
    """单 Agent 的 FSM 瞬时快照。"""

    agent_id: str = Field(
        ...,
        description="Agent 唯一标识 (例: INTJ_logician)",
        min_length=1,
        max_length=64,
    )
    state: FSMStateModel = Field(..., description="当前 FSM 状态")
    dwell_seconds: float = Field(
        ...,
        ge=0.0,
        description="当前状态已停留秒数",
    )
    transitions_total: int = Field(
        ...,
        ge=0,
        description="累计成功跃迁次数 (不含 no-op)",
    )
    illegal_attempts_total: int = Field(
        ...,
        ge=0,
        description="累计非法跃迁尝试次数 (诊断指标)",
    )

    @staticmethod
    def from_internal(snap: FSMSnapshot) -> "AgentFSMStateModel":
        return AgentFSMStateModel(
            agent_id=snap.agent_id,
            state=FSMStateModel.from_core(snap.state),
            dwell_seconds=round(snap.dwell_seconds, 3),
            transitions_total=snap.transitions_total,
            illegal_attempts_total=snap.illegal_attempts_total,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 单 Agent Aggro 状态
# ──────────────────────────────────────────────────────────────────────────────


class AggroBucketModel(_StrictModel):
    """单 Agent 的 Aggro 完整快照。"""

    agent_id: str = Field(
        ...,
        description="Agent 唯一标识",
        min_length=1,
        max_length=64,
    )
    current: float = Field(..., description="当前 Aggro 值 (∈ [min, max])")
    peak: float = Field(
        ...,
        description="历史峰值 (单调递增)",
    )
    decay_lambda: float = Field(
        ...,
        gt=0.0,
        description="衰减系数 λ (单位: 1/秒)",
    )
    last_decay_seconds: float = Field(
        ...,
        ge=0.0,
        description="最近一次更新与上次的时间间隔 Δt (秒)",
    )
    updates_total: int = Field(
        ..., ge=0, description="累计更新次数 (含纯衰减)"
    )
    clamped_total: int = Field(
        ...,
        ge=0,
        description="累计触发 clamp 截断次数 (越界保护命中)",
    )

    # ─── 派生指标 (前端无需自行计算) ───
    normalized: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="归一化值 (current - min) / (max - min) · 供进度条直接绑定",
    )
    above_interrupt_threshold: bool = Field(
        ...,
        description="当前是否 ≥ 抢话阈值 · 供 HUD 触发脉冲动效",
    )

    @staticmethod
    def from_internal(
        snap: AggroSnapshot,
        *,
        aggro_min: float,
        aggro_max: float,
        interrupt_threshold: float,
    ) -> "AggroBucketModel":
        """
        从 core AggroSnapshot 构造 · 需注入边界参数完成派生计算。

        派生计算放在此处而非业务层,是因为:
            1. 模型字段必须自洽 (前端不重算)
            2. 边界参数来自 settings,集中转换避免散落
        """
        span = max(1e-9, aggro_max - aggro_min)
        normalized = (snap.current - aggro_min) / span
        # 防御浮点误差 (clamp 到 [0, 1])
        normalized = max(0.0, min(1.0, normalized))

        return AggroBucketModel(
            agent_id=snap.agent_id,
            current=round(snap.current, 3),
            peak=round(snap.peak, 3),
            decay_lambda=round(snap.decay_lambda, 6),
            last_decay_seconds=round(snap.last_decay_seconds, 4),
            updates_total=snap.updates_total,
            clamped_total=snap.clamped_total,
            normalized=round(normalized, 4),
            above_interrupt_threshold=(
                snap.current >= interrupt_threshold
            ),
        )


# ──────────────────────────────────────────────────────────────────────────────
# 仲裁器: 申请类型 / 麦克风令牌 / 指标
# ──────────────────────────────────────────────────────────────────────────────


class RequestKindModel(str, enum.Enum):
    """麦克风申请来源类型 (与 core.arbitrator.RequestKind 镜像)。"""

    AGGRO_THRESHOLD = "AGGRO_THRESHOLD"
    INTERRUPT = "INTERRUPT"
    VACUUM_FILL = "VACUUM_FILL"
    SCHEDULED = "SCHEDULED"

    @staticmethod
    def from_core(kind: RequestKind) -> "RequestKindModel":
        return RequestKindModel(kind.value)


class MicrophoneTokenModel(_StrictModel):
    """当前麦克风持有令牌的对外契约 (interrupt_event 不暴露)。"""

    epoch: int = Field(..., ge=1, description="单调递增编号")
    holder_agent_id: str = Field(..., min_length=1, max_length=64)
    granted_at_wall_unix: float = Field(
        ...,
        gt=0.0,
        description="授权时间 (Unix 时间戳)",
    )
    request_kind: RequestKindModel = Field(...)
    priority_score: float = Field(...)
    interrupt_pending: bool = Field(
        ...,
        description="是否已有抢话信号在等待 (event.is_set)",
    )

    @staticmethod
    def from_internal(token: MicrophoneToken) -> "MicrophoneTokenModel":
        return MicrophoneTokenModel(
            epoch=token.epoch,
            holder_agent_id=token.holder_agent_id,
            granted_at_wall_unix=round(token.granted_at_wall_unix, 6),
            request_kind=RequestKindModel.from_core(token.request_kind),
            priority_score=round(token.priority_score, 4),
            interrupt_pending=token.interrupt_event.is_set(),
        )


class ArbitratorMetricsModel(_StrictModel):
    """仲裁器全局指标。"""

    current_holder_id: Optional[str] = Field(
        None,
        description="当前麦克风持有者 (None = 空闲)",
        max_length=64,
    )
    current_epoch: int = Field(..., ge=0)
    current_token_age_sec: float = Field(..., ge=0.0)
    granted_total: int = Field(..., ge=0)
    interrupt_granted_total: int = Field(..., ge=0)
    rejected_total: int = Field(..., ge=0)
    debounce_drop_total: int = Field(..., ge=0)
    last_speaker_id: Optional[str] = Field(None, max_length=64)
    cooldown_remaining_sec: float = Field(..., ge=0.0)
    seconds_since_any_speech: float = Field(..., ge=0.0)

    @staticmethod
    def from_internal(m: ArbitratorMetrics) -> "ArbitratorMetricsModel":
        return ArbitratorMetricsModel(
            current_holder_id=m.current_holder_id,
            current_epoch=m.current_epoch,
            current_token_age_sec=round(m.current_token_age_sec, 3),
            granted_total=m.granted_total,
            interrupt_granted_total=m.interrupt_granted_total,
            rejected_total=m.rejected_total,
            debounce_drop_total=m.debounce_drop_total,
            last_speaker_id=m.last_speaker_id,
            cooldown_remaining_sec=round(m.cooldown_remaining_sec, 3),
            seconds_since_any_speech=round(m.seconds_since_any_speech, 3),
        )


# ──────────────────────────────────────────────────────────────────────────────
# 信号量指标
# ──────────────────────────────────────────────────────────────────────────────


class LLMSemaphoreMetricsModel(_StrictModel):
    """LLMSemaphore 算力闸门指标。"""

    capacity: int = Field(..., ge=1, le=3, description="并发上限")
    held_count: int = Field(..., ge=0, description="当前持有数")
    waiting_count: int = Field(..., ge=0, description="当前等待数")
    acquired_total: int = Field(..., ge=0)
    released_total: int = Field(..., ge=0)
    timeout_total: int = Field(..., ge=0)
    cancelled_total: int = Field(..., ge=0)
    last_acquire_wait_ms: float = Field(..., ge=0.0)
    holders: list[str] = Field(
        default_factory=list,
        description="当前持有者 Agent ID 列表 (按获取顺序)",
    )

    @staticmethod
    def from_internal(
        m: SemaphoreMetrics,
    ) -> "LLMSemaphoreMetricsModel":
        return LLMSemaphoreMetricsModel(
            capacity=m.capacity,
            held_count=m.held_count,
            waiting_count=m.waiting_count,
            acquired_total=m.acquired_total,
            released_total=m.released_total,
            timeout_total=m.timeout_total,
            cancelled_total=m.cancelled_total,
            last_acquire_wait_ms=round(m.last_acquire_wait_ms, 3),
            holders=list(m.holders),
        )


# ──────────────────────────────────────────────────────────────────────────────
# 看门狗指标
# ──────────────────────────────────────────────────────────────────────────────


class WatchdogEvictionModel(_StrictModel):
    """单次驱逐事件的对外契约 (用于 watchdog.last_eviction)。"""

    entry_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1, max_length=64)
    severity: str = Field(..., pattern=r"^(soft|hard)$")
    reason: str = Field(..., max_length=256)
    at_wall_unix: float = Field(..., gt=0.0)
    overdue_seconds: float = Field(..., ge=0.0)


class WatchdogMetricsModel(_StrictModel):
    """看门狗指标。"""

    is_running: bool = Field(...)
    in_flight_count: int = Field(..., ge=0)
    soft_evict_total: int = Field(..., ge=0)
    hard_evict_total: int = Field(..., ge=0)
    cancelled_total: int = Field(..., ge=0)
    last_eviction: Optional[WatchdogEvictionModel] = Field(
        None,
        description="最近一次驱逐记录 (None = 启动后无任何驱逐)",
    )

    @staticmethod
    def from_internal(m: WatchdogMetrics) -> "WatchdogMetricsModel":
        last_evict_model: Optional[WatchdogEvictionModel] = None
        if m.last_eviction is not None:
            try:
                last_evict_model = WatchdogEvictionModel.model_validate(
                    m.last_eviction
                )
            except Exception:
                # 兜底: 元数据不规范时忽略
                last_evict_model = None

        return WatchdogMetricsModel(
            is_running=m.is_running,
            in_flight_count=m.in_flight_count,
            soft_evict_total=m.soft_evict_total,
            hard_evict_total=m.hard_evict_total,
            cancelled_total=m.cancelled_total,
            last_eviction=last_evict_model,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Agent 静态描述 (HUD 卡片渲染用)
# ──────────────────────────────────────────────────────────────────────────────


class AgentDescriptorModel(_StrictModel):
    """
    Agent 静态描述 · 由 agents/*.py 在 register 时填充,运行期不变。

    供 HUD `AgentCard.vue` 渲染卡片元信息 (头像光晕色、人格特征标签等)。
    """

    agent_id: str = Field(..., min_length=1, max_length=64)
    mbti: MBTIType = Field(...)
    faction: Faction = Field(...)
    display_name: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="HUD 卡片显示的中文人格名 (例: 逻辑硬核 INTJ)",
    )
    archetype: str = Field(
        ...,
        max_length=64,
        description="人格原型 (例: 逻辑硬核型)",
    )
    behavior_anchor: str = Field(
        ...,
        max_length=512,
        description="MBTI 核心行为锚点 (强心剂注入文本预览)",
    )
    decay_lambda: float = Field(
        ...,
        gt=0.0,
        description="个性化 λ (单位: 1/秒)",
    )
    has_chroma_access: bool = Field(
        False,
        description="是否拥有 ChromaDB 检索权 (仅 ISFJ_archivist=True)",
    )
    is_summarizer: bool = Field(
        False,
        description="是否承担后台 Summarizer 职责 (仅 ESTP_conductor=True)",
    )


# ──────────────────────────────────────────────────────────────────────────────
# 单 Agent 完整运行时状态聚合
# ──────────────────────────────────────────────────────────────────────────────


class AgentStateModel(_StrictModel):
    """
    单 Agent 的"卡片级"完整状态 · HUD 监控舱单卡所需字段全集。

    聚合:
        - descriptor    静态人格描述
        - fsm           当前 FSM 状态
        - aggro         当前 Aggro 桶
        - is_holder     是否当前持麦
        - is_interrupted_pending 是否已有抢话信号待处理
    """

    descriptor: AgentDescriptorModel = Field(...)
    fsm: AgentFSMStateModel = Field(...)
    aggro: AggroBucketModel = Field(...)

    is_holder: bool = Field(
        False,
        description="是否当前持有麦克风令牌",
    )
    is_interrupt_pending: bool = Field(
        False,
        description="持麦时是否已有抢话信号待处理 (前端可叠加红色边框)",
    )

    @staticmethod
    def assemble(
        *,
        descriptor: AgentDescriptorModel,
        fsm_snap: FSMSnapshot,
        aggro_snap: AggroSnapshot,
        aggro_min: float,
        aggro_max: float,
        interrupt_threshold: float,
        current_token: Optional[MicrophoneToken],
    ) -> "AgentStateModel":
        """
        装配工厂方法 · 集中处理"内部快照 → 对外卡片"映射。

        is_holder / is_interrupt_pending 由 current_token 推导,
        前端无需自行计算。
        """
        is_holder = (
            current_token is not None
            and current_token.holder_agent_id == descriptor.agent_id
        )
        is_intr_pending = (
            is_holder
            and current_token is not None
            and current_token.interrupt_event.is_set()
        )

        return AgentStateModel(
            descriptor=descriptor,
            fsm=AgentFSMStateModel.from_internal(fsm_snap),
            aggro=AggroBucketModel.from_internal(
                aggro_snap,
                aggro_min=aggro_min,
                aggro_max=aggro_max,
                interrupt_threshold=interrupt_threshold,
            ),
            is_holder=is_holder,
            is_interrupt_pending=is_intr_pending,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 基类
    "_StrictModel",
    # 通用枚举
    "MBTIType",
    "Faction",
    "MBTI_FACTION_MAP",
    "faction_of",
    # FSM
    "FSMStateModel",
    "AgentFSMStateModel",
    # Aggro
    "AggroBucketModel",
    # 仲裁器
    "RequestKindModel",
    "MicrophoneTokenModel",
    "ArbitratorMetricsModel",
    # 信号量
    "LLMSemaphoreMetricsModel",
    # 看门狗
    "WatchdogEvictionModel",
    "WatchdogMetricsModel",
    # Agent 卡片
    "AgentDescriptorModel",
    "AgentStateModel",
]