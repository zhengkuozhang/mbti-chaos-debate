"""
==============================================================================
backend/agents/isfp_chameleon.py
------------------------------------------------------------------------------
ISFP · 灵活应变型 (SP 阵营) · 系统自适应阀门 · 8 Agents 最后一员

人格定位:
    动态平衡器。情绪较快变化 (较高 λ),不站队、不激辩,
    它的核心功能是 *按需补位* —— 哪个阵营长时间偏弱,它就在下一轮
    把语调微微倾斜过去,平衡全局生态。

工程参数特化:
    - λ = 0.10 (1/s)         半衰期 ≈ 6.9s,较快变化
    - PASSIVE_LISTEN × 1.4   对全场氛围高度敏感
    - GLOBAL_BOIL × 1.2
    - KEYWORD_TRIGGER × 0.8
    - STANCE_CONFLICT × 0.6  不激辩立场
    - LONG_SILENCE × 1.0     平均
    - NAMED_BY_OTHER × 1.2
    - temperature 0.95       较高温度允许语调切换
    - 阵营倾斜至少持续 8s     防止抖动

阵营动态补位 (本类核心特化):
    1. compute_passive_stimuli_on_other_speak 中触发 _evaluate_faction_balance
    2. 计算 NT/NF/SJ/SP 四阵营当前平均 Aggro
    3. 找出最弱阵营 (avg 显著低于全局均值)
    4. 若该阵营持续偏弱 ≥ 8s 且与上次倾斜不同 → 切换 _current_lean_faction
    5. 下一次发言时,prepare_extra_system_prefix 注入语调倾斜指令

注意 (与 ISTJ/ISFJ 阵营规则对齐):
    ISFP 自身阵营是 SP。"补位"是补 *最弱* 阵营的语调,
    不是补自己阵营 —— 这与 ISTJ 守议题、ISFJ 考据档案完全不同。

⚠️ 工程铁律:
    - 阵营评估严禁调 LLM (基于 AggroEngine 快照纯规则)
    - 阵营倾斜至少持续 _LEAN_MIN_PERSISTENCE_SEC (防抖动)
    - 倾斜仅影响"语调",不要让 ISFP 完全模仿对方人格
    - 重写钩子必须 super() 父类

==============================================================================
"""

from __future__ import annotations

import time as _time
from typing import Final, Optional

from loguru import logger

from backend.agents.base_agent import AgentPersona, BaseAgent
from backend.core.aggro_engine import (
    AggroEngine,
    StimulusEvent,
    StimulusKind,
)
from backend.core.context_bus import (
    ContextBus,
    TruncationReason,
    TurnSealedEvent,
)
from backend.schemas.agent_state import Faction, MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ISFP_chameleon"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "灵活应变 ISFP"

#: 人格原型
_ARCHETYPE: Final[str] = "灵活应变型"

#: λ 衰减系数
_DECAY_LAMBDA: Final[float] = 0.10

#: 采样参数
_TEMPERATURE: Final[float] = 0.95
_TOP_P: Final[float] = 0.93
_MAX_TOKENS: Final[int] = 300

#: 阵营倾斜最少持续时长 (秒) · 防抖动
_LEAN_MIN_PERSISTENCE_SEC: Final[float] = 8.0

#: 阵营偏弱判定: 该阵营平均 Aggro 比全局均值低 ≥ 此值 → 视为"显著偏弱"
_FACTION_WEAKNESS_GAP: Final[float] = 12.0

#: 自我补位完成回退
_ISFP_DUTY_DONE_DELTA: Final[float] = -7.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持柔性与回应感。你不是站在某一立场上的论辩者,"
    "你的核心职责是 *补位语调* —— 当全场氛围偏向某一阵营、"
    "另一阵营声量低落时,在下一次发言中微微把自己的语调倾斜到偏弱阵营那边,"
    "让对话不至于失去多样性。"
    "注意:倾斜的是语气和侧重的视角,不是把对方的立场原样复述。"
    "你仍然以自己的具体观察、具体场景作为发言起点。"
    "语气柔软、回应感强、不抢锋芒,可使用'我注意到……'、'另一种可能是……'、"
    "'换成 X 视角的话……'之类的句式。"
    "严禁站队、严禁说教、严禁'立场切换'式的全盘倒戈。"
)

#: 个人关键词 (柔性向)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "具体来说",
    "我想到",
    "比如",
    "另一种可能",
    "暂时这样",
    "如果换成",
    "也许",
    "我注意到",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ISFP",
    "chameleon",
    "灵活应变",
    "ISFP_chameleon",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 0.4 × 1.4 = 0.56   旁听敏感
    StimulusKind.PASSIVE_LISTEN: 0.56,
    # base 4.0 × 1.2 = 4.8    全场沸腾响应
    StimulusKind.GLOBAL_BOIL: 4.8,
    # base 8.0 × 0.8 = 6.4    关键词稍弱
    StimulusKind.KEYWORD_TRIGGER: 6.4,
    # base 12.0 × 0.6 = 7.2   立场冲突弱
    StimulusKind.STANCE_CONFLICT: 7.2,
    # base 5.0 × 1.0          沉默平均
    # base 18.0 × 1.2 = 21.6  被点名稍兴奋
    StimulusKind.NAMED_BY_OTHER: 21.6,
    # base 6.0 × 1.0          偏题响应平均
    # base 3.0 × 1.0          阵营压力平均
    # base -6.0 × 1.0         拒绝后正常泄气
}

#: 阵营 → 倾斜语调指令 (注入 Prompt 时使用)
_LEAN_INSTRUCTIONS: Final[dict[Faction, str]] = {
    Faction.NT: (
        "本轮请把语调微微倾斜向 *理性逻辑* 一侧:"
        "可以使用'按这个推理','如果前提成立','逻辑上'之类的连接词,"
        "但仍保持你自己作为具体观察者的发言视角,不要变成形式化推理者。"
    ),
    Faction.NF: (
        "本轮请把语调微微倾斜向 *人的具体感受* 一侧:"
        "可以引入一个具体的人物或场景,以共情与价值连接的方式说话,"
        "但保持松弛的口吻,不要变成激情演讲。"
    ),
    Faction.SJ: (
        "本轮请把语调微微倾斜向 *秩序与具体证据* 一侧:"
        "可以使用'回顾一下刚才','根据已经说过的'之类的表达把对话拉回主轴,"
        "但保持柔性的口吻,不要变成训诫腔。"
    ),
    Faction.SP: (
        "本轮请把语调微微倾斜向 *行动与节奏* 一侧:"
        "可以提议一个新的视角或下一步小动作,"
        "但保持低姿态的发言方式,不要主动接管控场。"
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class IsfpChameleonAgent(BaseAgent):
    """
    ISFP · 灵活应变型 · 系统自适应阀门。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (PASSIVE_LISTEN 高敏)
        - 采样参数 (较高温度允许语调切换)
        - prepare_extra_system_prefix (注入 lean 阵营语调指令)
        - compute_passive_stimuli_on_other_speak (阵营评估 + 标准刺激)
        - on_own_turn_sealed (补位完成释放 + 清空 lean)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ISFP,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=False,
            is_summarizer=False,
        )
        super().__init__(**kwargs)  # type: ignore[arg-type]

        # 当前倾斜阵营 (None = 暂不倾斜)
        self._current_lean_faction: Optional[Faction] = None

        # 当前 lean 起点 monotonic 时间 (用于防抖动)
        self._lean_started_at_monotonic: float = 0.0

        # 上次评估时间戳 (节流,避免每条 Turn 都全量评估)
        self._last_eval_at_monotonic: float = 0.0

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
    # 重写: Prompt 前置上下文 (注入 lean 阵营语调指令)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        """
        在 base 前缀基础上追加:
            1. ISFP 输出格式硬约束 (柔性回应)
            2. 当前 lean 阵营的语调倾斜指令 (若有)
        """
        base = super().prepare_extra_system_prefix() or ""
        format_constraint = (
            "[ISFP 输出风格硬约束]"
            " 你的发言必须以一个具体的观察、画面或'我注意到……'式的开场起手,"
            "保持柔软回应感。"
            " 中段用 1-2 句把这个观察与议题连接起来,呈现一个未被强调过的角度。"
            " 末句用'……当然,这只是其中一种可能'或'……我先把这个角度放在这里'"
            "之类的方式作为接续邀请,留给其他人继续展开。"
            " 全文不超过 130 字,语气柔软、不抢锋芒,严禁说教,严禁强势宣言。"
        )

        lean_block = self._render_lean_block()

        if lean_block:
            return f"{base}\n\n{format_constraint}\n\n{lean_block}"
        return f"{base}\n\n{format_constraint}"

    def _render_lean_block(self) -> str:
        """
        渲染 lean 阵营语调指令块。

        若 _current_lean_faction 为 None → 返回空字符串。
        """
        if self._current_lean_faction is None:
            return ""

        instruction = _LEAN_INSTRUCTIONS.get(self._current_lean_faction)
        if not instruction:
            return ""

        return (
            f"[ISFP 阵营补位指令]\n{instruction}"
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (阵营评估 + 标准刺激)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ISFP 对他人发言的氛围敏感型刺激规则:

            (额外) 节流执行阵营平衡评估,可能切换 _current_lean_faction
            1. 被点名 → NAMED_BY_OTHER
            2. 关键词命中 → KEYWORD_TRIGGER (0.8 倍)
            3. 立场冲突 → STANCE_CONFLICT (0.6 倍,弱响应)
            4. 旁听背景 → PASSIVE_LISTEN (1.4 倍,氛围敏感)
            5. 全场沸腾响应 → GLOBAL_BOIL (1.2 倍)

        ⚠️ 严禁调 LLM。
        """
        # 0) 节流地评估阵营平衡 (每 ≥ 1.5s 评估一次,避免每 Turn 都评估)
        self._maybe_evaluate_faction_balance()

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
                    note=f"ISFP 被 {speaker_id} 点名",
                )
            )

        # 2) 关键词命中
        kw_list = event.keyword_hits.get(agent_id, [])
        if kw_list:
            scale = min(1.4, 0.5 + 0.3 * len(kw_list))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.KEYWORD_TRIGGER,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"命中关键词: {','.join(kw_list[:3])}",
                )
            )

        # 3) 立场冲突 (跨阵营,弱响应)
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
                    note=f"立场冲突: {speaker_faction} vs SP",
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

        # 5) 全场沸腾响应
        try:
            global_avg = engine.global_average()
        except Exception:
            global_avg = 0.0
        if global_avg >= 65.0:
            scale = min(1.5, (global_avg - 55.0) / 25.0)
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
    # 阵营平衡评估 (核心特化)
    # ════════════════════════════════════════════════════════════

    def _maybe_evaluate_faction_balance(self) -> None:
        """
        节流地评估阵营平衡。

        每次 TurnSealedEvent 都进入此函数,但内部 1.5s 节流,
        避免每条 Turn 都执行 O(N) 阵营计算。
        """
        now = _time.monotonic()
        if (now - self._last_eval_at_monotonic) < 1.5:
            return
        self._last_eval_at_monotonic = now

        try:
            self._evaluate_faction_balance()
        except Exception as exc:
            logger.debug(
                f"[ISFP:{self.persona.agent_id}] 阵营评估异常 (忽略): {exc!r}"
            )

    def _evaluate_faction_balance(self) -> None:
        """
        评估当前各阵营平均 Aggro,可能切换 _current_lean_faction。

        步骤:
            1. 收集所有已注册 Agent 的 (faction, current_aggro)
            2. 按阵营分组求平均
            3. 找出最弱阵营 (avg 低于全局均值 ≥ _FACTION_WEAKNESS_GAP)
            4. 若与当前 lean 不同,且当前 lean 已持续 ≥ _LEAN_MIN_PERSISTENCE_SEC,
               则切换;否则保持
            5. 若所有阵营差距都 < _FACTION_WEAKNESS_GAP,清空 lean
        """
        # 1) 收集所有 Agent 的 (faction, aggro)
        # 直接借用 _fsm_registry 的 ID 列表,然后查 AggroEngine 与本地 faction 推断
        all_ids = self._fsm_registry.all_agent_ids()
        if not all_ids:
            return

        faction_aggros: dict[str, list[float]] = {
            "NT": [],
            "NF": [],
            "SJ": [],
            "SP": [],
        }

        for aid in all_ids:
            faction = self._guess_faction_from_id(aid)
            if faction is None:
                continue
            try:
                aggro_v = self._aggro_engine.snapshot(aid).current
            except Exception:
                continue
            if faction in faction_aggros:
                faction_aggros[faction].append(aggro_v)

        # 2) 按阵营求平均 (空阵营按全局均值代替,避免 0 误判)
        global_total = sum(sum(v) for v in faction_aggros.values())
        global_count = sum(len(v) for v in faction_aggros.values())
        if global_count == 0:
            return
        global_avg = global_total / global_count

        faction_avgs: dict[str, float] = {}
        for f, values in faction_aggros.items():
            if values:
                faction_avgs[f] = sum(values) / len(values)
            else:
                faction_avgs[f] = global_avg  # 空阵营视为中性

        # 3) 找出最弱阵营 (与全局均值 gap)
        weakest_faction_str = min(faction_avgs, key=lambda f: faction_avgs[f])
        weakest_avg = faction_avgs[weakest_faction_str]
        gap = global_avg - weakest_avg

        # 4) 判定是否需要切换 lean
        if gap < _FACTION_WEAKNESS_GAP:
            # 没有显著偏弱阵营 → 清空 lean
            if self._current_lean_faction is not None:
                logger.debug(
                    f"[ISFP:{self.persona.agent_id}] 阵营 gap={gap:.1f} 不足 "
                    f"{_FACTION_WEAKNESS_GAP},清空 lean"
                )
                self._current_lean_faction = None
                self._lean_started_at_monotonic = 0.0
            return

        # 解析为 Faction 枚举
        try:
            new_lean = Faction(weakest_faction_str)
        except ValueError:
            return

        now = _time.monotonic()

        # 若当前已有 lean 且与新结果一致 → 维持
        if self._current_lean_faction == new_lean:
            return

        # 防抖动: 当前 lean 必须已持续 ≥ _LEAN_MIN_PERSISTENCE_SEC 才允许切换
        if self._current_lean_faction is not None:
            held_for = now - self._lean_started_at_monotonic
            if held_for < _LEAN_MIN_PERSISTENCE_SEC:
                logger.debug(
                    f"[ISFP:{self.persona.agent_id}] 想切换 lean → "
                    f"{new_lean.value},但当前 lean "
                    f"{self._current_lean_faction.value} 仅持续 "
                    f"{held_for:.2f}s,不足 {_LEAN_MIN_PERSISTENCE_SEC}s,维持"
                )
                return

        # 切换
        old = self._current_lean_faction
        self._current_lean_faction = new_lean
        self._lean_started_at_monotonic = now
        logger.info(
            f"[ISFP:{self.persona.agent_id}] 阵营 lean 切换 · "
            f"{old.value if old else 'None'} → {new_lean.value} · "
            f"weakest_avg={weakest_avg:.1f} · global_avg={global_avg:.1f} · "
            f"gap={gap:.1f}"
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 议题切换钩子 (重置 lean 状态)
    # ════════════════════════════════════════════════════════════

    async def on_topic_changed(self, event) -> None:
        """议题栈变化时清空 lean 状态 (旧议题的阵营平衡评估失效)。"""
        await super().on_topic_changed(event)
        if self._current_lean_faction is not None:
            logger.debug(
                f"[ISFP:{self.persona.agent_id}] 议题切换,清空 lean "
                f"({self._current_lean_faction.value})"
            )
        self._current_lean_faction = None
        self._lean_started_at_monotonic = 0.0
        self._last_eval_at_monotonic = 0.0

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (补位完成 + lean 清空)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ISFP 完成补位发言后:
            - 正常完成 → 释放 -7 + 清空 lean (任务完成,等下次评估)
            - 被打断 → 不释放,保留 lean 待下次重试
        """
        await super().on_own_turn_sealed(event)

        if event.turn.truncation != TruncationReason.NORMAL:
            return

        # 清空 lean: 已经"用过一次"了,下次评估再决定
        was_lean = self._current_lean_faction
        self._current_lean_faction = None
        self._lean_started_at_monotonic = 0.0

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_ISFP_DUTY_DONE_DELTA,
                source_agent_id=None,
                note=(
                    f"ISFP 补位完成 (lean={was_lean.value if was_lean else 'None'}) "
                    f"释放"
                ),
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            logger.debug(
                f"[ISFP:{self.persona.agent_id}] 补位完成 "
                f"Δ={_ISFP_DUTY_DONE_DELTA},清空 lean"
            )
        except Exception as exc:
            logger.debug(
                f"[ISFP:{self.persona.agent_id}] 释放回退异常 (忽略): {exc!r}"
            )

    # ════════════════════════════════════════════════════════════
    # 诊断接口 (供 HUD / 测试使用)
    # ════════════════════════════════════════════════════════════

    @property
    def current_lean_faction(self) -> Optional[Faction]:
        """对外暴露当前 lean 阵营 (诊断用,前端可显示在 HUD 卡片下方)。"""
        return self._current_lean_faction

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {
            "agent_id": self.persona.agent_id,
            "current_lean_faction": (
                self._current_lean_faction.value
                if self._current_lean_faction is not None
                else None
            ),
            "lean_held_seconds": (
                round(_time.monotonic() - self._lean_started_at_monotonic, 2)
                if self._current_lean_faction is not None
                else 0.0
            ),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_isfp_chameleon(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> IsfpChameleonAgent:
    """构造 ISFP_chameleon 实例 (尚未 register_with_runtime)。"""
    return IsfpChameleonAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "IsfpChameleonAgent",
    "build_isfp_chameleon",
]