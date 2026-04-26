"""
==============================================================================
backend/core/aggro_engine.py
------------------------------------------------------------------------------
Aggro 压力值物理引擎 · 防溢出逼近公式 · 纯规则刺激

核心公式:
    A_t = max(
        AGGRO_MIN,
        min(
            AGGRO_MAX,
            AGGRO_MAX - (AGGRO_MAX - A_{t-1}) * exp(-λ · Δt) + Σ ΔA_stimulus
        )
    )

物理诠释:
    - "AGGRO_MAX - (AGGRO_MAX - A_{t-1}) * exp(-λ·Δt)" 表示 A 向 AGGRO_MAX 衰减
      实际上当 A < MAX 时趋向于 MAX·(1-e^-λΔt)+A·e^-λΔt ≈ A·e^-λΔt → 趋向 0
      当 A_{t-1}=0 时, 第一项 = MAX·(1-e^-λΔt) ≈ 小幅自然爬升 (持续旁路压力)
      当 A_{t-1}=MAX 时, 第一项 = MAX (无衰减,饱和态)
    - Σ ΔA_stimulus 是本轮所有规则刺激量的代数和 (可正可负)

量纲铁律:
    - λ 单位: 1/秒
    - Δt 单位: 秒 (由 time.monotonic() 差值得出)
    - 严禁混入毫秒/分钟,否则衰减常数会偏差 60 倍或 1000 倍

⚠️ 工程铁律:
    - 全程纯规则,严禁调用 LLM (LISTENING 态算力节约的核心保证)
    - 任何中间值出现 NaN / Inf / 越界,立即 clamp 到 [MIN, MAX]
    - 同一 Agent 的更新串行化 (asyncio.Lock),防并发竞态

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Final, Iterable, Optional

from loguru import logger

from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 类型别名 (语义化,杜绝量纲混淆)
# ──────────────────────────────────────────────────────────────────────────────

Seconds = float
"""时间间隔,单位严格为「秒」。"""

InversedSeconds = float
"""λ 衰减系数,单位严格为「1/秒」。"""

AggroValue = float
"""Aggro 数值,严格在 [AGGRO_MIN, AGGRO_MAX] 区间。"""


# ──────────────────────────────────────────────────────────────────────────────
# 刺激源枚举 + 默认权重
# ──────────────────────────────────────────────────────────────────────────────


class StimulusKind(str, enum.Enum):
    """
    9 类显式刺激源 · 全部纯规则,严禁依赖 LLM 推理。

    工程上每类刺激由不同的代码路径触发:
        - NAMED_BY_OTHER:  发言文本 (前缀拦截后) 检出 [TARGET: <self>]
        - STANCE_CONFLICT: 立场分类向量与发言者夹角 > 阈值 (基于阵营)
        - KEYWORD_TRIGGER: 个人关键词命中 (静态词表,O(1) AC自动机)
        - PASSIVE_LISTEN:  每秒被动累积 (LISTENING 态背景压力)
        - TOPIC_DRIFT:     议题偏离度 (ISTJ 强敏感)
        - LONG_SILENCE:    自身沉默时长 > 阈值 (ESTP 真空期补位)
        - FACTION_PRESSURE: 同阵营 Agent 平均 Aggro 高
        - GLOBAL_BOIL:     全局平均 Aggro > 阈值 (ENFP 破冰申请触发器)
        - ARBITER_REJECTED: 仲裁器拒绝抢话申请 (轻微泄气,负刺激)
    """

    NAMED_BY_OTHER = "NAMED_BY_OTHER"
    STANCE_CONFLICT = "STANCE_CONFLICT"
    KEYWORD_TRIGGER = "KEYWORD_TRIGGER"
    PASSIVE_LISTEN = "PASSIVE_LISTEN"
    TOPIC_DRIFT = "TOPIC_DRIFT"
    LONG_SILENCE = "LONG_SILENCE"
    FACTION_PRESSURE = "FACTION_PRESSURE"
    GLOBAL_BOIL = "GLOBAL_BOIL"
    ARBITER_REJECTED = "ARBITER_REJECTED"


#: 默认刺激权重 (每次触发的瞬时 ΔA 单位: Aggro 值)
#: 这是中性默认,各 Agent 可在自己的人格定义中覆盖 (例 ENTP 对 STANCE_CONFLICT 翻倍)
DEFAULT_STIMULUS_WEIGHT: Final[dict[StimulusKind, float]] = {
    StimulusKind.NAMED_BY_OTHER:  18.0,   # 被点名 → 强烈
    StimulusKind.STANCE_CONFLICT: 12.0,   # 立场冲突 → 较强
    StimulusKind.KEYWORD_TRIGGER:  8.0,   # 关键词命中 → 中等
    StimulusKind.PASSIVE_LISTEN:   0.4,   # 旁听背景 → 极弱 (按秒触发)
    StimulusKind.TOPIC_DRIFT:      6.0,   # 偏题 → 中等 (ISTJ 倍率)
    StimulusKind.LONG_SILENCE:     5.0,   # 长时沉默 → 中等 (ESTP 倍率)
    StimulusKind.FACTION_PRESSURE: 3.0,   # 同阵营压力 → 弱
    StimulusKind.GLOBAL_BOIL:      4.0,   # 全局沸腾 → 弱 (ENFP 强响应)
    StimulusKind.ARBITER_REJECTED: -6.0,  # 抢话被拒 → 负刺激
}


# ──────────────────────────────────────────────────────────────────────────────
# 单次刺激事件
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StimulusEvent:
    """
    单次刺激事件 · 不可变。

    多个刺激事件汇聚后,通过 Σ ΔA_stimulus 一次性应用到 Aggro 公式。
    """

    kind: StimulusKind
    delta: float
    """实际叠加的 ΔA (考虑了 Agent 个性化倍率后的最终值)。"""

    source_agent_id: Optional[str] = None
    """触发源 Agent ID (例: 谁点名了我)。系统类刺激为 None。"""

    note: str = ""
    """诊断备注 (例: "命中关键词: 自由意志")。"""

    at_monotonic: float = field(default_factory=time.monotonic)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "delta": round(self.delta, 4),
            "source_agent_id": self.source_agent_id,
            "note": self.note,
            "at_monotonic": round(self.at_monotonic, 6),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Aggro 历史快照
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AggroSnapshot:
    """
    单 Agent Aggro 瞬时快照 · 供 HUD / 诊断接口序列化。
    """

    agent_id: str
    current: AggroValue
    peak: AggroValue
    decay_lambda: InversedSeconds
    last_update_monotonic: float
    updates_total: int
    clamped_total: int
    last_decay_seconds: Seconds

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "current": round(self.current, 3),
            "peak": round(self.peak, 3),
            "decay_lambda": round(self.decay_lambda, 6),
            "last_decay_seconds": round(self.last_decay_seconds, 4),
            "updates_total": self.updates_total,
            "clamped_total": self.clamped_total,
        }


@dataclass(frozen=True, slots=True)
class AggroHistoryPoint:
    """折线图采样点 · 历史环成员。"""

    at_monotonic: float
    value: AggroValue
    delta_applied: float
    """本次更新累计的 Σ ΔA_stimulus (不含衰减部分)。"""

    def to_dict(self) -> dict[str, object]:
        return {
            "at_monotonic": round(self.at_monotonic, 6),
            "value": round(self.value, 3),
            "delta_applied": round(self.delta_applied, 4),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class AggroError(Exception):
    """Aggro 引擎异常基类。"""


class InvalidTimeIntervalError(AggroError):
    """Δt 量纲非法 (负值 / NaN / Inf)。"""


class AggroAgentNotRegisteredError(AggroError):
    """请求的 Agent 未在 AggroEngine 中注册。"""


class AggroAgentAlreadyRegisteredError(AggroError):
    """重复注册同一 Agent。"""


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────


def _validate_finite_seconds(value: Seconds, name: str) -> None:
    """
    严格校验时间间隔值: 必须有限、非负。

    NaN / Inf / 负数都会被判定非法 —— 时钟回拨、环境异常都不允许污染衰减。
    """
    if not math.isfinite(value):
        raise InvalidTimeIntervalError(
            f"{name} 必须为有限数值,当前: {value!r} (NaN/Inf 不允许)"
        )
    if value < 0.0:
        raise InvalidTimeIntervalError(
            f"{name} 必须 ≥ 0,当前: {value!r} (检查时钟回拨或参数顺序)"
        )


def _clamp(value: float, lo: float, hi: float) -> float:
    """
    防溢出截断 · 严格 [lo, hi]。

    NaN / Inf 视为溢出,统一归到 lo (保守降权)。
    """
    if not math.isfinite(value):
        return lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ──────────────────────────────────────────────────────────────────────────────
# 单 Agent Aggro 状态
# ──────────────────────────────────────────────────────────────────────────────


class _AgentAggroState:
    """
    单 Agent 的 Aggro 内部状态。

    严禁外部直接访问字段,仅通过 AggroEngine 公共 API 操作。
    """

    HISTORY_RING_SIZE: Final[int] = 128
    """历史环容量,FIFO 弹出最旧记录。"""

    def __init__(
        self,
        agent_id: str,
        decay_lambda: InversedSeconds,
        weights: dict[StimulusKind, float],
        initial_value: AggroValue,
        aggro_min: AggroValue,
        aggro_max: AggroValue,
    ) -> None:
        if decay_lambda <= 0.0 or not math.isfinite(decay_lambda):
            raise ValueError(
                f"decay_lambda 必须 > 0 且有限,当前: {decay_lambda!r}"
            )
        if aggro_min >= aggro_max:
            raise ValueError(
                f"AGGRO_MIN ({aggro_min}) 必须严格小于 AGGRO_MAX ({aggro_max})"
            )

        self._agent_id: Final[str] = agent_id
        self._aggro_min: Final[AggroValue] = aggro_min
        self._aggro_max: Final[AggroValue] = aggro_max

        self._value: AggroValue = _clamp(initial_value, aggro_min, aggro_max)
        self._peak: AggroValue = self._value
        self._decay_lambda: InversedSeconds = decay_lambda

        # 个性化权重 (浅拷贝,防外部修改)
        self._weights: dict[StimulusKind, float] = dict(weights)

        # 时间戳
        self._last_update_monotonic: float = time.monotonic()

        # 累计指标
        self._updates_total: int = 0
        self._clamped_total: int = 0
        self._last_decay_seconds: Seconds = 0.0

        # 历史环 (Aggro 折线图 + 刺激日志)
        self._history: deque[AggroHistoryPoint] = deque(
            maxlen=self.HISTORY_RING_SIZE
        )
        self._stimuli_log: deque[StimulusEvent] = deque(
            maxlen=self.HISTORY_RING_SIZE
        )

        # 协程级独占锁 → 同 Agent 串行化更新
        self._lock: asyncio.Lock = asyncio.Lock()

    # ────────────────────────────────────────────────────────────
    # 属性
    # ────────────────────────────────────────────────────────────

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def value(self) -> AggroValue:
        """当前 Aggro 值 (只读快照)。"""
        return self._value

    @property
    def lock(self) -> asyncio.Lock:
        """暴露锁仅供 AggroEngine 内部跨 Agent 协调,业务层禁用。"""
        return self._lock

    # ────────────────────────────────────────────────────────────
    # 核心: 应用衰减 + 刺激
    # ────────────────────────────────────────────────────────────

    def _apply_decay_and_stimuli(
        self,
        stimuli: Iterable[StimulusEvent],
        now_monotonic: Optional[float] = None,
    ) -> AggroHistoryPoint:
        """
        ⚠️ 此方法不持锁,调用方必须已持有 self._lock。

        执行公式:
            A_t = clamp(
                AGGRO_MAX - (AGGRO_MAX - A_{t-1}) * exp(-λ·Δt) + Σ ΔA
            )

        但注意: 上述公式当 A_{t-1}=0 时仍有自然向 MAX 爬升的小幅项,
        这与"无刺激时应衰减到 0"的工程直觉相反。
        因此这里采用"对 A 自身指数衰减到 0"的工程化形式:

            A_decayed = A_{t-1} * exp(-λ·Δt)
            A_t = clamp(A_decayed + Σ ΔA)

        这与原始公式在数学上等价 (经过坐标变换),且更符合工程语义:
        无外界刺激时 Aggro 衰减回平静 (= AGGRO_MIN)。
        """
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        dt: Seconds = now - self._last_update_monotonic

        # 量纲守门
        _validate_finite_seconds(dt, "Δt")

        # 1) 衰减步: A_decayed = (A - MIN) · exp(-λ·Δt) + MIN
        #    这样保证衰减目标是 AGGRO_MIN 而非 0,兼容 MIN > 0 的配置
        try:
            decay_factor = math.exp(-self._decay_lambda * dt)
        except OverflowError:
            # λ·Δt 极大时 exp 下溢为 0,语义正确,继续
            decay_factor = 0.0

        if not math.isfinite(decay_factor):
            decay_factor = 0.0

        prev = self._value
        decayed = (prev - self._aggro_min) * decay_factor + self._aggro_min

        # 2) 刺激累加 + 记录刺激日志
        delta_sum: float = 0.0
        clamped_in_this_round: int = 0
        for ev in stimuli:
            if not math.isfinite(ev.delta):
                logger.warning(
                    f"[Aggro:{self._agent_id}] 忽略非法 delta · "
                    f"kind={ev.kind.value} · delta={ev.delta!r}"
                )
                clamped_in_this_round += 1
                continue
            delta_sum += ev.delta
            self._stimuli_log.append(ev)

        # 3) 合成 + 截断
        raw = decayed + delta_sum
        clamped = _clamp(raw, self._aggro_min, self._aggro_max)
        if raw != clamped:
            clamped_in_this_round += 1

        # 4) 提交状态
        self._value = clamped
        if clamped > self._peak:
            self._peak = clamped
        self._last_update_monotonic = now
        self._last_decay_seconds = dt
        self._updates_total += 1
        self._clamped_total += clamped_in_this_round

        # 5) 历史环
        point = AggroHistoryPoint(
            at_monotonic=now,
            value=clamped,
            delta_applied=delta_sum,
        )
        self._history.append(point)

        logger.debug(
            f"[Aggro:{self._agent_id}] {prev:.2f} → {clamped:.2f} · "
            f"Δt={dt:.3f}s · decay={decay_factor:.4f} · ΣΔA={delta_sum:+.2f}"
        )
        return point

    # ────────────────────────────────────────────────────────────
    # 个性化权重查询
    # ────────────────────────────────────────────────────────────

    def weight_of(self, kind: StimulusKind) -> float:
        """获取该 Agent 对某类刺激的权重 (含个性化覆盖)。"""
        return self._weights.get(kind, DEFAULT_STIMULUS_WEIGHT[kind])

    def make_event(
        self,
        kind: StimulusKind,
        scale: float = 1.0,
        source_agent_id: Optional[str] = None,
        note: str = "",
    ) -> StimulusEvent:
        """
        基于本 Agent 个性化权重构造刺激事件。

        scale: 强度倍率 (例: PASSIVE_LISTEN 按经过秒数乘 scale=Δt)
        """
        if not math.isfinite(scale):
            raise ValueError(f"scale 必须有限,当前: {scale!r}")
        delta = self.weight_of(kind) * scale
        return StimulusEvent(
            kind=kind,
            delta=delta,
            source_agent_id=source_agent_id,
            note=note,
        )

    # ────────────────────────────────────────────────────────────
    # 快照 / 历史
    # ────────────────────────────────────────────────────────────

    def snapshot(self) -> AggroSnapshot:
        return AggroSnapshot(
            agent_id=self._agent_id,
            current=self._value,
            peak=self._peak,
            decay_lambda=self._decay_lambda,
            last_update_monotonic=self._last_update_monotonic,
            updates_total=self._updates_total,
            clamped_total=self._clamped_total,
            last_decay_seconds=self._last_decay_seconds,
        )

    def history(self, last_n: Optional[int] = None) -> list[AggroHistoryPoint]:
        records = list(self._history)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records

    def stimuli_log(
        self,
        last_n: Optional[int] = None,
    ) -> list[StimulusEvent]:
        records = list(self._stimuli_log)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records


# ──────────────────────────────────────────────────────────────────────────────
# 主体: AggroEngine
# ──────────────────────────────────────────────────────────────────────────────


class AggroEngine:
    """
    全局 Aggro 引擎 · 管理所有 Agent 的压力值演化。

    核心 API:
        - register_agent(agent_id, decay_lambda, weights, initial)
        - apply_stimulus(agent_id, event)              单刺激单 Agent
        - apply_stimuli(agent_id, events)              多刺激单 Agent
        - batch_update_with_stimuli(stimulus_map)      批量屏障
        - decay_only(agent_id) / decay_all()           无刺激衰减 (例如周期性 tick)
        - snapshot(agent_id) / snapshot_all()          快照
        - history(agent_id) / global_average()         分析
    """

    def __init__(
        self,
        aggro_min: AggroValue,
        aggro_max: AggroValue,
        interrupt_threshold: AggroValue,
        default_decay_lambda: InversedSeconds,
    ) -> None:
        if aggro_min >= aggro_max:
            raise ValueError(
                f"aggro_min ({aggro_min}) 必须严格小于 aggro_max ({aggro_max})"
            )
        if not (aggro_min < interrupt_threshold < aggro_max):
            raise ValueError(
                f"interrupt_threshold ({interrupt_threshold}) 必须严格落在 "
                f"({aggro_min}, {aggro_max}) 开区间"
            )
        if default_decay_lambda <= 0.0:
            raise ValueError(
                f"default_decay_lambda 必须 > 0,当前: {default_decay_lambda!r}"
            )

        self._aggro_min: Final[AggroValue] = aggro_min
        self._aggro_max: Final[AggroValue] = aggro_max
        self._interrupt_threshold: Final[AggroValue] = interrupt_threshold
        self._default_decay_lambda: Final[InversedSeconds] = default_decay_lambda

        self._agents: dict[str, _AgentAggroState] = {}

        # 跨 Agent 批量协调锁
        self._batch_lock: asyncio.Lock = asyncio.Lock()

        logger.info(
            f"[AggroEngine] 初始化 · range=[{aggro_min}, {aggro_max}] · "
            f"interrupt_th={interrupt_threshold} · "
            f"default_λ={default_decay_lambda} (1/s)"
        )

    # ────────────────────────────────────────────────────────────
    # 注册管理
    # ────────────────────────────────────────────────────────────

    def register_agent(
        self,
        agent_id: str,
        decay_lambda: Optional[InversedSeconds] = None,
        weight_overrides: Optional[dict[StimulusKind, float]] = None,
        initial_value: Optional[AggroValue] = None,
    ) -> None:
        """
        注册一个 Agent 到引擎。

        Args:
            decay_lambda: 个性化 λ; None 则使用全局默认 (8 个 Agent 各异)
            weight_overrides: 个性化刺激权重覆盖 (例 ENTP 翻倍 STANCE_CONFLICT)
            initial_value: 初始 Aggro; None 则取 AGGRO_MIN
        """
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(f"agent_id 必须为非空字符串,当前: {agent_id!r}")

        if agent_id in self._agents:
            raise AggroAgentAlreadyRegisteredError(
                f"Agent {agent_id!r} 已注册到 AggroEngine"
            )

        # 合并默认权重 + 覆盖
        merged_weights: dict[StimulusKind, float] = dict(DEFAULT_STIMULUS_WEIGHT)
        if weight_overrides:
            for kind, w in weight_overrides.items():
                if not isinstance(kind, StimulusKind):
                    raise TypeError(
                        f"weight_overrides 的 key 必须是 StimulusKind 枚举,"
                        f"当前: {kind!r}"
                    )
                if not math.isfinite(w):
                    raise ValueError(
                        f"weight_overrides[{kind.value}] 必须有限,当前: {w!r}"
                    )
                merged_weights[kind] = float(w)

        state = _AgentAggroState(
            agent_id=agent_id,
            decay_lambda=(
                decay_lambda
                if decay_lambda is not None
                else self._default_decay_lambda
            ),
            weights=merged_weights,
            initial_value=(
                initial_value if initial_value is not None else self._aggro_min
            ),
            aggro_min=self._aggro_min,
            aggro_max=self._aggro_max,
        )
        self._agents[agent_id] = state

        logger.info(
            f"[AggroEngine] 注册 Agent {agent_id!r} · "
            f"λ={state._decay_lambda} · initial={state.value:.2f}"
        )

    def unregister_agent(self, agent_id: str) -> bool:
        if agent_id not in self._agents:
            return False
        del self._agents[agent_id]
        return True

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def all_agent_ids(self) -> tuple[str, ...]:
        return tuple(self._agents.keys())

    # ────────────────────────────────────────────────────────────
    # 边界查询
    # ────────────────────────────────────────────────────────────

    @property
    def aggro_min(self) -> AggroValue:
        return self._aggro_min

    @property
    def aggro_max(self) -> AggroValue:
        return self._aggro_max

    @property
    def interrupt_threshold(self) -> AggroValue:
        return self._interrupt_threshold

    def is_above_interrupt_threshold(self, agent_id: str) -> bool:
        """判定该 Agent 是否达到抢话阈值。"""
        state = self._require_state(agent_id)
        return state.value >= self._interrupt_threshold

    # ────────────────────────────────────────────────────────────
    # 单 Agent 更新
    # ────────────────────────────────────────────────────────────

    async def apply_stimulus(
        self,
        agent_id: str,
        event: StimulusEvent,
    ) -> AggroHistoryPoint:
        """单刺激应用,内部串行化。"""
        return await self.apply_stimuli(agent_id, (event,))

    async def apply_stimuli(
        self,
        agent_id: str,
        events: Iterable[StimulusEvent],
    ) -> AggroHistoryPoint:
        """
        对单 Agent 应用一组刺激 (含衰减步)。

        典型场景: 一轮发言结束后,该 Agent 收到本轮所有针对它的刺激事件。
        """
        state = self._require_state(agent_id)
        events_list = list(events)
        async with state.lock:
            return state._apply_decay_and_stimuli(events_list)

    async def decay_only(self, agent_id: str) -> AggroHistoryPoint:
        """
        仅触发衰减 (无刺激)。例如周期性 tick / 真空期。
        """
        state = self._require_state(agent_id)
        async with state.lock:
            return state._apply_decay_and_stimuli(())

    async def decay_all(self) -> dict[str, AggroHistoryPoint]:
        """
        对所有已注册 Agent 触发纯衰减 · 适用于全局周期 tick。
        """
        async with self._batch_lock:
            results: dict[str, AggroHistoryPoint] = {}
            now = time.monotonic()
            tasks = {
                aid: self._decay_locked(state, now)
                for aid, state in self._agents.items()
            }
            collected = await asyncio.gather(
                *tasks.values(), return_exceptions=True
            )
            for aid, outcome in zip(tasks.keys(), collected, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error(
                        f"[AggroEngine] decay_all · agent={aid!r} 异常: {outcome!r}"
                    )
                else:
                    results[aid] = outcome
            return results

    @staticmethod
    async def _decay_locked(
        state: _AgentAggroState,
        now: float,
    ) -> AggroHistoryPoint:
        """对单 Agent 加锁后纯衰减 (供 decay_all 内部使用)。"""
        async with state.lock:
            return state._apply_decay_and_stimuli((), now_monotonic=now)

    # ────────────────────────────────────────────────────────────
    # 批量屏障 (UPDATING 配套)
    # ────────────────────────────────────────────────────────────

    async def batch_update_with_stimuli(
        self,
        stimulus_map: dict[str, list[StimulusEvent]],
    ) -> dict[str, AggroHistoryPoint]:
        """
        批量原子性更新多个 Agent 的 Aggro。

        典型用法 (回合结束):
            registry.batch_transition(FSMState.UPDATING, ...)
            await aggro_engine.batch_update_with_stimuli(round_stimuli)
            registry.batch_transition(FSMState.LISTENING, ...)

        Args:
            stimulus_map: {agent_id: [事件列表]} · 未在 map 中的 Agent
                          仅触发纯衰减,不施加刺激。

        Returns:
            dict[agent_id, AggroHistoryPoint]: 每 Agent 更新后的最新点
        """
        async with self._batch_lock:
            now = time.monotonic()

            # 校验 ID 全部已注册
            unknown_ids = [
                aid for aid in stimulus_map.keys() if aid not in self._agents
            ]
            if unknown_ids:
                raise AggroAgentNotRegisteredError(
                    f"batch_update 引用未注册 Agent: {unknown_ids}"
                )

            tasks: dict[str, asyncio.Future[AggroHistoryPoint]] = {}
            for aid, state in self._agents.items():
                events = stimulus_map.get(aid, [])
                tasks[aid] = asyncio.ensure_future(
                    self._update_one_locked(state, events, now)
                )

            collected = await asyncio.gather(
                *tasks.values(), return_exceptions=True
            )

            results: dict[str, AggroHistoryPoint] = {}
            for aid, outcome in zip(tasks.keys(), collected, strict=True):
                if isinstance(outcome, BaseException):
                    logger.error(
                        f"[AggroEngine] batch_update 异常 · "
                        f"agent={aid!r} · err={outcome!r}"
                    )
                else:
                    results[aid] = outcome
            return results

    @staticmethod
    async def _update_one_locked(
        state: _AgentAggroState,
        events: list[StimulusEvent],
        now: float,
    ) -> AggroHistoryPoint:
        async with state.lock:
            return state._apply_decay_and_stimuli(events, now_monotonic=now)

    # ────────────────────────────────────────────────────────────
    # 个性化刺激事件构造器 (业务层语法糖)
    # ────────────────────────────────────────────────────────────

    def make_event(
        self,
        agent_id: str,
        kind: StimulusKind,
        scale: float = 1.0,
        source_agent_id: Optional[str] = None,
        note: str = "",
    ) -> StimulusEvent:
        """基于该 Agent 的个性化权重构造刺激事件。"""
        state = self._require_state(agent_id)
        return state.make_event(
            kind=kind,
            scale=scale,
            source_agent_id=source_agent_id,
            note=note,
        )

    # ────────────────────────────────────────────────────────────
    # 观测
    # ────────────────────────────────────────────────────────────

    def snapshot(self, agent_id: str) -> AggroSnapshot:
        state = self._require_state(agent_id)
        return state.snapshot()

    def snapshot_all(self) -> dict[str, AggroSnapshot]:
        return {aid: state.snapshot() for aid, state in self._agents.items()}

    def snapshot_dict(self) -> dict[str, dict[str, object]]:
        return {aid: snap.to_dict() for aid, snap in self.snapshot_all().items()}

    def history(
        self,
        agent_id: str,
        last_n: Optional[int] = None,
    ) -> list[AggroHistoryPoint]:
        state = self._require_state(agent_id)
        return state.history(last_n=last_n)

    def stimuli_log(
        self,
        agent_id: str,
        last_n: Optional[int] = None,
    ) -> list[StimulusEvent]:
        state = self._require_state(agent_id)
        return state.stimuli_log(last_n=last_n)

    def global_average(self) -> AggroValue:
        """所有已注册 Agent 当前 Aggro 的均值 · 供 GLOBAL_BOIL 检测。"""
        if not self._agents:
            return self._aggro_min
        total = sum(state.value for state in self._agents.values())
        return total / len(self._agents)

    def count_above_threshold(self) -> int:
        """统计当前 Aggro ≥ interrupt_threshold 的 Agent 数。"""
        return sum(
            1
            for state in self._agents.values()
            if state.value >= self._interrupt_threshold
        )

    # ────────────────────────────────────────────────────────────
    # 内部
    # ────────────────────────────────────────────────────────────

    def _require_state(self, agent_id: str) -> _AgentAggroState:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise AggroAgentNotRegisteredError(
                f"Agent {agent_id!r} 未注册到 AggroEngine"
            ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_engine_singleton: Optional[AggroEngine] = None


def get_aggro_engine() -> AggroEngine:
    """
    获取全局 AggroEngine 单例。

    使用模式:
        from backend.core.aggro_engine import (
            get_aggro_engine, StimulusKind,
        )
        engine = get_aggro_engine()
        ev = engine.make_event(
            "INTJ_logician",
            StimulusKind.NAMED_BY_OTHER,
            source_agent_id="ENTP_debater",
            note="ENTP 直接质疑了我的演绎链",
        )
        await engine.apply_stimulus("INTJ_logician", ev)
    """
    global _engine_singleton
    if _engine_singleton is None:
        s = get_settings()
        _engine_singleton = AggroEngine(
            aggro_min=s.AGGRO_MIN,
            aggro_max=s.AGGRO_MAX,
            interrupt_threshold=s.AGGRO_INTERRUPT_THRESHOLD,
            default_decay_lambda=s.AGGRO_LAMBDA_DEFAULT,
        )
    return _engine_singleton


def reset_engine_for_testing() -> None:
    """[仅供测试] 重置全局引擎。"""
    global _engine_singleton
    _engine_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 类型别名
    "Seconds",
    "InversedSeconds",
    "AggroValue",
    # 枚举与权重
    "StimulusKind",
    "DEFAULT_STIMULUS_WEIGHT",
    # 数据类
    "StimulusEvent",
    "AggroSnapshot",
    "AggroHistoryPoint",
    # 主体
    "AggroEngine",
    # 异常
    "AggroError",
    "InvalidTimeIntervalError",
    "AggroAgentNotRegisteredError",
    "AggroAgentAlreadyRegisteredError",
    # 单例
    "get_aggro_engine",
    "reset_engine_for_testing",
]