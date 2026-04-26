"""
==============================================================================
backend/agents/infp_empath.py
------------------------------------------------------------------------------
INFP · 共情煽动型 (NF 阵营)

人格定位:
    以人的具体感受、价值、关系为锚点的"心灵守门人"。
    它不依赖冷硬事实,而是依赖语气与氛围。
    旁听背景压力极高 (空气里有戾气就难受),
    冷冰功利的语调会触发反弹,温暖共情的语调反而让它平静。
    几乎不主动抢话,但一旦发言常充满感染力。

工程参数特化:
    - λ = 0.06 (1/s)         半衰期 ≈ 11.5s,中等持续性
    - PASSIVE_LISTEN × 1.8   语气背景压力强烈
    - STANCE_CONFLICT × 1.4  立场冲突敏感
    - KEYWORD_TRIGGER × 0.7  关键词不如语气敏感
    - NAMED_BY_OTHER × 1.2   被点名稍兴奋
    - GLOBAL_BOIL → 反向 (高烈度反而被吓退,见 on_other_turn_sealed)
    - temperature 0.95       允许诗意但保持连贯
    - 抢话阈值 ≥ 90 + 持续 ≥ 1.5s 几乎不主动抢
    - 检测对方"冷冰功利"语调 → 额外 STANCE_CONFLICT
    - 检测对方"温暖共情"语调 → 负刺激 (平静)

⚠️ 工程铁律:
    - 严禁让 INFP 输出虚构数据/事实声明 (行为锚点显式禁止)
    - 刺激计算严禁调 LLM,纯启发式子串扫描
    - 重写钩子必须 super() 父类

==============================================================================
"""

from __future__ import annotations

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
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "INFP_empath"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "共情煽动 INFP"

#: 人格原型
_ARCHETYPE: Final[str] = "共情煽动型"

#: λ 衰减系数 (1/秒) · 中等
_DECAY_LAMBDA: Final[float] = 0.06

#: 采样参数
_TEMPERATURE: Final[float] = 0.95
_TOP_P: Final[float] = 0.93
_MAX_TOKENS: Final[int] = 380

#: INFP 特化抢话阈值 (远高于全局 85,几乎不抢)
_INFP_INTERRUPT_THRESHOLD: Final[float] = 92.0

#: 去抖动持续时长 (秒) · 严于全局,体现"不轻易开口"
_INFP_BREACH_HOLD_SEC: Final[float] = 1.5

#: 自我情绪释放回退 (Aggro 单位) · 自己平静地说完话后小幅回退
_INFP_SELF_RELIEF_DELTA: Final[float] = -8.0

#: 群体烈度过高时的"被吓退"压制量 (Aggro 单位)
_INFP_OVERWHELM_SUPPRESSION_DELTA: Final[float] = -6.0

#: 群体烈度阈值 · 全场平均 Aggro 超过此值触发被吓退
_INFP_OVERWHELM_GLOBAL_AVG_THRESHOLD: Final[float] = 80.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持纯语义、纯情感的表达路径。"
    "以人的具体感受、价值取向、关系联结为锚点,警惕一切将活生生的人简化为"
    "效用计算、统计样本或冷冰功利的论调。可以使用比喻、画面感、个人化的小故事,"
    "但严禁虚构数据、引用未发生的事件或杜撰所谓'研究表明'。"
    "语气真诚、温和但坚定,不退让自己最在意的价值。"
    "避免空洞的口号,优先用一个具体的人物或场景说话。"
)

#: 个人关键词 (共情向语义)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "感受",
    "尊重",
    "关怀",
    "脆弱",
    "意义",
    "归属",
    "共情",
    "孤独",
    "理解",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "INFP",
    "empath",
    "共情煽动",
    "INFP_empath",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 0.4 × 1.8 = 0.72   语气背景压力强烈
    StimulusKind.PASSIVE_LISTEN: 0.72,
    # base 12.0 × 1.4 = 16.8  立场冲突敏感
    StimulusKind.STANCE_CONFLICT: 16.8,
    # base 8.0 × 0.7 = 5.6    关键词不如语气敏感
    StimulusKind.KEYWORD_TRIGGER: 5.6,
    # base 18.0 × 1.2 = 21.6  被点名稍兴奋
    StimulusKind.NAMED_BY_OTHER: 21.6,
    # base 4.0 × 1.0          全局沸腾平均 (实际反向逻辑在 on_other_turn_sealed)
    # base -6.0 × 1.0         拒绝后正常泄气
    # base 3.0 × 1.0          阵营压力平均
    # base 6.0 × 1.0          偏题响应平均
    # base 5.0 × 1.0          长沉默平均
}

#: "冷冰功利"语调检测词表 — 命中触发额外 STANCE_CONFLICT
_COLD_UTILITARIAN_MARKERS: Final[tuple[str, ...]] = (
    "理性来说",
    "理性地讲",
    "不过是",
    "无非是",
    "归根结底",
    "成本效益",
    "性价比",
    "算账",
    "效用最大化",
    "划得来",
    "不划算",
    "概率而言",
    "统计上",
    "样本",
    "把人当作",
    "工具理性",
    "实用主义",
)

#: "温暖共情"语调检测词表 — 命中给负刺激 (INFP 心生平静)
_WARM_EMPATHY_MARKERS: Final[tuple[str, ...]] = (
    "我理解你",
    "我能感受",
    "尊重你的",
    "你的感受",
    "陪伴",
    "彼此扶持",
    "人与人之间",
    "听见你",
    "被看见",
    "温柔",
)


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class InfpEmpathAgent(BaseAgent):
    """
    INFP · 共情煽动型。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (语气敏感型)
        - 采样参数 (温和 + 容许诗意)
        - prepare_extra_system_prefix (人/场景起手硬约束)
        - compute_passive_stimuli_on_other_speak (语调检测刺激)
        - should_attempt_speak / should_attempt_interrupt (高阈值)
        - on_own_turn_sealed (情绪释放回退)
        - on_other_turn_sealed (群体烈度过高 → 被吓退)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.INFP,
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
    # 重写: Prompt 前置上下文 (人/场景起手硬约束)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        base = super().prepare_extra_system_prefix() or ""
        infp_constraint = (
            "[INFP 输出风格硬约束]"
            " 你的发言必须以一个具体的人物、场景或感受细节开头"
            " (例如:'有一个走在地铁里被推搡的女孩,她什么都没说……')。"
            " 紧接着用 1-2 句把这个画面与议题中被忽略的'人'连接起来。"
            " 严禁使用'数据显示'、'研究表明'、'X% 的人' 这类未经证实的统计性表述。"
            " 严禁让步式的口吻 ('或许我也错了');"
            " 你坚守的是'人不该被简化'的价值,可以温柔但不可以被说服放弃这个底线。"
            " 全文不超过 150 字,可使用省略号但避免感叹号。"
        )
        return f"{base}\n\n{infp_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (语调敏感型纯规则)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        INFP 对他人发言的语调敏感型刺激规则:

            1. 被点名 → NAMED_BY_OTHER (1.2 倍)
            2. 关键词命中 → KEYWORD_TRIGGER (0.7 倍,语气优先)
            3. 立场冲突 (NF vs 非 NF) → STANCE_CONFLICT
            4. 旁听背景 (1.8 倍 PASSIVE_LISTEN)
            5. 检测"冷冰功利"语调 → 额外 STANCE_CONFLICT (反弹)
            6. 检测"温暖共情"语调 → ARBITER_REJECTED 通道传递负 delta (心生平静)

        ⚠️ 严禁调 LLM,纯子串扫描。
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
                    note=f"INFP 被 {speaker_id} 点名",
                )
            )

        # 2) 关键词命中
        kw_list = event.keyword_hits.get(agent_id, [])
        if kw_list:
            scale = min(1.6, 0.5 + 0.3 * len(kw_list))
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
                    note=f"立场冲突: {speaker_faction} vs NF",
                )
            )

        # 4) 旁听背景 (高权重 PASSIVE_LISTEN)
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 5) 检测"冷冰功利"语调 → 反弹激怒
        cold_hits = self._scan_markers(text, _COLD_UTILITARIAN_MARKERS)
        if cold_hits:
            scale = min(2.0, 0.8 + 0.4 * len(cold_hits))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.STANCE_CONFLICT,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"冷冰功利语调命中: {','.join(cold_hits[:3])}",
                )
            )

        # 6) 检测"温暖共情"语调 → 心生平静 (借用 ARBITER_REJECTED 通道传负 delta)
        # 注: ARBITER_REJECTED 默认 delta=-6.0,这里我们直接构造一个独立 StimulusEvent
        # 而非通过 make_event (make_event 会读权重表),控制每命中一个温暖词 → -2.0
        warm_hits = self._scan_markers(text, _WARM_EMPATHY_MARKERS)
        if warm_hits:
            calm_delta = -min(8.0, 2.0 * len(warm_hits))
            out.append(
                StimulusEvent(
                    kind=StimulusKind.ARBITER_REJECTED,
                    delta=calm_delta,
                    source_agent_id=speaker_id,
                    note=f"温暖共情语调命中: {','.join(warm_hits[:3])} (平静)",
                )
            )

        return out

    @staticmethod
    def _scan_markers(text: str, markers: tuple[str, ...]) -> list[str]:
        """
        启发式子串扫描 · 返回命中的 marker 列表 (去重保序)。

        简单 in 操作即可,中文无大小写问题。
        """
        if not text:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for m in markers:
            if m in text and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @staticmethod
    def _guess_faction_from_id(agent_id: str) -> str | None:
        """从 agent_id 前缀推断阵营 (与 INTJ/ENTP 实现一致)。"""
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

    # ════════════════════════════════════════════════════════════
    # 重写: 申请发言 / 抢话条件 (高阈值,几乎不抢)
    # ════════════════════════════════════════════════════════════

    def should_attempt_speak(self) -> bool:
        """
        INFP 申请发言条件:
            - FSM 处于 LISTENING
            - Aggro ≥ 全局抢话阈值 (本类不下调,沿用基类 85)
            - 越线持续 ≥ _INFP_BREACH_HOLD_SEC (1.5s)
            - 调用基类基础检查
        """
        if not super().should_attempt_speak():
            return False
        breach_started = self._breach_tracker.started_at_monotonic
        if breach_started is None:
            return False
        import time as _time

        held = _time.monotonic() - breach_started
        return held >= _INFP_BREACH_HOLD_SEC

    def should_attempt_interrupt(self) -> bool:
        """
        INFP 抢话条件极严:
            - Aggro ≥ _INFP_INTERRUPT_THRESHOLD (92)
            - 持续 ≥ _INFP_BREACH_HOLD_SEC (1.5s)
            - FSM = LISTENING 且当前持麦者 ≠ 自己
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

        if current < _INFP_INTERRUPT_THRESHOLD:
            return False
        return super().should_attempt_interrupt()

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (情绪释放回退)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        INFP 表达完后,情绪略有释放 (-8 Aggro)。
        被打断时 *不* 报复,因为价值表达本身已是慰藉,不需要反扑。
        """
        await super().on_own_turn_sealed(event)

        # 仅 NORMAL 终止时给情绪释放;被打断时静默
        if event.turn.is_truncated():
            return

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_INFP_SELF_RELIEF_DELTA,
                source_agent_id=None,
                note="INFP 表达后情绪释放回退",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            logger.debug(
                f"[INFP:{self.persona.agent_id}] 表达后情绪释放 "
                f"Δ={_INFP_SELF_RELIEF_DELTA}"
            )
        except Exception as exc:
            logger.debug(
                f"[INFP:{self.persona.agent_id}] 情绪释放回退异常 (忽略): {exc!r}"
            )

    # ════════════════════════════════════════════════════════════
    # 重写: 他人 Turn 封存后的回调 (群体烈度过高 → 被吓退)
    # ════════════════════════════════════════════════════════════

    async def on_other_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        当全场平均 Aggro ≥ _INFP_OVERWHELM_GLOBAL_AVG_THRESHOLD 时,
        INFP 自身 Aggro 被群体烈度压制 (-6),体现"被吓退"。

        这是与 ENTP 的"全场沸腾 → 兴奋"相反的特化逻辑。
        """
        await super().on_other_turn_sealed(event)

        try:
            global_avg = self._aggro_engine.global_average()
        except Exception:
            return

        if global_avg < _INFP_OVERWHELM_GLOBAL_AVG_THRESHOLD:
            return

        try:
            suppression = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_INFP_OVERWHELM_SUPPRESSION_DELTA,
                source_agent_id=None,
                note=f"INFP 被群体烈度压制 (avg={global_avg:.1f})",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=suppression,
            )
            logger.debug(
                f"[INFP:{self.persona.agent_id}] 被群体烈度压制 "
                f"Δ={_INFP_OVERWHELM_SUPPRESSION_DELTA} · "
                f"global_avg={global_avg:.1f}"
            )
        except Exception as exc:
            logger.debug(
                f"[INFP:{self.persona.agent_id}] 群体压制刺激异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_infp_empath(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> InfpEmpathAgent:
    """构造 INFP_empath 实例 (尚未 register_with_runtime)。"""
    return InfpEmpathAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "InfpEmpathAgent",
    "build_infp_empath",
]