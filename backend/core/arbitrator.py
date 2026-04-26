"""
==============================================================================
backend/core/arbitrator.py
------------------------------------------------------------------------------
全局仲裁器 · 中断令牌优先级锁 · Microphone Authority

工程死局背景:
    8 个 Agent 同时检测到 "我应该发言" 时,若各自直奔 LLM 推理,
    会出现:
      1. 多人同时输出 → 文本流污染
      2. 都在等信号量 → 死锁
      3. 都成功推理后竞争 WS → 视觉撕裂
    必须有唯一的"麦克风权威"裁决谁发言、何时打断、何时切换。

核心机制:
    1. 单令牌 (MicrophoneToken) + asyncio.Lock 守门
       同一时刻全系统只有一个 Agent 持有令牌
    2. 单调递增 epoch · 防 ABA 问题
       释放/抢占必须携带 epoch 验证,旧令牌的 release 静默失败
    3. 三维度优先级合成:
         priority = aggro * AGGRO_WEIGHT
                  + max(0, aggro - threshold) * URGENCY_WEIGHT
                  - cooldown_penalty
                  - consecutive_grants_penalty
    4. 去抖动 (Debounce):
       申请方需维持 Aggro ≥ 阈值 ≥ AGGRO_INTERRUPT_DEBOUNCE_SEC 秒
       由申请方在 request_microphone() 调用前自行计时
    5. 霸麦冷却 (Cooldown):
       刚说完的 Agent 进入 SPEAKER_COOLDOWN_SEC 冷却
       期间所有申请直接拒绝,杜绝"我刚说完又抢回来"
    6. 抢话路由协议:
       interrupter → request_interrupt(priority)
       仲裁器评估优先级 → 通知当前持有者 (interrupt_event.set)
       持有者 (llm_router) 监听到信号 → aclose() 断流
       持有者调用 release_token(epoch) 释放
       仲裁器下发新令牌给抢话者

⚠️ 工程铁律:
    - 释放令牌必须携带 epoch (防止抢话后"幽灵释放")
    - 同一 Agent 严禁连续多次持麦 (冷却兜底)
    - interrupt_event 必须由持有者轮询,严禁仲裁器主动 cancel 任务
      (任务 cancel 是 llm_router 的 aclose 路径职责)

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Final, Optional

from loguru import logger

from backend.core.config import (
    AGGRO_INTERRUPT_DEBOUNCE_SEC,
    get_settings,
)


# ──────────────────────────────────────────────────────────────────────────────
# 物理常量
# ──────────────────────────────────────────────────────────────────────────────

#: 刚发言完的 Agent 进入冷却时长 (秒) · 期间抢话申请直接拒绝
SPEAKER_COOLDOWN_SEC: Final[float] = 3.0

#: 优先级合成中 Aggro 主项权重
_PRIORITY_AGGRO_WEIGHT: Final[float] = 1.0

#: 紧急度加成权重 (Aggro 超出阈值的部分额外加成)
_PRIORITY_URGENCY_WEIGHT: Final[float] = 1.5

#: 冷却期内的优先级惩罚 (强制低于任何正常申请)
_PRIORITY_COOLDOWN_PENALTY: Final[float] = 1e6

#: 同 Agent 连续持麦的阻尼惩罚 (每次连续 +一份)
_PRIORITY_CONSECUTIVE_PENALTY_PER_GRANT: Final[float] = 25.0

#: 真空期阈值 (秒) · 超过此时长无人发言,允许 ESTP 申请真空期分配
DEFAULT_VACUUM_DETECTION_SEC: Final[float] = 6.0


# ──────────────────────────────────────────────────────────────────────────────
# 申请来源 (用于诊断 + 优先级微调)
# ──────────────────────────────────────────────────────────────────────────────


class RequestKind(str, enum.Enum):
    """麦克风申请来源类型。"""

    AGGRO_THRESHOLD = "AGGRO_THRESHOLD"
    """正常 Aggro 阈值触发"""

    INTERRUPT = "INTERRUPT"
    """高 Aggro 抢话 · 当前已有持有者"""

    VACUUM_FILL = "VACUUM_FILL"
    """真空期补位 (ESTP 控场)"""

    SCHEDULED = "SCHEDULED"
    """系统调度 (会话开始 / 轮询)"""


# ──────────────────────────────────────────────────────────────────────────────
# 麦克风令牌
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MicrophoneToken:
    """
    麦克风持有令牌 · 不可变。

    持有者必须在释放/被打断时携带此 token (epoch) 调用 release_token,
    epoch 不匹配 → 视为幽灵释放,静默丢弃。
    """

    epoch: int
    """单调递增编号 · 防 ABA"""

    holder_agent_id: str

    granted_at_monotonic: float
    granted_at_wall_unix: float

    request_kind: RequestKind

    priority_score: float
    """授权时的优先级分数 (供诊断)"""

    interrupt_event: asyncio.Event = field(repr=False)
    """
    持有者通过 await event.wait() 监听抢话通知。
    被抢话时仲裁器调用 event.set(),持有者随即触发 aclose() 断流。
    """

    def to_dict(self) -> dict[str, object]:
        return {
            "epoch": self.epoch,
            "holder_agent_id": self.holder_agent_id,
            "granted_at_monotonic": round(self.granted_at_monotonic, 6),
            "granted_at_wall_unix": round(self.granted_at_wall_unix, 6),
            "request_kind": self.request_kind.value,
            "priority_score": round(self.priority_score, 4),
            "interrupt_pending": self.interrupt_event.is_set(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 申请记录
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """单次授权 / 拒绝记录 · 历史环成员。"""

    request_id: str
    agent_id: str
    request_kind: RequestKind
    aggro_at_request: float
    priority_score: float
    granted: bool
    rejected_reason: Optional[str]
    epoch_granted: Optional[int]
    interrupted_holder_id: Optional[str]
    """如果是抢话授权,被打断的持有者 ID"""
    at_monotonic: float
    at_wall_unix: float

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "request_kind": self.request_kind.value,
            "aggro_at_request": round(self.aggro_at_request, 3),
            "priority_score": round(self.priority_score, 4),
            "granted": self.granted,
            "rejected_reason": self.rejected_reason,
            "epoch_granted": self.epoch_granted,
            "interrupted_holder_id": self.interrupted_holder_id,
            "at_monotonic": round(self.at_monotonic, 6),
            "at_wall_unix": round(self.at_wall_unix, 6),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 仲裁器观测指标
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ArbitratorMetrics:
    current_holder_id: Optional[str]
    current_epoch: int
    current_token_age_sec: float
    granted_total: int
    interrupt_granted_total: int
    rejected_total: int
    debounce_drop_total: int
    last_release_at_monotonic: float
    last_speaker_id: Optional[str]
    cooldown_remaining_sec: float
    seconds_since_any_speech: float

    def to_dict(self) -> dict[str, object]:
        return {
            "current_holder_id": self.current_holder_id,
            "current_epoch": self.current_epoch,
            "current_token_age_sec": round(self.current_token_age_sec, 3),
            "granted_total": self.granted_total,
            "interrupt_granted_total": self.interrupt_granted_total,
            "rejected_total": self.rejected_total,
            "debounce_drop_total": self.debounce_drop_total,
            "last_speaker_id": self.last_speaker_id,
            "cooldown_remaining_sec": round(self.cooldown_remaining_sec, 3),
            "seconds_since_any_speech": round(self.seconds_since_any_speech, 3),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class ArbitratorError(Exception):
    pass


class MicrophoneBusyError(ArbitratorError):
    """request_microphone 失败 · 当前已有持有者,且无法抢话。"""


class CooldownActiveError(ArbitratorError):
    """请求 Agent 处于发言后冷却期。"""


class DebounceNotMetError(ArbitratorError):
    """去抖动窗口未满,申请被丢弃。"""


class InvalidEpochError(ArbitratorError):
    """release_token 携带了不匹配的 epoch (幽灵释放尝试)。"""


# ──────────────────────────────────────────────────────────────────────────────
# 主体: Arbitrator
# ──────────────────────────────────────────────────────────────────────────────


class Arbitrator:
    """
    全局仲裁器 (进程级单例)。

    核心 API:
        - request_microphone(agent_id, kind, aggro, ...) -> MicrophoneToken
        - request_interrupt(agent_id, aggro, ...)        -> MicrophoneToken
        - release_token(token)                           -> bool
        - nominate_vacuum_speaker(agent_id, ...)         -> Optional[MicrophoneToken]
        - current_holder()                               -> Optional[MicrophoneToken]
        - metrics()                                      -> ArbitratorMetrics
    """

    HISTORY_RING_SIZE: Final[int] = 256

    def __init__(
        self,
        interrupt_threshold: float,
        speaker_cooldown_sec: float = SPEAKER_COOLDOWN_SEC,
        vacuum_detection_sec: float = DEFAULT_VACUUM_DETECTION_SEC,
        debounce_sec: float = AGGRO_INTERRUPT_DEBOUNCE_SEC,
    ) -> None:
        if speaker_cooldown_sec < 0.0:
            raise ValueError(
                f"speaker_cooldown_sec 必须 ≥ 0,当前: {speaker_cooldown_sec}"
            )
        if vacuum_detection_sec < 1.0:
            raise ValueError(
                f"vacuum_detection_sec 必须 ≥ 1,当前: {vacuum_detection_sec}"
            )
        if debounce_sec < 0.0:
            raise ValueError(
                f"debounce_sec 必须 ≥ 0,当前: {debounce_sec}"
            )

        self._interrupt_threshold: Final[float] = interrupt_threshold
        self._cooldown_sec: Final[float] = speaker_cooldown_sec
        self._vacuum_sec: Final[float] = vacuum_detection_sec
        self._debounce_sec: Final[float] = debounce_sec

        # 单调递增 epoch (从 1 起)
        self._epoch_counter: int = 0

        # 当前持有者 (None 表示空闲)
        self._current: Optional[MicrophoneToken] = None

        # 串行化锁 · 所有 grant/release 必须持锁
        self._lock: asyncio.Lock = asyncio.Lock()

        # 上次释放时间 + 上次说话者 (冷却判定)
        self._last_release_monotonic: float = time.monotonic()
        self._last_speaker_id: Optional[str] = None

        # 上次有任何发言活动的时间 (真空期判定)
        self._last_speech_activity_monotonic: float = time.monotonic()

        # 同 Agent 连续持麦计数 (阻尼惩罚)
        self._consecutive_grants: dict[str, int] = {}

        # 累计指标
        self._granted_total: int = 0
        self._interrupt_granted_total: int = 0
        self._rejected_total: int = 0
        self._debounce_drop_total: int = 0

        # 历史环
        self._history: deque[GrantRecord] = deque(maxlen=self.HISTORY_RING_SIZE)

        logger.info(
            f"[Arbitrator] 初始化 · interrupt_th={interrupt_threshold} · "
            f"cooldown={speaker_cooldown_sec}s · "
            f"vacuum={vacuum_detection_sec}s · "
            f"debounce={debounce_sec}s"
        )

    # ════════════════════════════════════════════════════════════
    # 公开属性
    # ════════════════════════════════════════════════════════════

    @property
    def interrupt_threshold(self) -> float:
        return self._interrupt_threshold

    def current_holder(self) -> Optional[MicrophoneToken]:
        """获取当前持有者 (无锁只读快照)。"""
        return self._current

    def is_idle(self) -> bool:
        return self._current is None

    def cooldown_remaining_sec(self, agent_id: str) -> float:
        """
        指定 Agent 当前剩余冷却时长 (秒)。

        规则: 仅"上次发言者 = agent_id"时才有冷却,其他 Agent 无冷却。
        """
        if self._last_speaker_id != agent_id:
            return 0.0
        elapsed = time.monotonic() - self._last_release_monotonic
        return max(0.0, self._cooldown_sec - elapsed)

    def seconds_since_last_speech(self) -> float:
        """距离上次任何发言活动经过的秒数 (供真空期判定)。"""
        return time.monotonic() - self._last_speech_activity_monotonic

    # ════════════════════════════════════════════════════════════
    # 核心: 申请麦克风
    # ════════════════════════════════════════════════════════════

    async def request_microphone(
        self,
        agent_id: str,
        kind: RequestKind,
        aggro: float,
        aggro_above_threshold_since_monotonic: Optional[float] = None,
    ) -> MicrophoneToken:
        """
        申请麦克风 (非抢话场景)。

        Args:
            agent_id: 申请者
            kind: 申请来源 (AGGRO_THRESHOLD / VACUUM_FILL / SCHEDULED)
            aggro: 申请时该 Agent 的 Aggro 值
            aggro_above_threshold_since_monotonic:
                Aggro 持续 ≥ 阈值的起点时间 (monotonic),
                供去抖动判定。None 表示申请方放弃去抖动校验
                (例: SCHEDULED 系统调度无需去抖动)

        Returns:
            MicrophoneToken: 授权令牌

        Raises:
            MicrophoneBusyError: 当前已有持有者
            CooldownActiveError: 申请方处于冷却期
            DebounceNotMetError: 去抖动窗口未满 (仅 AGGRO_THRESHOLD)
        """
        self._validate_agent_id(agent_id)

        async with self._lock:
            # 1) 当前空闲?
            if self._current is not None:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=kind,
                    aggro=aggro,
                    priority=0.0,
                    reason=(
                        f"microphone busy · holder="
                        f"{self._current.holder_agent_id!r}"
                    ),
                )
                raise MicrophoneBusyError(
                    f"麦克风被占用 · holder="
                    f"{self._current.holder_agent_id!r} · "
                    f"epoch={self._current.epoch}"
                )

            # 2) 冷却检查 (仅同 Agent)
            cooldown_remaining = self.cooldown_remaining_sec(agent_id)
            if cooldown_remaining > 0:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=kind,
                    aggro=aggro,
                    priority=0.0,
                    reason=(
                        f"cooldown {cooldown_remaining:.2f}s remaining"
                    ),
                )
                raise CooldownActiveError(
                    f"Agent {agent_id!r} 仍在发言冷却期 "
                    f"({cooldown_remaining:.2f}s 剩余)"
                )

            # 3) 去抖动检查 (仅 AGGRO_THRESHOLD 来源需要)
            if kind == RequestKind.AGGRO_THRESHOLD:
                if not self._check_debounce(
                    aggro_above_threshold_since_monotonic
                ):
                    self._debounce_drop_total += 1
                    self._reject_and_record(
                        agent_id=agent_id,
                        kind=kind,
                        aggro=aggro,
                        priority=0.0,
                        reason=(
                            f"debounce window not met "
                            f"(need ≥ {self._debounce_sec}s)"
                        ),
                    )
                    raise DebounceNotMetError(
                        f"Agent {agent_id!r} 去抖动窗口未满 "
                        f"(要求 ≥ {self._debounce_sec}s)"
                    )

            # 4) 优先级计算 (用于诊断,空闲场景下为登记值)
            priority = self._compute_priority(
                aggro=aggro,
                kind=kind,
                consecutive_grants=self._consecutive_grants.get(agent_id, 0),
                is_in_cooldown=False,
            )

            # 5) 授权
            token = self._grant_locked(
                agent_id=agent_id,
                kind=kind,
                aggro=aggro,
                priority=priority,
                interrupted_holder_id=None,
            )
            return token

    # ════════════════════════════════════════════════════════════
    # 核心: 抢话
    # ════════════════════════════════════════════════════════════

    async def request_interrupt(
        self,
        agent_id: str,
        aggro: float,
        aggro_above_threshold_since_monotonic: Optional[float] = None,
    ) -> MicrophoneToken:
        """
        申请抢话 (Interrupt)。

        逻辑:
            1. 申请方 Aggro 必须 ≥ 抢话阈值
            2. 若当前空闲 → 退化为正常授权
            3. 若当前有持有者:
               - 同 Agent 自抢话 → 拒绝
               - 持有者冷却豁免 (持有者刚拿到令牌不到 0.5s 不允许抢) → 拒绝
               - 申请方优先级 ≤ 持有者优先级 → 拒绝
               - 申请方优先级 > 持有者优先级 →
                 通知持有者 (interrupt_event.set) 并阻塞等待释放,
                 释放后立即下发新令牌

        Raises:
            MicrophoneBusyError: 优先级不足或持有者刚获得令牌
            CooldownActiveError / DebounceNotMetError: 同 request_microphone
            ArbitratorError: 申请方 Aggro 不足
        """
        self._validate_agent_id(agent_id)

        if aggro < self._interrupt_threshold:
            raise ArbitratorError(
                f"Agent {agent_id!r} Aggro={aggro:.2f} 未达抢话阈值 "
                f"{self._interrupt_threshold} · 请改用 request_microphone"
            )

        # 第一阶段: 持锁判断是否能抢
        async with self._lock:
            # 冷却 / 去抖动
            cooldown_remaining = self.cooldown_remaining_sec(agent_id)
            if cooldown_remaining > 0:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=0.0,
                    reason=f"cooldown {cooldown_remaining:.2f}s remaining",
                )
                raise CooldownActiveError(
                    f"Agent {agent_id!r} 抢话被冷却拦截 "
                    f"({cooldown_remaining:.2f}s 剩余)"
                )

            if not self._check_debounce(aggro_above_threshold_since_monotonic):
                self._debounce_drop_total += 1
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=0.0,
                    reason=(
                        f"debounce window not met "
                        f"(need ≥ {self._debounce_sec}s)"
                    ),
                )
                raise DebounceNotMetError(
                    f"Agent {agent_id!r} 抢话去抖动窗口未满"
                )

            # 优先级
            requester_priority = self._compute_priority(
                aggro=aggro,
                kind=RequestKind.INTERRUPT,
                consecutive_grants=self._consecutive_grants.get(agent_id, 0),
                is_in_cooldown=False,
            )

            # 当前空闲 → 直接授权 (退化路径)
            if self._current is None:
                token = self._grant_locked(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=requester_priority,
                    interrupted_holder_id=None,
                )
                return token

            # 有持有者: 自抢话拒绝
            if self._current.holder_agent_id == agent_id:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=requester_priority,
                    reason="self-interrupt forbidden",
                )
                raise MicrophoneBusyError(
                    f"Agent {agent_id!r} 不能抢自己的麦克风"
                )

            # 持有者刚拿到令牌不到 debounce_sec → 不允许抢 (避免抖动)
            holder_age = time.monotonic() - self._current.granted_at_monotonic
            if holder_age < self._debounce_sec:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=requester_priority,
                    reason=(
                        f"holder too fresh ({holder_age:.2f}s < "
                        f"{self._debounce_sec}s)"
                    ),
                )
                raise MicrophoneBusyError(
                    f"持有者 {self._current.holder_agent_id!r} 刚获得令牌"
                    f"({holder_age:.2f}s),抢话被拒"
                )

            # 持有者优先级 (使用其授权时分数,避免动态评估扰动)
            holder_priority = self._current.priority_score
            if requester_priority <= holder_priority:
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=requester_priority,
                    reason=(
                        f"priority {requester_priority:.2f} not greater "
                        f"than holder's {holder_priority:.2f}"
                    ),
                )
                raise MicrophoneBusyError(
                    f"抢话优先级不足: {requester_priority:.2f} ≤ "
                    f"持有者 {holder_priority:.2f}"
                )

            # 通知持有者: interrupt_event.set
            interrupted_holder = self._current
            interrupted_epoch = interrupted_holder.epoch
            interrupted_holder.interrupt_event.set()

            logger.info(
                f"[Arbitrator] INTERRUPT 信号下发 · "
                f"interrupter={agent_id!r}({requester_priority:.2f}) > "
                f"holder={interrupted_holder.holder_agent_id!r}"
                f"({holder_priority:.2f})"
            )

        # ── 第二阶段: 锁外等待持有者释放 ──
        # 必须释放锁,因为持有者在 release_token 时也要拿锁
        try:
            await asyncio.wait_for(
                self._wait_for_release(interrupted_epoch),
                timeout=get_settings().LLM_REQUEST_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"[Arbitrator] 等待持有者 "
                f"{interrupted_holder.holder_agent_id!r} 释放超时 · "
                f"strong-evict 兜底"
            )
            # 强制驱逐: 锁内若仍是该 epoch,强行清空
            async with self._lock:
                if (
                    self._current is not None
                    and self._current.epoch == interrupted_epoch
                ):
                    self._strong_evict_locked(interrupted_holder)

        # ── 第三阶段: 重新加锁授权 ──
        async with self._lock:
            # 二次校验: 此刻应当空闲 (持有者已释放)
            if self._current is not None:
                # 极端竞态: 等待期间又被其他 Agent 抢先
                # 此时直接拒绝,要求 interrupter 重试
                self._reject_and_record(
                    agent_id=agent_id,
                    kind=RequestKind.INTERRUPT,
                    aggro=aggro,
                    priority=requester_priority,
                    reason=(
                        f"race lost · new holder="
                        f"{self._current.holder_agent_id!r}"
                    ),
                )
                raise MicrophoneBusyError(
                    f"抢话竞态失败 · 新持有者: "
                    f"{self._current.holder_agent_id!r}"
                )

            token = self._grant_locked(
                agent_id=agent_id,
                kind=RequestKind.INTERRUPT,
                aggro=aggro,
                priority=requester_priority,
                interrupted_holder_id=interrupted_holder.holder_agent_id,
            )
            self._interrupt_granted_total += 1
            return token

    # ════════════════════════════════════════════════════════════
    # 核心: 释放令牌
    # ════════════════════════════════════════════════════════════

    async def release_token(self, token: MicrophoneToken) -> bool:
        """
        释放令牌。

        epoch 必须匹配当前持有者,否则视为幽灵释放,静默返回 False。

        Returns:
            True  - 成功释放
            False - epoch 不匹配 / 当前已空闲 (幽灵释放)
        """
        if not isinstance(token, MicrophoneToken):
            raise TypeError(
                f"token 必须为 MicrophoneToken,当前类型: "
                f"{type(token).__name__}"
            )

        async with self._lock:
            if self._current is None:
                logger.warning(
                    f"[Arbitrator] release_token 时无持有者 · "
                    f"token.epoch={token.epoch} (幽灵释放,忽略)"
                )
                return False

            if self._current.epoch != token.epoch:
                logger.warning(
                    f"[Arbitrator] epoch 不匹配 · "
                    f"current={self._current.epoch} · "
                    f"requested={token.epoch} (幽灵释放,忽略)"
                )
                return False

            self._release_locked(reason="normal_release")
            return True

    async def force_release_if_held_by(
        self,
        agent_id: str,
        reason: str = "force_release",
    ) -> bool:
        """
        强制释放: 仅当持有者匹配 agent_id 时释放。

        用于 lifespan shutdown / 看门狗熔断兜底。
        """
        async with self._lock:
            if (
                self._current is not None
                and self._current.holder_agent_id == agent_id
            ):
                self._release_locked(reason=reason)
                return True
            return False

    # ════════════════════════════════════════════════════════════
    # 核心: 真空期分配 (ESTP 控场)
    # ════════════════════════════════════════════════════════════

    async def nominate_vacuum_speaker(
        self,
        agent_id: str,
        nominator_id: str,
        aggro: float,
    ) -> Optional[MicrophoneToken]:
        """
        真空期补位: ESTP 控场在长时无人发言时调用此 API
        指派下一个发言者。

        判定:
            1. 当前必须空闲
            2. 距离上次发言活动 ≥ vacuum_detection_sec
            3. 被指派者不在冷却期
            4. nominator 必须是 ESTP_conductor (上层校验,本模块不写死)

        Returns:
            授权令牌 (成功) / None (条件未满足,静默失败)
        """
        self._validate_agent_id(agent_id)
        self._validate_agent_id(nominator_id)

        async with self._lock:
            if self._current is not None:
                logger.debug(
                    f"[Arbitrator] vacuum nominate 失败 · 当前非空闲 "
                    f"(holder={self._current.holder_agent_id!r})"
                )
                return None

            silence_sec = self.seconds_since_last_speech()
            if silence_sec < self._vacuum_sec:
                logger.debug(
                    f"[Arbitrator] vacuum nominate 失败 · "
                    f"silence={silence_sec:.2f}s < 阈值 {self._vacuum_sec}s"
                )
                return None

            cooldown_remaining = self.cooldown_remaining_sec(agent_id)
            if cooldown_remaining > 0:
                logger.debug(
                    f"[Arbitrator] vacuum nominate 失败 · "
                    f"被指派者 {agent_id!r} 仍在冷却 ({cooldown_remaining:.2f}s)"
                )
                return None

            priority = self._compute_priority(
                aggro=aggro,
                kind=RequestKind.VACUUM_FILL,
                consecutive_grants=self._consecutive_grants.get(agent_id, 0),
                is_in_cooldown=False,
            )

            logger.info(
                f"[Arbitrator] VACUUM 补位 · {nominator_id!r} 指派 "
                f"{agent_id!r} (silence={silence_sec:.2f}s)"
            )

            token = self._grant_locked(
                agent_id=agent_id,
                kind=RequestKind.VACUUM_FILL,
                aggro=aggro,
                priority=priority,
                interrupted_holder_id=None,
            )
            return token

    # ════════════════════════════════════════════════════════════
    # 内部: 持锁路径
    # ════════════════════════════════════════════════════════════

    def _grant_locked(
        self,
        agent_id: str,
        kind: RequestKind,
        aggro: float,
        priority: float,
        interrupted_holder_id: Optional[str],
    ) -> MicrophoneToken:
        """⚠️ 调用方必须已持有 self._lock"""
        self._epoch_counter += 1
        now_mono = time.monotonic()
        now_wall = time.time()
        token = MicrophoneToken(
            epoch=self._epoch_counter,
            holder_agent_id=agent_id,
            granted_at_monotonic=now_mono,
            granted_at_wall_unix=now_wall,
            request_kind=kind,
            priority_score=priority,
            interrupt_event=asyncio.Event(),
        )
        self._current = token
        self._granted_total += 1

        # 连续持麦计数
        if self._last_speaker_id == agent_id:
            self._consecutive_grants[agent_id] = (
                self._consecutive_grants.get(agent_id, 0) + 1
            )
        else:
            # 切换说话者: 清零旧人计数,新人首授为 1
            self._consecutive_grants.clear()
            self._consecutive_grants[agent_id] = 1

        # 历史记录
        request_id = str(uuid.uuid4())
        record = GrantRecord(
            request_id=request_id,
            agent_id=agent_id,
            request_kind=kind,
            aggro_at_request=aggro,
            priority_score=priority,
            granted=True,
            rejected_reason=None,
            epoch_granted=token.epoch,
            interrupted_holder_id=interrupted_holder_id,
            at_monotonic=now_mono,
            at_wall_unix=now_wall,
        )
        self._history.append(record)

        logger.info(
            f"[Arbitrator] GRANT · agent={agent_id!r} · "
            f"kind={kind.value} · epoch={token.epoch} · "
            f"priority={priority:.2f} · aggro={aggro:.2f}"
            + (
                f" · interrupted={interrupted_holder_id!r}"
                if interrupted_holder_id
                else ""
            )
        )
        return token

    def _release_locked(self, reason: str) -> None:
        """⚠️ 调用方必须已持有 self._lock。前置: self._current is not None"""
        assert self._current is not None  # noqa: S101 (内部一致性断言)
        released = self._current
        now = time.monotonic()
        self._last_release_monotonic = now
        self._last_speech_activity_monotonic = now
        self._last_speaker_id = released.holder_agent_id
        self._current = None
        # 防止下游误持有后再 set
        # (interrupt_event 不复用,但清理状态以利诊断)

        logger.info(
            f"[Arbitrator] RELEASE · agent={released.holder_agent_id!r} · "
            f"epoch={released.epoch} · reason={reason}"
        )

    def _strong_evict_locked(self, expected: MicrophoneToken) -> None:
        """⚠️ 调用方必须已持有 self._lock。强制驱逐当前持有者 (epoch 校验)。"""
        if (
            self._current is not None
            and self._current.epoch == expected.epoch
        ):
            logger.warning(
                f"[Arbitrator] STRONG-EVICT · "
                f"agent={self._current.holder_agent_id!r} · "
                f"epoch={self._current.epoch}"
            )
            self._release_locked(reason="strong_evict_after_timeout")

    def _reject_and_record(
        self,
        agent_id: str,
        kind: RequestKind,
        aggro: float,
        priority: float,
        reason: str,
    ) -> None:
        """⚠️ 调用方必须已持有 self._lock。"""
        self._rejected_total += 1
        now_mono = time.monotonic()
        now_wall = time.time()
        record = GrantRecord(
            request_id=str(uuid.uuid4()),
            agent_id=agent_id,
            request_kind=kind,
            aggro_at_request=aggro,
            priority_score=priority,
            granted=False,
            rejected_reason=reason,
            epoch_granted=None,
            interrupted_holder_id=None,
            at_monotonic=now_mono,
            at_wall_unix=now_wall,
        )
        self._history.append(record)
        logger.debug(
            f"[Arbitrator] REJECT · agent={agent_id!r} · "
            f"kind={kind.value} · reason={reason!r}"
        )

    # ════════════════════════════════════════════════════════════
    # 内部: 工具
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not agent_id or not isinstance(agent_id, str):
            raise ValueError(
                f"agent_id 必须为非空字符串,当前: {agent_id!r}"
            )

    def _check_debounce(
        self,
        aggro_above_threshold_since_monotonic: Optional[float],
    ) -> bool:
        """
        判定去抖动窗口是否满足。

        None = 申请方放弃去抖动校验 (例: SCHEDULED 来源)
        """
        if aggro_above_threshold_since_monotonic is None:
            return True
        elapsed = (
            time.monotonic() - aggro_above_threshold_since_monotonic
        )
        return elapsed >= self._debounce_sec

    def _compute_priority(
        self,
        aggro: float,
        kind: RequestKind,
        consecutive_grants: int,
        is_in_cooldown: bool,
    ) -> float:
        """
        三维度优先级合成:
            base = aggro * AGGRO_WEIGHT
            urgency = max(0, aggro - threshold) * URGENCY_WEIGHT
            cooldown_penalty = COOLDOWN_PENALTY if is_in_cooldown else 0
            consecutive_penalty = CONSEC_PENALTY * (consecutive_grants - 1)+
            kind 微调: VACUUM_FILL 略加成 (鼓励控场)
        """
        base = aggro * _PRIORITY_AGGRO_WEIGHT
        urgency = (
            max(0.0, aggro - self._interrupt_threshold)
            * _PRIORITY_URGENCY_WEIGHT
        )
        cooldown_penalty = (
            _PRIORITY_COOLDOWN_PENALTY if is_in_cooldown else 0.0
        )
        consec_count = max(0, consecutive_grants - 1)
        consec_penalty = (
            consec_count * _PRIORITY_CONSECUTIVE_PENALTY_PER_GRANT
        )

        kind_bonus = 0.0
        if kind == RequestKind.VACUUM_FILL:
            kind_bonus = 5.0  # 真空期补位轻微加成
        elif kind == RequestKind.SCHEDULED:
            kind_bonus = -100.0  # 调度类降权,优先让 Agent 自然申请

        return base + urgency - cooldown_penalty - consec_penalty + kind_bonus

    async def _wait_for_release(self, expected_epoch: int) -> None:
        """
        轮询等待指定 epoch 的持有者释放 (锁外执行)。

        ⚠️ 简单 poll · 间隔 50ms,占用极低。
           如需事件驱动可改为 asyncio.Event,但这里 poll 已足够。
        """
        while True:
            if self._current is None or self._current.epoch != expected_epoch:
                return
            await asyncio.sleep(0.05)

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> ArbitratorMetrics:
        now = time.monotonic()
        token_age = (
            (now - self._current.granted_at_monotonic)
            if self._current is not None
            else 0.0
        )
        cooldown_remain = (
            self.cooldown_remaining_sec(self._last_speaker_id)
            if self._last_speaker_id
            else 0.0
        )
        return ArbitratorMetrics(
            current_holder_id=(
                self._current.holder_agent_id
                if self._current is not None
                else None
            ),
            current_epoch=(
                self._current.epoch if self._current is not None else 0
            ),
            current_token_age_sec=token_age,
            granted_total=self._granted_total,
            interrupt_granted_total=self._interrupt_granted_total,
            rejected_total=self._rejected_total,
            debounce_drop_total=self._debounce_drop_total,
            last_release_at_monotonic=self._last_release_monotonic,
            last_speaker_id=self._last_speaker_id,
            cooldown_remaining_sec=cooldown_remain,
            seconds_since_any_speech=self.seconds_since_last_speech(),
        )

    def history(self, last_n: Optional[int] = None) -> list[GrantRecord]:
        records = list(self._history)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "current_token": (
                self._current.to_dict()
                if self._current is not None
                else None
            ),
            "consecutive_grants": dict(self._consecutive_grants),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_arbitrator_singleton: Optional[Arbitrator] = None


def get_arbitrator() -> Arbitrator:
    """
    获取全局 Arbitrator 单例。

    使用模式:
        from backend.core.arbitrator import (
            get_arbitrator, RequestKind, MicrophoneToken,
        )
        arb = get_arbitrator()
        token = await arb.request_microphone(
            agent_id="INTJ_logician",
            kind=RequestKind.AGGRO_THRESHOLD,
            aggro=86.5,
            aggro_above_threshold_since_monotonic=t_above_threshold_start,
        )
        try:
            # 业务: 推理 + 流式输出
            ...
        finally:
            await arb.release_token(token)
    """
    global _arbitrator_singleton
    if _arbitrator_singleton is None:
        s = get_settings()
        _arbitrator_singleton = Arbitrator(
            interrupt_threshold=s.AGGRO_INTERRUPT_THRESHOLD,
        )
    return _arbitrator_singleton


def reset_arbitrator_for_testing() -> None:
    """[仅供测试] 重置全局 Arbitrator 单例。"""
    global _arbitrator_singleton
    _arbitrator_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 常量
    "SPEAKER_COOLDOWN_SEC",
    "DEFAULT_VACUUM_DETECTION_SEC",
    # 枚举
    "RequestKind",
    # 数据类
    "MicrophoneToken",
    "GrantRecord",
    "ArbitratorMetrics",
    # 主体
    "Arbitrator",
    # 异常
    "ArbitratorError",
    "MicrophoneBusyError",
    "CooldownActiveError",
    "DebounceNotMetError",
    "InvalidEpochError",
    # 单例
    "get_arbitrator",
    "reset_arbitrator_for_testing",
]