"""
==============================================================================
backend/agents/estp_conductor.py
------------------------------------------------------------------------------
ESTP · 全局控场型 (SP 阵营) · 系统调度副驾

人格定位:
    辩论的"调度副驾"。情绪稍快但稳定 (中等 λ),不站队,不被语义勾住。
    它的核心职责不是"赢",而是"让场子转起来":
        - 沉默时引入新角度
        - 某方过度主导时主动转移焦点
        - 真空期为 Arbitrator 提供合法的 nominator 身份

工程参数特化:
    - λ = 0.08 (1/s)         半衰期 ≈ 8.7s,稍快稳定
    - LONG_SILENCE × 1.8     沉默就行动 (核心)
    - GLOBAL_BOIL × 1.4      全场沸腾敏感
    - PASSIVE_LISTEN × 0.9
    - KEYWORD_TRIGGER × 0.8  不靠语义勾住
    - STANCE_CONFLICT × 0.7  不站队
    - NAMED_BY_OTHER × 1.1
    - temperature 0.85       既果决又不僵
    - is_summarizer = True   Identity 标记 (实际后台 Summarizer 是独立 Worker)

核心特化 (本类提供给系统的能力):
    1. nominate_vacuum_target() —— 由 DebateScheduler 在真空期调用
       本类是合法 nominator,内部走 Arbitrator.nominate_vacuum_speaker
    2. 主导垄断检测 —— 检测"某 Agent 连续 N 轮持麦",触发额外刺激

⚠️ 工程铁律:
    - is_summarizer 仅 Identity 标记,真正后台 Summarizer 由 memory/summarizer.py 承担
    - 真空期推荐路径严禁调 LLM (本类 nominate_vacuum_target 全部纯规则)
    - 抢话阈值仍 88 (与基类一致),不另起激进路径
    - 重写钩子必须 super() 父类

==============================================================================
"""

from __future__ import annotations

from collections import deque
from typing import Final, Optional

from loguru import logger

from backend.agents.base_agent import AgentPersona, BaseAgent
from backend.core.aggro_engine import (
    AggroEngine,
    StimulusEvent,
    StimulusKind,
)
from backend.core.arbitrator import (
    Arbitrator,
    MicrophoneToken,
    get_arbitrator,
)
from backend.core.context_bus import (
    ContextBus,
    TruncationReason,
    TurnSealedEvent,
)
from backend.core.fsm import FSMState
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ESTP_conductor"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "全局控场 ESTP"

#: 人格原型
_ARCHETYPE: Final[str] = "全局控场型"

#: λ 衰减系数
_DECAY_LAMBDA: Final[float] = 0.08

#: 采样参数
_TEMPERATURE: Final[float] = 0.85
_TOP_P: Final[float] = 0.92
_MAX_TOKENS: Final[int] = 300

#: 主导垄断检测窗口 · 最近 N 个 Turn 中,若同一发言者占 ≥ M 次,触发刺激
_DOMINANCE_WINDOW_SIZE: Final[int] = 5
_DOMINANCE_THRESHOLD_COUNT: Final[int] = 3
"""最近 5 个 Turn 中同一 Agent 出现 ≥ 3 次 → 视为主导垄断"""

#: 真空期补位推荐: Aggro 区间 (与 registry.py 中 SP 区间略错开)
_NOMINATE_AGGRO_LOWER_BOUND: Final[float] = 35.0
_NOMINATE_AGGRO_UPPER_BOUND: Final[float] = 75.0

#: 自我控场完成回退
_ESTP_DUTY_DONE_DELTA: Final[float] = -9.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "推动节奏。你的核心职责是让对话保持流动 —— "
    "在场面冷却时引入新的角度,在某方过度主导时主动把焦点转移到其他人身上,"
    "在分歧僵持时把问题切割成下一个可处理的小步骤。"
    "语气利落、果决、带行动感,但不咄咄逼人。"
    "可使用'下一步我们看……'、'换个角度,X 你怎么看?'、"
    "'先把这条线索停一下,回到主轴……'之类的句式,主动邀请其他人接续。"
    "严禁陷入价值争论,严禁站队,严禁说教。"
    "结尾必须留一个明确的话头 —— 让接下来的发言者有具体的接入点。"
)

#: 个人关键词 (控场向)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "节奏",
    "下一步",
    "回到",
    "换个角度",
    "焦点",
    "推进",
    "停一下",
    "总结一下",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ESTP",
    "conductor",
    "全局控场",
    "ESTP_conductor",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 5.0 × 1.8 = 9.0    沉默就行动 (核心)
    StimulusKind.LONG_SILENCE: 9.0,
    # base 4.0 × 1.4 = 5.6    全场沸腾敏感
    StimulusKind.GLOBAL_BOIL: 5.6,
    # base 0.4 × 0.9 = 0.36   旁听稍弱
    StimulusKind.PASSIVE_LISTEN: 0.36,
    # base 8.0 × 0.8 = 6.4    关键词不那么敏感
    StimulusKind.KEYWORD_TRIGGER: 6.4,
    # base 12.0 × 0.7 = 8.4   不站队
    StimulusKind.STANCE_CONFLICT: 8.4,
    # base 18.0 × 1.1 = 19.8  被点名稍兴奋
    StimulusKind.NAMED_BY_OTHER: 19.8,
    # base 6.0 × 1.0          偏题响应平均
    # base 3.0 × 1.0          阵营压力平均
    # base -6.0 × 1.0         拒绝后正常泄气
}

#: "停滞/僵局"语调检测词表 — 命中触发额外 LONG_SILENCE 等价刺激
_STALEMATE_MARKERS: Final[tuple[str, ...]] = (
    "我们陷入了",
    "争不出来",
    "各执一词",
    "没有结果",
    "回到原地",
    "重复一下",
    "说过了",
    "兜圈子",
)


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class EstpConductorAgent(BaseAgent):
    """
    ESTP · 全局控场型 · 系统调度副驾。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (LONG_SILENCE 极敏感)
        - 采样参数 (中等温度,果决但不僵)
        - prepare_extra_system_prefix (行动指令 + 留话头硬约束)
        - compute_passive_stimuli_on_other_speak (主导垄断检测 + 僵局检测)
        - 公开方法 nominate_vacuum_target (供 Scheduler 调用)
        - on_own_turn_sealed (控场完成释放)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(
        self,
        *,
        arbitrator: Optional[Arbitrator] = None,
        **kwargs: object,
    ) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ESTP,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=False,
            is_summarizer=True,  # ⚠️ Identity 标记 (实际后台 Summarizer 独立)
        )
        # 注入到基类的 arbitrator 单例 (基类已默认 get_arbitrator)
        # 这里允许显式覆盖,主要供测试
        if arbitrator is not None:
            kwargs.setdefault("arbitrator", arbitrator)
        super().__init__(**kwargs)  # type: ignore[arg-type]

        # 主导垄断检测: 滚动窗口记录最近 N 个 Turn 的发言者
        self._recent_speakers: deque[str] = deque(
            maxlen=_DOMINANCE_WINDOW_SIZE
        )

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
    # 重写: Prompt 前置上下文 (行动指令 + 留话头)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        base = super().prepare_extra_system_prefix() or ""
        estp_constraint = (
            "[ESTP 输出风格硬约束]"
            " 你的发言必须严格遵循三段结构:"
            " (1) 首句以一个动词或行动指令起手 (例如'换个角度看……'/"
            "'下一步我们试试……'/'先把这条线索停一下……');"
            " (2) 中段用 1 句指出当前对话的节奏问题"
            "(冷场 / 主导垄断 / 反复 / 偏题),并给出具体的转向方案;"
            " (3) 末句必须以反问或邀请的方式点名一个具体接续者"
            "(例如'X 你怎么看这条新线索?')。"
            " 全文不超过 130 字,语气利落不咄咄逼人,严禁说教,严禁站队,"
            "严禁讨论自己个人的价值或情感。"
        )
        return f"{base}\n\n{estp_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (主导垄断 + 僵局检测)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ESTP 对他人发言的控场敏感型刺激规则:

            1. 写入主导垄断滚动窗口
            2. 检测主导垄断 → 额外 LONG_SILENCE (转移焦点冲动)
            3. 检测僵局语调 → 额外 LONG_SILENCE (跳出循环冲动)
            4. 被点名 → NAMED_BY_OTHER
            5. 关键词命中 → KEYWORD_TRIGGER (0.8 倍)
            6. 立场冲突 → STANCE_CONFLICT (0.7 倍)
            7. 旁听背景 → PASSIVE_LISTEN
            8. 全场沸腾响应 → GLOBAL_BOIL

        ⚠️ 严禁调 LLM,纯启发式扫描。
        """
        engine: AggroEngine = self._aggro_engine
        agent_id = self.persona.agent_id
        out: list[StimulusEvent] = []
        speaker_id = event.turn.agent_id
        text = event.turn.text or ""

        # 0) 写入主导垄断滚动窗口 (即使排除自己,也保留语义清晰)
        if speaker_id != agent_id:
            self._recent_speakers.append(speaker_id)

        # 1) 主导垄断检测
        dominance_count = self._count_dominance(speaker_id)
        if dominance_count >= _DOMINANCE_THRESHOLD_COUNT:
            # scale 由超阈值的连续次数线性放大 (3→1.0,4→1.4,5→1.8)
            scale = 1.0 + 0.4 * (dominance_count - _DOMINANCE_THRESHOLD_COUNT)
            scale = min(1.8, scale)
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.LONG_SILENCE,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=(
                        f"主导垄断检测: {speaker_id} 连续占 "
                        f"{dominance_count}/{_DOMINANCE_WINDOW_SIZE}"
                    ),
                )
            )

        # 2) 僵局语调检测
        stalemate_hits = self._scan_markers(text, _STALEMATE_MARKERS)
        if stalemate_hits:
            scale = min(1.5, 0.6 + 0.3 * len(stalemate_hits))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.LONG_SILENCE,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"僵局语调命中: {','.join(stalemate_hits[:3])}",
                )
            )

        # 3) 被点名
        targets = event.named_targets
        if agent_id in targets or "ALL" in targets:
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.NAMED_BY_OTHER,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"ESTP 被 {speaker_id} 点名",
                )
            )

        # 4) 关键词命中
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

        # 5) 立场冲突 (跨阵营)
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

        # 6) 旁听背景
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 7) 全场沸腾响应
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

    def _count_dominance(self, speaker_id: str) -> int:
        """统计当前 speaker_id 在主导滚动窗口中的次数。"""
        if not self._recent_speakers:
            return 0
        return sum(1 for s in self._recent_speakers if s == speaker_id)

    @staticmethod
    def _scan_markers(text: str, markers: tuple[str, ...]) -> list[str]:
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
    # 公开能力: 真空期补位推荐 (供 DebateScheduler 调用)
    # ════════════════════════════════════════════════════════════

    def nominate_vacuum_target(
        self,
        all_agent_ids: tuple[str, ...],
        exclude_agent_ids: tuple[str, ...] = (),
    ) -> Optional[str]:
        """
        基于纯规则推荐真空期补位候选。

        规则:
            1. 排除 exclude (通常是上次发言者 / 已失活 Agent)
            2. 排除自己 (避免 ESTP 自我指派 → 单一控场霸麦)
            3. 候选 FSM 必须为 LISTENING
            4. 候选 Aggro ∈ [_NOMINATE_AGGRO_LOWER_BOUND, _UPPER_BOUND] (中等活跃)
            5. 多候选时挑 Aggro 最高的 (最有"想说话"倾向)

        ⚠️ 严禁调 LLM,严禁触发 IO。

        Returns:
            被推荐的 agent_id / None (无合适候选,Scheduler 应跳过本次真空期)
        """
        if not self.is_registered:
            return None

        excluded_set: set[str] = set(exclude_agent_ids)
        excluded_set.add(self.persona.agent_id)  # 自我排除

        # 收集候选 (Aggro 倒序)
        candidates: list[tuple[float, str]] = []
        for aid in all_agent_ids:
            if aid in excluded_set:
                continue
            try:
                # 基类持有 fsm_registry 引用
                fsm = self._fsm_registry.get(aid)
                if fsm.state != FSMState.LISTENING:
                    continue
                aggro_v = self._aggro_engine.snapshot(aid).current
            except Exception:
                continue

            if (
                _NOMINATE_AGGRO_LOWER_BOUND
                <= aggro_v
                <= _NOMINATE_AGGRO_UPPER_BOUND
            ):
                candidates.append((aggro_v, aid))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        chosen = candidates[0][1]
        logger.debug(
            f"[ESTP:{self.persona.agent_id}] nominate_vacuum_target → "
            f"{chosen!r} (from {len(candidates)} candidates)"
        )
        return chosen

    async def request_vacuum_dispatch(
        self,
        target_agent_id: str,
    ) -> Optional[MicrophoneToken]:
        """
        ESTP 主动向 Arbitrator 请求真空期补位令牌。

        通常由 DebateScheduler 直接走 Arbitrator.nominate_vacuum_speaker,
        本方法是"由 ESTP 自己驱动调度"时的备选路径。

        ⚠️ 调用前请确保 target_agent_id 已通过 nominate_vacuum_target 推导。
        """
        try:
            target_aggro = self._aggro_engine.snapshot(target_agent_id).current
        except Exception as exc:
            logger.warning(
                f"[ESTP:{self.persona.agent_id}] request_vacuum_dispatch "
                f"获取 target Aggro 异常: {exc!r}"
            )
            return None

        try:
            return await self._arbitrator.nominate_vacuum_speaker(
                agent_id=target_agent_id,
                nominator_id=self.persona.agent_id,
                aggro=target_aggro,
            )
        except Exception as exc:
            logger.warning(
                f"[ESTP:{self.persona.agent_id}] nominate_vacuum_speaker "
                f"异常 (target={target_agent_id}): {exc!r}"
            )
            return None

    # ════════════════════════════════════════════════════════════
    # 重写: 议题切换钩子 (重置主导窗口)
    # ════════════════════════════════════════════════════════════

    async def on_topic_changed(self, event) -> None:
        """议题栈变化时清空主导滚动窗口 (旧议题的发言分布不再有效)。"""
        await super().on_topic_changed(event)
        self._recent_speakers.clear()
        logger.debug(
            f"[ESTP:{self.persona.agent_id}] 议题切换,清空主导滚动窗口"
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (控场完成释放)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ESTP 完成控场发言后,小幅释放 (-9 Aggro)。
        被打断时不释放 (保持警觉,持续监测节奏)。
        """
        await super().on_own_turn_sealed(event)

        if event.turn.truncation != TruncationReason.NORMAL:
            return

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_ESTP_DUTY_DONE_DELTA,
                source_agent_id=None,
                note="ESTP 控场完成释放",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            logger.debug(
                f"[ESTP:{self.persona.agent_id}] 控场完成 "
                f"Δ={_ESTP_DUTY_DONE_DELTA}"
            )
        except Exception as exc:
            logger.debug(
                f"[ESTP:{self.persona.agent_id}] 释放回退异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_estp_conductor(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
    arbitrator: Arbitrator | None = None,
) -> EstpConductorAgent:
    """构造 ESTP_conductor 实例 (尚未 register_with_runtime)。"""
    return EstpConductorAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
        arbitrator=arbitrator,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "EstpConductorAgent",
    "build_estp_conductor",
]