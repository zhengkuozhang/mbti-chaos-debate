"""
==============================================================================
backend/agents/enfp_jester.py
------------------------------------------------------------------------------
ENFP · 幽默解构型 (NF 阵营)

人格定位:
    解构破冰者。用错位、双关、反讽、玩笑解构对方的严肃姿态。
    当全场气氛紧绷时主动用幽默打破僵局 —— 这是 ENFP 在系统中
    *最关键的功能性角色*: 防止辩论陷入纯激烈对抗的死循环。

工程参数特化 (核心: 破冰路径):
    - λ = 0.07 (1/s)         半衰期 ≈ 9.9s,略快于 INFP
    - GLOBAL_BOIL × 2.5      全场沸腾极敏感 → 行动触发器
    - LONG_SILENCE × 1.4     长沉默也催化解构冲动
    - KEYWORD_TRIGGER × 0.6  关键词不敏感 (它不被语义勾住)
    - PASSIVE_LISTEN × 0.8   旁听不焦躁
    - STANCE_CONFLICT × 0.7  不靠激怒推动
    - NAMED_BY_OTHER × 1.3
    - temperature 1.1        高温度,鼓励脑洞
    - 破冰冷却 60s           严禁连环破冰打扰节奏
    - 抢话阈值 88 (保守,仅在自身真激动时抢)

破冰路径 (本类核心特化):
    当 ① 全场平均 Aggro ≥ 85 且 ② 自身 Aggro ≥ 50 但 < 抢话阈值 且
    ③ 距上次破冰 ≥ 60s 且 ④ 当前空闲或刚释放 → ENFP 走 SCHEDULED 路径
    主动申请发言。这是脱离 Aggro 阈值规则的 *特殊任务路径*,
    但仍由 Arbitrator 统一仲裁,接受 cooldown / debounce 约束。

⚠️ 工程铁律:
    - 破冰申请不绕过 Arbitrator (走 RequestKind.SCHEDULED)
    - 严禁连环破冰打扰节奏 (60s 冷却 + 自我跟踪)
    - 严禁人身攻击与低俗笑话 (行为锚点显式禁止)
    - 刺激计算严禁调 LLM

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
from backend.core.arbitrator import (
    CooldownActiveError,
    DebounceNotMetError,
    MicrophoneBusyError,
    MicrophoneToken,
    RequestKind,
)
from backend.core.context_bus import (
    ContextBus,
    TurnSealedEvent,
)
from backend.core.fsm import FSMState
from backend.core.llm_router import InvocationResult
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ENFP_jester"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "幽默解构 ENFP"

#: 人格原型
_ARCHETYPE: Final[str] = "幽默解构型"

#: λ 衰减系数 (1/秒)
_DECAY_LAMBDA: Final[float] = 0.07

#: 采样参数
_TEMPERATURE: Final[float] = 1.1
_TOP_P: Final[float] = 0.95
_MAX_TOKENS: Final[int] = 320

#: ENFP 特化抢话阈值 (保守一些)
_ENFP_INTERRUPT_THRESHOLD: Final[float] = 88.0

#: 破冰触发条件
_ENFP_BREAKICE_GLOBAL_AVG_THRESHOLD: Final[float] = 85.0
"""全场平均 Aggro ≥ 此值 → 破冰条件 1"""

_ENFP_BREAKICE_SELF_AGGRO_MIN: Final[float] = 50.0
"""自身 Aggro ≥ 此值 → 破冰条件 2 下界 (说明它"在听" )"""

_ENFP_BREAKICE_SELF_AGGRO_MAX: Final[float] = 85.0
"""自身 Aggro < 此值 → 破冰条件 2 上界 (≥85 走基类正常路径)"""

_ENFP_BREAKICE_COOLDOWN_SEC: Final[float] = 60.0
"""破冰冷却时长 · 严禁 60s 内连续破冰"""

#: 自我释放回退 (Aggro 单位) · 成功破冰后给自身一个负刺激
_ENFP_SELF_RELIEF_DELTA: Final[float] = -10.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持松弛感与脑洞。用错位 (把严肃话题挪到一个荒谬场景)、"
    "双关 (一词多义制造意外)、反讽 (反话正说)、自嘲 (拿自己开玩笑) "
    "去解构对方的严肃姿态。当争执紧绷时,你的任务是引入一个意外的视角"
    "让所有人都笑或愣一下,然后议题反而更清晰。"
    "严禁人身攻击,严禁带性别/地域/族群歧视的笑话,严禁低俗笑话与刻板印象。"
    "幽默永远指向情境、概念、姿态本身,而非具体某个人的人格缺陷。"
    "语气轻快、节奏带跳跃感,可使用括号附注、突然反问。"
)

#: 个人关键词 (解构向)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "其实很有意思",
    "想象一下",
    "换个角度",
    "你不觉得",
    "脑洞",
    "如果反过来",
    "意外的是",
    "套娃",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ENFP",
    "jester",
    "幽默解构",
    "ENFP_jester",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 4.0 × 2.5 = 10.0   全场沸腾极敏感 (核心触发器)
    StimulusKind.GLOBAL_BOIL: 10.0,
    # base 5.0 × 1.4 = 7.0    长沉默催化解构冲动
    StimulusKind.LONG_SILENCE: 7.0,
    # base 8.0 × 0.6 = 4.8    关键词不敏感
    StimulusKind.KEYWORD_TRIGGER: 4.8,
    # base 0.4 × 0.8 = 0.32   旁听不焦躁
    StimulusKind.PASSIVE_LISTEN: 0.32,
    # base 12.0 × 0.7 = 8.4   立场冲突不强
    StimulusKind.STANCE_CONFLICT: 8.4,
    # base 18.0 × 1.3 = 23.4  被点名兴奋
    StimulusKind.NAMED_BY_OTHER: 23.4,
    # base 6.0 × 1.0          偏题响应平均
    # base 3.0 × 1.0          阵营压力平均
    # base -6.0 × 1.0         拒绝后正常泄气
}

#: "严肃语调"检测词表 — 命中触发解构冲动 (额外 GLOBAL_BOIL)
_TOO_SERIOUS_MARKERS: Final[tuple[str, ...]] = (
    "我必须强调",
    "毫无疑问",
    "这不容置疑",
    "正本清源",
    "底线问题",
    "原则上",
    "必须明确",
    "事关",
    "本质上",
    "归根到底",
)


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class EnfpJesterAgent(BaseAgent):
    """
    ENFP · 幽默解构型 · 系统破冰者。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (GLOBAL_BOIL 极敏感)
        - 采样参数 (高温度脑洞)
        - prepare_extra_system_prefix (错位/双关起手 + 留下台阶)
        - compute_passive_stimuli_on_other_speak (沸腾敏感型刺激)
        - should_attempt_speak / should_attempt_interrupt
        - attempt_speak (新增破冰路径)
        - on_own_turn_sealed (成功破冰释放)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(self, **kwargs: object) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ENFP,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=False,
            is_summarizer=False,
        )
        super().__init__(**kwargs)  # type: ignore[arg-type]

        # 破冰冷却跟踪 (本类自维护)
        self._last_breakice_at_monotonic: float = 0.0

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
    # 重写: Prompt 前置上下文 (错位/双关起手 + 留下台阶)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        base = super().prepare_extra_system_prefix() or ""
        enfp_constraint = (
            "[ENFP 输出风格硬约束]"
            " 你的发言必须以一个错位画面、双关词或意外联想起手"
            " (例如:'其实这场争论让我想起一只猫追激光笔的样子……')。"
            " 紧接着用 1-2 句把这个意象与议题连接起来,揭示某个被严肃姿态遮蔽的角度。"
            " 最后要留一个'下台阶': 让前面持极端立场的人能笑着接过去,而不必当场认错。"
            " 严禁使用刻板印象、性别地域族群笑话、低俗段子、人身攻击。"
            " 全文不超过 130 字,可使用括号附注与轻度反问。"
        )
        return f"{base}\n\n{enfp_constraint}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (沸腾敏感型)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ENFP 对他人发言的沸腾敏感型刺激规则:

            1. 被点名 → NAMED_BY_OTHER (1.3 倍)
            2. 关键词命中 → KEYWORD_TRIGGER (0.6 倍,不敏感)
            3. 立场冲突 → STANCE_CONFLICT (0.7 倍,不靠激怒)
            4. 旁听背景 → PASSIVE_LISTEN (0.8 倍)
            5. 检测严肃语调过度 → 额外 GLOBAL_BOIL (解构冲动)
            6. 全场沸腾响应 → GLOBAL_BOIL (核心触发器)

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
                    note=f"ENFP 被 {speaker_id} 点名",
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

        # 4) 旁听背景
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 5) 检测严肃语调过度 → 额外 GLOBAL_BOIL (解构冲动)
        serious_hits = self._scan_markers(text, _TOO_SERIOUS_MARKERS)
        if serious_hits:
            scale = min(1.5, 0.6 + 0.3 * len(serious_hits))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.GLOBAL_BOIL,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"严肃语调命中,解构冲动: {','.join(serious_hits[:3])}",
                )
            )

        # 6) 全场沸腾响应 (核心触发器)
        try:
            global_avg = engine.global_average()
        except Exception:
            global_avg = 0.0
        if global_avg >= 60.0:
            scale = min(2.5, (global_avg - 50.0) / 20.0)
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
    # 破冰触发判定
    # ════════════════════════════════════════════════════════════

    def _is_breakice_window(self) -> bool:
        """
        判定当前是否进入破冰路径窗口。

        条件 (全部满足):
            ① 全场平均 Aggro ≥ _ENFP_BREAKICE_GLOBAL_AVG_THRESHOLD (85)
            ② 自身 Aggro 在 [_ENFP_BREAKICE_SELF_AGGRO_MIN, _MAX] 区间
            ③ 距上次破冰 ≥ _ENFP_BREAKICE_COOLDOWN_SEC (60s)
            ④ FSM = LISTENING
            ⑤ 当前麦克风空闲 (有持麦者就走基类抢话路径)
        """
        if not self.is_registered:
            return False

        try:
            if self._fsm is None or self._fsm.state != FSMState.LISTENING:
                return False
            if self._arbitrator.current_holder() is not None:
                return False
            global_avg = self._aggro_engine.global_average()
            self_aggro = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False

        if global_avg < _ENFP_BREAKICE_GLOBAL_AVG_THRESHOLD:
            return False
        if not (
            _ENFP_BREAKICE_SELF_AGGRO_MIN
            <= self_aggro
            < _ENFP_BREAKICE_SELF_AGGRO_MAX
        ):
            return False

        cooldown_passed = (
            _time.monotonic() - self._last_breakice_at_monotonic
        ) >= _ENFP_BREAKICE_COOLDOWN_SEC
        if not cooldown_passed:
            return False

        return True

    # ════════════════════════════════════════════════════════════
    # 重写: 申请发言 / 抢话条件
    # ════════════════════════════════════════════════════════════

    def should_attempt_speak(self) -> bool:
        """
        ENFP 申请发言条件:
            (a) 基类正常路径 (Aggro ≥ 阈值且持续越线)
            或
            (b) 破冰路径 (_is_breakice_window 为真,无视 Aggro 阈值)
        """
        if super().should_attempt_speak():
            return True
        return self._is_breakice_window()

    def should_attempt_interrupt(self) -> bool:
        """
        ENFP 抢话条件 (保守):
            - Aggro ≥ _ENFP_INTERRUPT_THRESHOLD (88)
            - 通过基类常规校验
            - 注意: 破冰路径不走 INTERRUPT,仅在无人持麦时走 SCHEDULED
        """
        if not self.is_registered:
            return False
        try:
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
        except Exception:
            return False
        if current < _ENFP_INTERRUPT_THRESHOLD:
            return False
        return super().should_attempt_interrupt()

    # ════════════════════════════════════════════════════════════
    # 重写: attempt_speak (新增破冰路径)
    # ════════════════════════════════════════════════════════════

    async def attempt_speak(
        self,
        *,
        request_kind: RequestKind = RequestKind.AGGRO_THRESHOLD,
        force_token: Optional[MicrophoneToken] = None,
    ) -> Optional[InvocationResult]:
        """
        ENFP 的 attempt_speak 重写:

        当 request_kind == AGGRO_THRESHOLD 时,优先检查是否进入破冰窗口。
        若是 → 改用 RequestKind.SCHEDULED 路径申请,并记录破冰时间戳。

        其他路径 (INTERRUPT / VACUUM_FILL / SCHEDULED 直接传入 / force_token)
        全部直接交给基类处理。
        """
        # 仅当上层调度为 AGGRO_THRESHOLD 时介入 (其他路径已是明确意图)
        if request_kind == RequestKind.AGGRO_THRESHOLD and force_token is None:
            if self._is_breakice_window():
                logger.info(
                    f"[ENFP:{self.persona.agent_id}] 进入破冰路径 · "
                    f"global_avg={self._aggro_engine.global_average():.1f}"
                )
                # 走 SCHEDULED 路径 (Arbitrator 不强制 debounce 校验)
                # 但仍受 cooldown 与基础守门约束
                result = await super().attempt_speak(
                    request_kind=RequestKind.SCHEDULED,
                )
                # 记录破冰时间 (无论成功失败,避免连环触发)
                self._last_breakice_at_monotonic = _time.monotonic()
                return result

        # 非破冰路径 → 走基类标准流程
        return await super().attempt_speak(
            request_kind=request_kind,
            force_token=force_token,
        )

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (成功破冰释放)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ENFP 成功发言后给自身 -10 Aggro (任务完成,松弛)。
        被打断时几乎不受影响 (只损失一半释放量,因为破冰被打断也不算羞辱)。
        """
        await super().on_own_turn_sealed(event)

        from backend.core.context_bus import TruncationReason

        # 计算释放量
        if event.turn.truncation == TruncationReason.NORMAL:
            delta = _ENFP_SELF_RELIEF_DELTA  # -10
        elif event.turn.truncation == TruncationReason.INTERRUPTED:
            # 被打断也释放,但只一半 (ENFP 心态好,不报复)
            delta = _ENFP_SELF_RELIEF_DELTA / 2.0  # -5
        else:
            # 看门狗 / 系统中止: 不主动释放
            return

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=delta,
                source_agent_id=None,
                note=(
                    "ENFP 发言后释放"
                    if event.turn.truncation == TruncationReason.NORMAL
                    else "ENFP 被打断,仍小幅释放"
                ),
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            logger.debug(
                f"[ENFP:{self.persona.agent_id}] 发言后释放 Δ={delta}"
            )
        except Exception as exc:
            logger.debug(
                f"[ENFP:{self.persona.agent_id}] 释放回退异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_enfp_jester(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
) -> EnfpJesterAgent:
    """构造 ENFP_jester 实例 (尚未 register_with_runtime)。"""
    return EnfpJesterAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "EnfpJesterAgent",
    "build_enfp_jester",
]