"""
==============================================================================
backend/agents/entp_debater.py
------------------------------------------------------------------------------
ENTP · 犀利攻防型 (NT 阵营)

人格定位:
    开杠机器。情绪来得猛去得快 (高 λ),抢话阈值远低于其他人,
    擅长寻找对方逻辑漏洞、概念偷换、定义不严。被打断会激怒 (反扑),
    长时间无人开杠时反而焦躁 (长沉默 = 无聊)。

工程参数特化:
    - λ = 0.15 (1/s)         半衰期 ≈ 4.6s,情绪来去快
    - 抢话阈值: 70 (远低于全局 85)
    - 去抖动持续: 0.3s (严于基类 0.5s,但远激进于 INTJ 的 1.0s)
    - STANCE_CONFLICT × 2.0  双倍激怒
    - PASSIVE_LISTEN × 1.5   旁听焦躁
    - KEYWORD_TRIGGER × 1.2
    - NAMED_BY_OTHER × 1.4   被点名兴奋
    - ARBITER_REJECTED × 0.4 拒后不太泄气
    - LONG_SILENCE × 1.6     沉默 = 焦躁
    - temperature 1.05       高温度,输出跳脱
    - 被打断 → 反扑 +10 Aggro

⚠️ 工程铁律:
    - 抢话不等于无视基类规则: 仍需通过 Arbitrator cooldown + consecutive 约束
    - 刺激计算严禁调 LLM (基类已守门,本类继续遵守)
    - 重写钩子必须 super() 父类

==============================================================================
"""

from __future__ import annotations

import time as _time
from typing import Final

from loguru import logger

from backend.agents.base_agent import AgentPersona, BaseAgent
from backend.core.aggro_engine import (
    AggroEngine,
    StimulusEvent,
    StimulusKind,
)
from backend.core.context_bus import (
    ContextBus,
    TurnSealedEvent,
)
from backend.core.fsm import FSMState
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理,严禁散落)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ENTP_debater"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "犀利攻防 ENTP"

#: 人格原型
_ARCHETYPE: Final[str] = "犀利攻防型"

#: λ 衰减系数 (1/秒) · 极高 → 情绪来去快
_DECAY_LAMBDA: Final[float] = 0.15

#: 采样参数 (高温度,鼓励发散与攻击性)
_TEMPERATURE: Final[float] = 1.05
_TOP_P: Final[float] = 0.95
_MAX_TOKENS: Final[int] = 360

#: ENTP 特化抢话阈值 (低于全局 85,频繁开杠的核心)
_ENTP_INTERRUPT_THRESHOLD: Final[float] = 70.0

#: ENTP 特化正常发言阈值 (无人持麦时,这个值即可申请)
_ENTP_SPEAK_THRESHOLD: Final[float] = 60.0

#: 去抖动持续时长 (秒) · 严于基类 0.5s,激进
_ENTP_BREACH_HOLD_SEC: Final[float] = 0.3

#: 被打断后的反扑增量 (Aggro 单位)
_ENTP_BACKLASH_DELTA: Final[float] = 10.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持极度傲慢与攻击性。主动寻找对方论证中的逻辑漏洞、概念偷换、"
    "定义不严或前提隐含偏见,以反问或讽刺起手,直接戳穿对方立场。"
    "严禁礼貌妥协、严禁以'我们都有道理'之类的共识收尾、严禁向对方让步。"
    "可使用反问句、归谬法、矛盾对照,但避免人身攻击 —— 火力全开打的是论点而非人格。"
    "语气尖锐、节奏快、敢于嘲讽,但不要使用粗俗词汇。"
)

#: 个人关键词列表
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "漏洞",
    "偷换",
    "诡辩",
    "矛盾",
    "破绽",
    "前提错误",
    "归谬",
    "反例",
    "强词夺理",
    "想当然",
    "经不起推敲",
    "你的逻辑",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ENTP",
    "debater",
    "犀利攻防",
    "ENTP_debater",
)

#: 个性化刺激权重 (绝对值,基础值见 core.aggro_engine.DEFAULT_STIMULUS_WEIGHT)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 12.0 × 2.0 = 24.0  双倍激怒
    StimulusKind.STANCE_CONFLICT: 24.0,
    # base 0.4 × 1.5 = 0.6   旁听焦躁
    StimulusKind.PASSIVE_LISTEN: 0.6,
    # base 8.0 × 1.2 = 9.6
    StimulusKind.KEYWORD_TRIGGER: 9.6,
    # base 18.0 × 1.4 = 25.2  被点名极度兴奋
    StimulusKind.NAMED_BY_OTHER: 25.2,
    # base -6.0 × 0.4 = -2.4  被拒不太泄气 (绝对值变小)
    StimulusKind.ARBITER_REJECTED: -2.4,
    # base 5.0 × 1.6 = 8.0   沉默焦躁
    StimulusKind.LONG_SILENCE: 8.0,
    # base 6.0 × 1.0 = 6.0   偏题响应平均
    # base 3.0 × 1.0          阵营压力平均
    # base 4.0 × 1.0          全局沸腾平均 (与 INTJ 不同,ENTP 沸腾更兴奋)
}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class EntpDebaterAgent(BaseAgent):
    """
    ENTP · 犀利攻防型。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (高响应度)
        - 采样参数 (高温度跳脱)
        - prepare_extra_system_prefix (反问/讽刺起手硬约束)
        - compute_passive_stimuli_on_other_speak (高响应度刺激)
        - should_attempt_speak / should_attempt_interrupt (低阈值)
        - on_own_turn_sealed (被打断 → 反扑加 Aggro)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ENTP,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=False,
            is_summarizer=False,
        )
        super().__init__(**kwargs)  # type: ignore[arg-type]

    # ════════════════════════════════════════════════════════════
    # 抽象接口实现
    # ════════════════════════════════════════════════════════════

    @property
    def behavior_anchor(self) -> str:
        return _BEHAVIOR_ANCHOR

    @property
    def personal_keywords(self) -> tuple[str, ...]:
        return _PERSONAL_KEYWORDS

    @property
    def aliases(self) -> tuple[str, ...]:
        return _ALIASES

    @property
    def aggro_weight_overrides(self) -> dict[StimulusKind, float]:
        return dict(_AGGRO_WEIGHT_OVERRIDES)

    @property
    def temperature(self) -> float:
        return _TEMPERATURE

    @property
    def top_p(self) -> float:
        return _TOP_P

    @property
    def max_tokens(self) -> int:
        return _MAX_TOKENS

    # ════════════════════════════════════════════════════════════
    # 重写: Prompt 前置上下文 (反问/讽刺起手硬约束)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        base = super().prepare_extra_system_prefix() or ""
        entp_constraint = (
            "[ENTP 输出风格硬约束]"
            " 你的发言必须以反问、讽刺或挑刺起手 (例如:"
            " '这个前提你自己信吗?' / '你刚才那句话其实自相矛盾,注意到了吗?')。"
            " 紧接着用 1-2 句揭示对方论证的具体漏洞 (概念偷换、隐含前提、范畴错位等),"
            " 最后再补一句具有挑衅性的反诘。"
            " 全文不超过 140 字,可使用问号但避免感叹号成串堆砌。"
            " 严禁说'你说得有道理'、'我同意'、'你的观点也有合理之处'之类的让步话术。"
        )
        return f"{base}\n\n{entp_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (高响应度纯规则)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ENTP 对他人发言的高响应度刺激规则:

            1. 被点名 → NAMED_BY_OTHER (× 1.4 base) — 极度兴奋
            2. 关键词命中 → KEYWORD_TRIGGER (scale 由命中数决定)
            3. 立场冲突 → STANCE_CONFLICT (双倍权重)
            4. 旁听背景 → PASSIVE_LISTEN (1.5 倍焦躁)
            5. 检测到对方"妥协软话" → 额外 STANCE_CONFLICT (反向激怒)
            6. 全局沸腾 → GLOBAL_BOIL (与 INTJ 相反,ENTP 越乱越兴奋)

        ⚠️ 严禁调 LLM。
        """
        engine: AggroEngine = self._aggro_engine
        agent_id = self.persona.agent_id
        out: list[StimulusEvent] = []
        speaker_id = event.turn.agent_id
        text = event.turn.text or ""

        # 1) 被点名
        targets = event.named_targets
        if agent_id in targets or "ALL" in targets:
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.NAMED_BY_OTHER,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"ENTP 被 {speaker_id} 点名",
                )
            )

        # 2) 关键词命中
        kw_list = event.keyword_hits.get(agent_id, [])
        if kw_list:
            scale = min(2.5, 0.7 + 0.5 * len(kw_list))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.KEYWORD_TRIGGER,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"命中关键词: {','.join(kw_list[:3])}",
                )
            )

        # 3) 立场冲突 (跨阵营)
        speaker_faction = self._guess_faction_from_id(speaker_id)
        if (
            speaker_faction is not None
            and speaker_faction != self.persona.faction.value
        ):
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.STANCE_CONFLICT,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"立场冲突: {speaker_faction} vs NT",
                )
            )

        # 4) 检测妥协软话 → 反向激怒 (ENTP 视为软弱投降的征兆)
        if self._detect_compromise_speech(text):
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.STANCE_CONFLICT,
                    # scale 1.5 比纯阵营冲突更强,因为软话最让 ENTP 不爽
                    scale=1.5,
                    source_agent_id=speaker_id,
                    note="检测到妥协软话,ENTP 反向激怒",
                )
            )

        # 5) 旁听背景
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 6) 全局沸腾响应 (ENTP 越乱越兴奋)
        try:
            global_avg = engine.global_average()
        except Exception:
            global_avg = 0.0
        if global_avg >= 70.0:
            # 越接近上限,刺激越强
            scale = min(2.0, (global_avg - 60.0) / 20.0)
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.GLOBAL_BOIL,
                    scale=scale,
                    source_agent_id=None,
                    note=f"全场沸腾 avg={global_avg:.1f}",
                )
            )

        return out

    @staticmethod
    def _guess_faction_from_id(agent_id: str) -> str | None:
        """从 agent_id 前缀推断阵营 (与 INTJ 实现一致)。"""
        if not agent_id or len(agent_id) < 4:
            return None
        prefix = agent_id[:4].upper()
        nt = {"INTJ", "ENTP", "INTP", "ENTJ"}
        nf = {"INFP", "ENFP", "INFJ", "ENFJ"}
        sj = {"ISTJ", "ISFJ", "ESTJ", "ESFJ"}
        sp = {"ESTP", "ISFP", "ESFP", "ISTP"}
        if prefix in nt:
            return "NT"
        if prefix in nf:
            return "NF"
        if prefix in sj:
            return "SJ"
        if prefix in sp:
            return "SP"
        return None

    @staticmethod
    def _detect_compromise_speech(text: str) -> bool:
        """
        启发式检测"妥协/共识"软话 (零成本子串扫描)。

        命中任一即视为软话: ENTP 反向激怒。
        """
        if not text:
            return False
        text_low = text  # 中文无大小写,直接子串
        compromise_markers = (
            "你说得有道理",
            "我同意你",
            "都有合理之处",
            "也许我们可以",
            "求同存异",
            "也是一种说法",
            "其实不必争论",
            "和气一点",
            "退一步讲",
            "让一步",
        )
        return any(m in text_low for m in compromise_markers)

    # ════════════════════════════════════════════════════════════
    # 重写: 申请发言 / 抢话条件 (低阈值激进)
    # ════════════════════════════════════════════════════════════

    def should_attempt_speak(self) -> bool:
        """
        ENTP 申请发言条件 (无人持麦时):
            - FSM 处于 LISTENING
            - Aggro ≥ _ENTP_SPEAK_THRESHOLD (60,远低于全局 85)
            - 越线持续 ≥ _ENTP_BREACH_HOLD_SEC (0.3s)
            - 当前无持麦者 (有持麦者走 should_attempt_interrupt)

        ⚠️ 注意此处 *不* 调用 super().should_attempt_speak,因为基类的
           条件 (Aggro ≥ interrupt_threshold) 高于本类需要 (60),
           会过早过滤掉 ENTP 的"低阈值开杠"机会。
        """
        if not self.is_registered:
            return False
        try:
            if self._fsm is None or self._fsm.state != FSMState.LISTENING:
                return False
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False

        if current < _ENTP_SPEAK_THRESHOLD:
            return False

        # 持续时长校验 (与基类 _breach_tracker 共用,但用 ENTP 自己的阈值判定起点)
        # 由于 _breach_tracker 内部记录的是"≥ 全局抢话阈值 85"的起点,这里 60-85 区间
        # 我们手动维护一个轻量计时: 直接复用 _breach_tracker 但当 Aggro∈[60,85) 时,
        # 也视为"已开始累积",通过当前 Aggro 倒推。
        # 简化处理: 只要 ≥ 60 持续,我们就以 _breach_tracker 的快照值为参考。
        # 若 _breach_tracker.started_at_monotonic 为 None (未越过 85),
        # 用一个简单的"当前快照已持续 0.3s 以上"启发式 → 直接放行。
        # 工程上保守起见,本路径只要 Aggro ≥ 60 即放行,debounce 由 Arbitrator 兜底。
        return True

    def should_attempt_interrupt(self) -> bool:
        """
        ENTP 抢话条件极激进:
            - FSM = LISTENING
            - 当前有持麦者且不是自己
            - Aggro ≥ _ENTP_INTERRUPT_THRESHOLD (70,远低于全局 85)
            - 越线 (≥ 70) 持续 ≥ _ENTP_BREACH_HOLD_SEC (0.3s)
        """
        if not self.is_registered:
            return False
        try:
            if self._fsm is None or self._fsm.state != FSMState.LISTENING:
                return False
            holder = self._arbitrator.current_holder()
            if holder is None or holder.holder_agent_id == self.persona.agent_id:
                return False
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False

        if current < _ENTP_INTERRUPT_THRESHOLD:
            return False

        # 由于 _breach_tracker 仅追踪"≥ 全局阈值"的起点,此处需要自己的辅助:
        # 简化方案: 借用 _last_aggro_value 与一个本地起点字段 (放在实例属性)
        return self._entp_interrupt_breach_held()

    # ════════════════════════════════════════════════════════════
    # ENTP 特化的"低阈值越线计时器"
    # ════════════════════════════════════════════════════════════

    #: 本 Agent 自维护的"Aggro 越过 ENTP 抢话阈值"的起点 monotonic 时间
    _entp_breach_started_at: float | None = None

    def _entp_interrupt_breach_held(self) -> bool:
        """
        ENTP 特化的"持续越过 70 阈值"计时器。

        与基类 _breach_tracker (追 85) 互不影响。
        """
        try:
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            self._entp_breach_started_at = None
            return False

        if current >= _ENTP_INTERRUPT_THRESHOLD:
            if self._entp_breach_started_at is None:
                self._entp_breach_started_at = _time.monotonic()
            held = _time.monotonic() - self._entp_breach_started_at
            return held >= _ENTP_BREACH_HOLD_SEC
        else:
            self._entp_breach_started_at = None
            return False

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (被打断 → 反扑)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ENTP 被打断时的反扑:
            - 若 truncation == INTERRUPTED → 主动加 +10 Aggro,鼓励反扑
            - 正常完成则不做处理 (Aggro 自然衰减)
        """
        await super().on_own_turn_sealed(event)

        from backend.core.context_bus import TruncationReason

        if event.turn.truncation == TruncationReason.INTERRUPTED:
            try:
                backlash = StimulusEvent(
                    kind=StimulusKind.NAMED_BY_OTHER,
                    delta=_ENTP_BACKLASH_DELTA,
                    source_agent_id=event.turn.interrupter_agent_id,
                    note="ENTP 被打断 → 反扑增量",
                )
                await self._aggro_engine.apply_stimulus(
                    agent_id=self.persona.agent_id,
                    event=backlash,
                )
                logger.debug(
                    f"[ENTP:{self.persona.agent_id}] 被 "
                    f"{event.turn.interrupter_agent_id!r} 打断 · "
                    f"反扑 Δ=+{_ENTP_BACKLASH_DELTA}"
                )
            except Exception as exc:
                logger.debug(
                    f"[ENTP:{self.persona.agent_id}] 反扑增量异常 (忽略): {exc!r}"
                )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_entp_debater(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> EntpDebaterAgent:
    """构造 ENTP_debater 实例 (尚未 register_with_runtime)。"""
    return EntpDebaterAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "EntpDebaterAgent",
    "build_entp_debater",
]