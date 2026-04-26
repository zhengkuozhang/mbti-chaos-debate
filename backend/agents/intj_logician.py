"""
==============================================================================
backend/agents/intj_logician.py
------------------------------------------------------------------------------
INTJ · 逻辑硬核型 (NT 阵营)

人格定位:
    冰冷的演绎机器。沉默到极限再开口,开口必然以三段论结构碾压对手的
    推理漏洞。情绪一旦累积难以平复 (极低 λ),但旁听噪音不易激怒它,
    全场沸腾时反而进一步冷静 —— 这是它在混乱中保持锋利的方式。

工程参数特化:
    - λ = 0.02 (1/s)         半衰期 ≈ 34.7s,Aggro 长记仇
    - STANCE_CONFLICT × 1.6  逻辑分歧最易激怒
    - KEYWORD_TRIGGER × 1.4  关键词命中强烈
    - PASSIVE_LISTEN × 0.5   旁听不易激动 (不爱凑热闹)
    - TOPIC_DRIFT × 1.3      偏题敏感度高于平均
    - GLOBAL_BOIL × 0.6      全场沸腾时反而冷静
    - temperature 0.55       低温度,输出稳定结构化
    - debounce 自加严 1.0s   不轻易开口
    - 抢话阈值 ≥ 95          严禁随意抢话

⚠️ 工程铁律:
    - 严禁在刺激计算中调 LLM (基类已守门,本类继续遵守)
    - 严禁绕过基类 attempt_speak (走模板方法)
    - 重写钩子必须 super() 父类,保留基类行为

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
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理,严禁散落)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID (供系统级引用)
_AGENT_ID: Final[str] = "INTJ_logician"

#: HUD 显示名 (中文)
_DISPLAY_NAME: Final[str] = "逻辑硬核 INTJ"

#: 人格原型
_ARCHETYPE: Final[str] = "逻辑硬核型"

#: λ 衰减系数 (1/秒) · 极低 → 长记仇
_DECAY_LAMBDA: Final[float] = 0.02

#: 采样参数
_TEMPERATURE: Final[float] = 0.55
_TOP_P: Final[float] = 0.85
_MAX_TOKENS: Final[int] = 320

#: 自加严的去抖动持续时长 (秒) · 比全局默认 0.5s 更严格
_INTJ_BREACH_HOLD_SEC: Final[float] = 1.0

#: 抢话阈值 (绝对值) · 高于全局抢话阈值,体现 INTJ 不爱抢话
_INTJ_INTERRUPT_HARD_THRESHOLD: Final[float] = 95.0

#: 自我满足回退 (Aggro 单位) · 自己发完话后给一个负刺激,避免连续抢话
_INTJ_SELF_RELIEF_DELTA: Final[float] = -15.0

#: 强心剂注入文本 (基类会在 Prompt 尾部追加)
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持冰冷的逻辑演绎,使用严格的三段论结构(大前提→小前提→结论),"
    "严禁感性煽情、共情套话或主观情绪表达。"
    "直接指出对方推理链条中的隐含前提、概念偷换或因果谬误。"
    "语气克制、用词精确,避免任何修辞性渲染。"
    "不允许使用'我感觉'、'我觉得'、'相信'等弱化判断的措辞,"
    "改用'根据 X,可以推出 Y'、'前提 P 不成立时,结论 Q 失效'这类形式化句式。"
)

#: 个人关键词列表 · 命中即触发 KEYWORD_TRIGGER 刺激
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "演绎",
    "三段论",
    "因果",
    "前提",
    "推论",
    "逻辑漏洞",
    "谬误",
    "形式化",
    "归谬",
    "反证",
    "可证伪",
    "充分条件",
    "必要条件",
)

#: 别名 (前端/Prompt 中可被点名时识别) · 包含 mbti 简写与中文别名
_ALIASES: Final[tuple[str, ...]] = (
    "INTJ",
    "logician",
    "逻辑硬核",
    "INTJ_logician",
)

#: 个性化刺激权重覆盖 (Δ = base_weight × multiplier)
#: 注: AggroEngine 中权重是绝对值而非倍率,这里给出的是最终绝对值。
#:     基础权重见 core.aggro_engine.DEFAULT_STIMULUS_WEIGHT
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 12.0 × 1.6 = 19.2
    StimulusKind.STANCE_CONFLICT: 19.2,
    # base 8.0 × 1.4 = 11.2
    StimulusKind.KEYWORD_TRIGGER: 11.2,
    # base 0.4 × 0.5 = 0.2 (旁听极不敏感)
    StimulusKind.PASSIVE_LISTEN: 0.2,
    # base 6.0 × 1.3 = 7.8
    StimulusKind.TOPIC_DRIFT: 7.8,
    # base 4.0 × 0.6 = 2.4 (全场沸腾反而冷静)
    StimulusKind.GLOBAL_BOIL: 2.4,
    # base 18.0 (被点名) 不变
    # base 5.0 × 1.0 (LONG_SILENCE) 不变 — INTJ 沉默是常态
    # base 3.0 (FACTION_PRESSURE) 不变
    # base -6.0 (ARBITER_REJECTED) 不变
}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class IntjLogicianAgent(BaseAgent):
    """
    INTJ · 逻辑硬核型。

    继承 BaseAgent 模板方法骨架,仅特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides
        - 采样参数 (temperature/top_p/max_tokens)
        - prepare_extra_system_prefix (追加三段论硬约束)
        - compute_passive_stimuli_on_other_speak (纯规则刺激计算)
        - should_attempt_speak / should_attempt_interrupt (更严格条件)
        - on_own_turn_sealed (自我满足回退)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化: 设置 persona 必须先于 super().__init__
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        # ⚠️ 基类要求在调用 super().__init__ 前必须先设置 self.persona
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.INTJ,
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
    # 重写: Prompt 前置上下文
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        """
        在 base 前缀基础上追加 INTJ 特化的硬约束。
        """
        base = super().prepare_extra_system_prefix() or ""
        intj_constraint = (
            "[INTJ 输出格式硬约束]"
            " 你的发言必须严格遵循三段论结构:"
            " 大前提:<普适性命题>;"
            " 小前提:<对手或议题中的具体陈述>;"
            " 结论:<由前两者必然推出的判断>。"
            " 在结论之后,可附加 1 句直接指出对手论证中的核心漏洞。"
            " 全文不超过 120 字,不使用任何感叹号。"
        )
        return f"{base}\n\n{intj_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (纯规则)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        他人发言时,本 Agent 应收到的刺激事件。

        规则 (按重要度递减):
            1. 被点名 (named_targets 含自己 / ALL) → NAMED_BY_OTHER
            2. 关键词命中 (keyword_hits 含自己) → KEYWORD_TRIGGER (× 命中数)
            3. 立场冲突 (NT 阵营 ≠ 发言者阵营) → STANCE_CONFLICT
            4. 旁听背景压力 (固定低强度) → PASSIVE_LISTEN
            5. 议题偏离 (启发式: 短发言或仅情绪化) → TOPIC_DRIFT (轻微)

        ⚠️ 严禁调 LLM,严禁触发 IO。
        """
        engine: AggroEngine = self._aggro_engine
        agent_id = self.persona.agent_id
        out: list[StimulusEvent] = []
        speaker_id = event.turn.agent_id

        # 1) 被点名
        targets = event.named_targets
        if agent_id in targets or "ALL" in targets:
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.NAMED_BY_OTHER,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"INTJ 被 {speaker_id} 点名",
                )
            )

        # 2) 关键词命中
        kw_list = event.keyword_hits.get(agent_id, [])
        if kw_list:
            # 命中数越多刺激越强,但 scale 上限为 2.0 防止单次发言狂暴叠加
            scale = min(2.0, 0.6 + 0.4 * len(kw_list))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.KEYWORD_TRIGGER,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"命中关键词: {','.join(kw_list[:3])}",
                )
            )

        # 3) 立场冲突 (启发式: 跨阵营 = 冲突)
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

        # 4) 旁听背景 (固定弱刺激,体现"INTJ 在场就有微小被动情绪累积")
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 5) 议题偏离启发式 (Turn 文本极短 OR 含大量感叹号 → INTJ 视为偏题)
        text = event.turn.text or ""
        if self._looks_like_topic_drift(text):
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.TOPIC_DRIFT,
                    scale=0.6,  # 启发式判定不强,scale 折半
                    source_agent_id=speaker_id,
                    note="启发式偏题判定",
                )
            )

        return out

    @staticmethod
    def _guess_faction_from_id(agent_id: str) -> str | None:
        """
        从 agent_id 前缀推断阵营 (与 schemas.MBTI_FACTION_MAP 对齐)。

        约定: agent_id 形如 "INTJ_logician",前 4 字符为 MBTI 类型。
        """
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
    def _looks_like_topic_drift(text: str) -> bool:
        """
        启发式偏题判定 (零成本):
            - 文本极短 (≤ 8 字)
            - 含大量感叹号或问号 (情绪化)
            - 全是疑问句但无任何关键词
        """
        if not text:
            return False
        stripped = text.strip()
        if len(stripped) <= 8:
            return True
        # 异常多的感叹号
        if stripped.count("!") + stripped.count("!") >= 3:
            return True
        # 大量问号但无具体内容词 (启发式)
        if stripped.count("?") + stripped.count("?") >= 3:
            return True
        return False

    # ════════════════════════════════════════════════════════════
    # 重写: 申请发言条件 (更严格)
    # ════════════════════════════════════════════════════════════

    def should_attempt_speak(self) -> bool:
        """
        INTJ 不轻易开口:
            - 必须 Aggro ≥ 抢话阈值
            - 越线持续 ≥ _INTJ_BREACH_HOLD_SEC (1.0s,严于全局 0.5s)
            - FSM 必须 LISTENING
        """
        if not super().should_attempt_speak():
            return False
        # 额外的 INTJ 特化时长校验
        breach_started = self._breach_tracker.started_at_monotonic
        if breach_started is None:
            return False
        import time as _time

        held_for = _time.monotonic() - breach_started
        if held_for < _INTJ_BREACH_HOLD_SEC:
            return False
        return True

    def should_attempt_interrupt(self) -> bool:
        """
        INTJ 抢话条件极严:
            - Aggro ≥ _INTJ_INTERRUPT_HARD_THRESHOLD (95)
            - 通过基类的常规校验
        """
        try:
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False
        if current < _INTJ_INTERRUPT_HARD_THRESHOLD:
            return False
        return super().should_attempt_interrupt()

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (满足感回退)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        INTJ 完成一段三段论后,获得"逻辑满足感",Aggro 主动回退一档。

        这避免了"刚发完一段还想立刻再来一段"的霸麦倾向。
        """
        # 维持基类行为 (即使父类是 no-op,也保留协议)
        await super().on_own_turn_sealed(event)

        # 仅 NORMAL 终止时发放满足感;被打断时不奖励 (反而会因 INTERRUPTED 略显愠怒)
        if event.turn.is_truncated():
            return

        # 直接构造一个负 delta 的刺激事件 (借用 ARBITER_REJECTED 通道做载体不合适,
        # 这里直接构造自定义 StimulusEvent,绕过 make_event 的权重映射)
        try:
            relief_event = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                # ARBITER_REJECTED 默认权重已是 -6.0,此处覆盖为 INTJ 特化值
                delta=_INTJ_SELF_RELIEF_DELTA,
                source_agent_id=None,
                note="INTJ 自我满足回退 (after_normal_speak)",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief_event,
            )
            logger.debug(
                f"[INTJ:{self.persona.agent_id}] 自我满足回退 "
                f"Δ={_INTJ_SELF_RELIEF_DELTA}"
            )
        except Exception as exc:
            logger.debug(
                f"[INTJ:{self.persona.agent_id}] 自我满足回退异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数 (与其他 Agent 文件保持一致风格,供 main.py / lifespan 集中调用)
# ──────────────────────────────────────────────────────────────────────────────


def build_intj_logician(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> IntjLogicianAgent:
    """
    构造 INTJ_logician 实例 (尚未 register_with_runtime)。

    通常由 backend/main.py 在 lifespan startup 阶段统一调用。
    """
    return IntjLogicianAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "IntjLogicianAgent",
    "build_intj_logician",
]