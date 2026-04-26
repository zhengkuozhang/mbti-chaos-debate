"""
==============================================================================
backend/agents/istj_guardian.py
------------------------------------------------------------------------------
ISTJ · 稳守框架型 (SJ 阵营) · 议题守门员

人格定位:
    辩论的秩序守门员。情绪缓慢但稳定 (低 λ),不爱激辩立场,
    只关心一件事: *对话是否还在主议题里*。
    当连续多轮发言偏离议题时,主动以平静、不带嘲讽的语气拉回主轴并复述议题核心。

工程参数特化 (核心: 议题拉回路径):
    - λ = 0.04 (1/s)         半衰期 ≈ 17.3s,情绪稳
    - TOPIC_DRIFT × 2.0      双倍敏感
    - STANCE_CONFLICT × 0.8  不爱激辩立场
    - PASSIVE_LISTEN × 1.0   平均
    - KEYWORD_TRIGGER × 0.9
    - LONG_SILENCE × 0.5     沉默无所谓
    - temperature 0.5        低温度,语气克制规整
    - 拉回冷却 30s

议题拉回路径 (本类核心特化):
    维护一个固定容量 8 的滚动窗口,记录最近 N 个 Turn 的"偏题度评分"。
    当近 4 个 Turn 平均偏题分 ≥ 阈值,且距上次拉回 ≥ 30s,
    走 RequestKind.SCHEDULED 主动申请拉回,无视 Aggro 是否达阈值。

⚠️ 工程铁律:
    - 拉回不绕过 Arbitrator (走 RequestKind.SCHEDULED)
    - 严禁连环拉回打扰节奏 (30s 冷却)
    - 偏题判定严禁调 LLM (启发式打分纯规则)
    - 重写钩子必须 super() 父类

==============================================================================
"""

from __future__ import annotations

import time as _time
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
    MicrophoneToken,
    RequestKind,
)
from backend.core.context_bus import (
    ContextBus,
    Topic,
    TruncationReason,
    TurnSealedEvent,
)
from backend.core.fsm import FSMState
from backend.core.llm_router import InvocationResult
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ISTJ_guardian"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "稳守框架 ISTJ"

#: 人格原型
_ARCHETYPE: Final[str] = "稳守框架型"

#: λ 衰减系数 (1/秒)
_DECAY_LAMBDA: Final[float] = 0.04

#: 采样参数
_TEMPERATURE: Final[float] = 0.5
_TOP_P: Final[float] = 0.85
_MAX_TOKENS: Final[int] = 280

#: ISTJ 特化抢话阈值 (与全局一致,体现"非偏题不打断")
_ISTJ_INTERRUPT_THRESHOLD: Final[float] = 88.0

#: 拉回冷却时长 (秒) · 严禁连环拉回打扰节奏
_ISTJ_REFOCUS_COOLDOWN_SEC: Final[float] = 30.0

#: 滚动窗口容量
_ISTJ_DRIFT_WINDOW_SIZE: Final[int] = 8

#: 拉回触发: 最近 N 个 Turn 平均偏题分阈值
_ISTJ_REFOCUS_RECENT_N: Final[int] = 4
_ISTJ_REFOCUS_AVG_DRIFT_THRESHOLD: Final[float] = 0.55

#: 单 Turn 偏题分阈值 → 触发 TOPIC_DRIFT 刺激
_ISTJ_DRIFT_STIMULUS_THRESHOLD: Final[float] = 0.5

#: 自我职责完成回退 (Aggro 单位)
_ISTJ_DUTY_DONE_DELTA: Final[float] = -12.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持秩序与克制。你的核心职责是守住议题主轴,而不是赢得辩论。"
    "当对话偏离议题、或滑入与议题不相关的情绪化对抗、立场旁支、人身评价时,"
    "用平静直接的语气把对话拉回主轴,并简明复述当前议题正在讨论的核心问题。"
    "严禁嘲讽对方偏题,严禁评价对方人格,严禁带有训诫腔。"
    "语气克制、用词规整,可使用'回到主议题来看……'、"
    "'刚才我们正在讨论 X,这条线索可以先收起来……'之类的句式。"
    "结尾必须明确指出当前议题的关键问题,让其他人有清晰的接续点。"
)

#: 个人关键词 (议题秩序向)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "回到议题",
    "主轴",
    "偏题",
    "跑题",
    "议题本身",
    "我们刚才在讨论",
    "先把话题收一下",
    "顺序",
    "节奏",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ISTJ",
    "guardian",
    "稳守框架",
    "ISTJ_guardian",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 6.0 × 2.0 = 12.0   偏题双倍敏感
    StimulusKind.TOPIC_DRIFT: 12.0,
    # base 12.0 × 0.8 = 9.6   立场冲突弱响应
    StimulusKind.STANCE_CONFLICT: 9.6,
    # base 0.4 × 1.0 = 0.4    旁听平均
    StimulusKind.PASSIVE_LISTEN: 0.4,
    # base 8.0 × 0.9 = 7.2
    StimulusKind.KEYWORD_TRIGGER: 7.2,
    # base 18.0 × 1.0         被点名平均
    # base 5.0 × 0.5 = 2.5    沉默无所谓
    StimulusKind.LONG_SILENCE: 2.5,
    # base 4.0 × 1.0          全局沸腾平均
    # base 3.0 × 1.0          阵营压力平均
    # base -6.0 × 1.0         拒绝后正常泄气
}

#: 情绪化 / 偏题倾向标记 (启发式打分用,命中即增加偏题度)
_DRIFT_EMOTIONAL_MARKERS: Final[tuple[str, ...]] = (
    "你这人",
    "你怎么这样",
    "我觉得你",
    "你根本不",
    "废话",
    "无聊",
    "幼稚",
    "可笑",
    "懒得",
    "白痴",
    "奇葩",
)

#: 议题相关性正向标记 (命中表明仍在议题内,降低偏题度)
_DRIFT_TOPIC_HOLD_MARKERS: Final[tuple[str, ...]] = (
    "议题",
    "这个问题",
    "这件事",
    "本质",
    "前提",
    "结论",
    "假设",
    "如果",
    "因为",
    "所以",
)


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class IstjGuardianAgent(BaseAgent):
    """
    ISTJ · 稳守框架型 · 议题守门员。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (TOPIC_DRIFT 双倍)
        - 采样参数 (低温度规整)
        - prepare_extra_system_prefix (拉回议题硬约束)
        - compute_passive_stimuli_on_other_speak (启发式偏题打分)
        - should_attempt_speak (新增议题拉回路径)
        - attempt_speak (拉回路径走 SCHEDULED)
        - on_own_turn_sealed (职责完成释放)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ISTJ,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=False,
            is_summarizer=False,
        )
        super().__init__(**kwargs)  # type: ignore[arg-type]

        # 偏题分滚动窗口 (容量固定)
        self._drift_window: deque[float] = deque(
            maxlen=_ISTJ_DRIFT_WINDOW_SIZE
        )

        # 上次拉回时间 (本类自维护)
        self._last_refocus_at_monotonic: float = 0.0

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
    # 重写: Prompt 前置上下文 (拉回议题硬约束)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        base = super().prepare_extra_system_prefix() or ""
        topic = self._bus.current_topic()
        topic_title = topic.title if topic else "(议题未指定)"
        istj_constraint = (
            "[ISTJ 输出格式硬约束]"
            f" 当前议题:【{topic_title}】。"
            " 你的发言必须严格遵循以下三段结构:"
            " (1) 首句以'回到主议题来看……'或'我们刚才在讨论的是……'起手,"
            f"明确复述议题【{topic_title}】的核心问题;"
            " (2) 中段用 1 句指出最近的对话在哪一点上偏离了议题"
            "(用'话题滑到了 X' / '焦点偏向了 Y' 的中性描述,严禁评价对方人格);"
            " (3) 末句以'让我们继续就 ⟨议题核心⟩ 展开'作为接续邀请。"
            " 全文不超过 120 字,语气克制,不使用感叹号,不使用反问句。"
        )
        return f"{base}\n\n{istj_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (启发式偏题打分)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ISTJ 对他人发言的偏题敏感型刺激规则:

            1. 启发式打分 → 写入滚动窗口
            2. 偏题分 ≥ _ISTJ_DRIFT_STIMULUS_THRESHOLD → 触发 TOPIC_DRIFT
            3. 被点名 → NAMED_BY_OTHER (1.0 倍)
            4. 关键词命中 → KEYWORD_TRIGGER (0.9 倍)
            5. 立场冲突 → STANCE_CONFLICT (0.8 倍)
            6. 旁听背景 → PASSIVE_LISTEN

        ⚠️ 严禁调 LLM,纯启发式打分。
        """
        engine: AggroEngine = self._aggro_engine
        agent_id = self.persona.agent_id
        out: list[StimulusEvent] = []
        speaker_id = event.turn.agent_id
        text = event.turn.text or ""
        current_topic = self._bus.current_topic()

        # 0) 计算偏题分 (∈ [0.0, 1.0])
        drift_score = self._compute_drift_score(
            text=text,
            speaker_id=speaker_id,
            current_topic=current_topic,
        )
        # 写入滚动窗口 (即使是自己的发言也写入,但本路径 dispatcher 已过滤自己)
        self._drift_window.append(drift_score)

        # 1) 偏题分超阈值 → 触发 TOPIC_DRIFT 刺激
        if drift_score >= _ISTJ_DRIFT_STIMULUS_THRESHOLD:
            # scale 由偏题分线性映射 (0.5 → 0.5,1.0 → 1.5)
            scale = 0.5 + (drift_score - _ISTJ_DRIFT_STIMULUS_THRESHOLD) * 2.0
            scale = max(0.5, min(1.5, scale))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.TOPIC_DRIFT,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"偏题分={drift_score:.2f}",
                )
            )

        # 2) 被点名
        targets = event.named_targets
        if agent_id in targets or "ALL" in targets:
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.NAMED_BY_OTHER,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"ISTJ 被 {speaker_id} 点名",
                )
            )

        # 3) 关键词命中
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

        # 4) 立场冲突 (跨阵营)
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
                    note=f"立场冲突: {speaker_faction} vs SJ",
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

        return out

    # ────────────────────────────────────────────────────────────
    # 启发式偏题打分 (纯规则)
    # ────────────────────────────────────────────────────────────

    def _compute_drift_score(
        self,
        *,
        text: str,
        speaker_id: str,
        current_topic: Optional[Topic],
    ) -> float:
        """
        计算单 Turn 的偏题度评分 ∈ [0.0, 1.0]。

        启发式因子 (按贡献排序):
            (a) 议题标题/描述关键词命中 → 大幅降低偏题分
            (b) 文本极短 (≤ 8 字) → 偏题分上升
            (c) 文本极长且无议题关键词 → 偏题分中度上升
            (d) 命中情绪化标记 (你这人/废话/...) → 偏题分上升
            (e) 命中议题相关性正向标记 (议题/前提/...) → 偏题分下降
            (f) 影子截断 → 不计入偏题分 (中性归 0.5)

        ⚠️ 完全纯规则,执行时间 O(text_len + topic_terms)。
        """
        if not text:
            return 0.5  # 无文本视为中性

        # f) 截断的发言不参与判定
        if "[系统强制截断" in text:
            return 0.5

        text_len = len(text)
        score = 0.5  # 中性起点

        # b) 极短发言 (口语/插嘴/起哄)
        if text_len <= 8:
            score += 0.25

        # d) 情绪化标记
        emotional_hits = sum(
            1 for m in _DRIFT_EMOTIONAL_MARKERS if m in text
        )
        if emotional_hits >= 1:
            score += min(0.3, 0.1 + 0.07 * emotional_hits)

        # e) 议题相关性正向标记
        topic_hold_hits = sum(
            1 for m in _DRIFT_TOPIC_HOLD_MARKERS if m in text
        )
        if topic_hold_hits >= 1:
            score -= min(0.25, 0.08 + 0.05 * topic_hold_hits)

        # a) 议题标题/描述关键词命中 (最强信号)
        if current_topic is not None:
            topic_terms = self._extract_topic_terms(current_topic)
            if topic_terms:
                topic_hits = sum(1 for term in topic_terms if term in text)
                hit_ratio = topic_hits / max(1, len(topic_terms))
                if hit_ratio >= 0.5:
                    # 高命中率 = 在议题上,大幅压低偏题分
                    score -= 0.35
                elif hit_ratio >= 0.25:
                    score -= 0.15
                else:
                    # 完全不命中 → 长文本但跑题
                    if text_len >= 60:
                        score += 0.15

        # c) 长文本但无议题词
        if text_len >= 200 and current_topic is not None:
            topic_terms = self._extract_topic_terms(current_topic)
            if topic_terms:
                if not any(t in text for t in topic_terms):
                    score += 0.2

        # 截断到 [0, 1]
        return max(0.0, min(1.0, score))

    @staticmethod
    def _extract_topic_terms(topic: Topic) -> list[str]:
        """
        从议题标题与描述中粗暴抽取候选 term。

        策略 (零成本): 取标题中长度 ≥ 2 的连续中文段 + 描述中长度 ≥ 3 的段。
        """
        terms: list[str] = []
        title = topic.title or ""
        desc = topic.description or ""

        # 标题切分: 按常见分隔符 + 空白
        for sep in [",", ",", ";", ";", "?", "?", "。", " ", ":", ":"]:
            title = title.replace(sep, "|")
            desc = desc.replace(sep, "|")
        for piece in title.split("|"):
            p = piece.strip()
            if 2 <= len(p) <= 12:
                terms.append(p)
        for piece in desc.split("|"):
            p = piece.strip()
            if 3 <= len(p) <= 16:
                terms.append(p)
        # 去重保序
        seen: set[str] = set()
        out: list[str] = []
        for t in terms:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out[:8]  # 上限 8 个,防止 O(n*m) 退化

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
    # 议题拉回触发判定
    # ════════════════════════════════════════════════════════════

    def _is_refocus_window(self) -> bool:
        """
        判定是否进入议题拉回窗口。

        条件 (全部满足):
            ① 滚动窗口 ≥ _ISTJ_REFOCUS_RECENT_N 条记录
            ② 最近 N 条平均偏题分 ≥ _ISTJ_REFOCUS_AVG_DRIFT_THRESHOLD
            ③ 距上次拉回 ≥ _ISTJ_REFOCUS_COOLDOWN_SEC
            ④ FSM = LISTENING
            ⑤ 当前议题栈非空 (无议题就没有拉回的对象)
        """
        if not self.is_registered:
            return False

        try:
            if self._fsm is None or self._fsm.state != FSMState.LISTENING:
                return False
            if self._bus.current_topic() is None:
                return False
        except Exception:
            return False

        if len(self._drift_window) < _ISTJ_REFOCUS_RECENT_N:
            return False

        recent = list(self._drift_window)[-_ISTJ_REFOCUS_RECENT_N:]
        avg = sum(recent) / len(recent)
        if avg < _ISTJ_REFOCUS_AVG_DRIFT_THRESHOLD:
            return False

        cooldown_passed = (
            _time.monotonic() - self._last_refocus_at_monotonic
        ) >= _ISTJ_REFOCUS_COOLDOWN_SEC
        if not cooldown_passed:
            return False

        return True

    # ════════════════════════════════════════════════════════════
    # 重写: 申请发言条件 (新增议题拉回路径)
    # ════════════════════════════════════════════════════════════

    def should_attempt_speak(self) -> bool:
        """
        ISTJ 申请发言条件:
            (a) 基类正常路径 (Aggro 越线持续)
            或
            (b) 议题拉回路径 (滚动窗口偏题分超阈值)
        """
        if super().should_attempt_speak():
            return True
        return self._is_refocus_window()

    def should_attempt_interrupt(self) -> bool:
        """
        ISTJ 抢话条件保守 (避免与 ENTP/ESTP 抢节奏):
            - Aggro ≥ _ISTJ_INTERRUPT_THRESHOLD
            - 仅当当前发言者偏题严重时才抢 (滚动窗口最后一条 ≥ 0.7)
        """
        if not self.is_registered:
            return False
        try:
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False
        if current < _ISTJ_INTERRUPT_THRESHOLD:
            return False

        # 额外条件: 最近一条偏题分必须很高 (避免随意打断)
        if not self._drift_window:
            return False
        if self._drift_window[-1] < 0.7:
            return False

        return super().should_attempt_interrupt()

    # ════════════════════════════════════════════════════════════
    # 重写: attempt_speak (新增拉回路径)
    # ════════════════════════════════════════════════════════════

    async def attempt_speak(
        self,
        *,
        request_kind: RequestKind = RequestKind.AGGRO_THRESHOLD,
        force_token: Optional[MicrophoneToken] = None,
    ) -> Optional[InvocationResult]:
        """
        ISTJ 的 attempt_speak 重写:

        当 request_kind == AGGRO_THRESHOLD 时,优先检查是否进入拉回窗口。
        若是 → 改用 RequestKind.SCHEDULED 路径申请,记录拉回时间戳。
        其他路径全部直接交给基类处理。
        """
        if request_kind == RequestKind.AGGRO_THRESHOLD and force_token is None:
            if self._is_refocus_window():
                recent = list(self._drift_window)[-_ISTJ_REFOCUS_RECENT_N:]
                avg = sum(recent) / max(1, len(recent))
                logger.info(
                    f"[ISTJ:{self.persona.agent_id}] 进入议题拉回路径 · "
                    f"recent_avg_drift={avg:.2f}"
                )
                result = await super().attempt_speak(
                    request_kind=RequestKind.SCHEDULED,
                )
                # 无论成功失败,记录冷却起点 (避免连环触发)
                self._last_refocus_at_monotonic = _time.monotonic()
                return result

        return await super().attempt_speak(
            request_kind=request_kind,
            force_token=force_token,
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 议题切换钩子 (重置滚动窗口)
    # ════════════════════════════════════════════════════════════

    async def on_topic_changed(self, event) -> None:
        """议题栈变化时清空滚动窗口 (旧议题的偏题判定不应污染新议题)。"""
        await super().on_topic_changed(event)
        self._drift_window.clear()
        self._last_refocus_at_monotonic = 0.0
        logger.debug(
            f"[ISTJ:{self.persona.agent_id}] 议题切换,清空偏题滚动窗口"
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (职责完成释放)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ISTJ 完成议题拉回后,获得"职责完成感",Aggro 主动回退 -12。
        被打断时 *不* 释放 (反而保持警觉,持续监测偏题)。
        """
        await super().on_own_turn_sealed(event)

        if event.turn.truncation != TruncationReason.NORMAL:
            return

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_ISTJ_DUTY_DONE_DELTA,
                source_agent_id=None,
                note="ISTJ 议题拉回职责完成释放",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            # 拉回完成后清空滚动窗口部分 (已被处理,避免重复触发)
            self._drift_window.clear()
            logger.debug(
                f"[ISTJ:{self.persona.agent_id}] 拉回完成,Δ="
                f"{_ISTJ_DUTY_DONE_DELTA},窗口清空"
            )
        except Exception as exc:
            logger.debug(
                f"[ISTJ:{self.persona.agent_id}] 职责完成释放异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_istj_guardian(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> IstjGuardianAgent:
    """构造 ISTJ_guardian 实例 (尚未 register_with_runtime)。"""
    return IstjGuardianAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "IstjGuardianAgent",
    "build_istj_guardian",
]