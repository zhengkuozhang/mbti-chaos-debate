"""
==============================================================================
backend/agents/base_agent.py
------------------------------------------------------------------------------
Agent 抽象基类 · 8 个 MBTI 子类的共同骨架

定位:
    所有具体 MBTI Agent (INTJ/ENTP/INFP/...) 必须继承本基类。
    基类封装了以下核心模板方法,子类只需实现 *人格特化* 的钩子:

    1. 一站式 register_with_runtime() —— 注册到 FSM/AggroEngine/ContextBus
    2. 一站式 attempt_speak() —— 申请麦克风 → FSM 转 COMPUTING → 推理 → 释放
    3. 抢话去抖动状态机 —— 跟踪 Aggro 持续越线起点
    4. 被动刺激计算钩子 —— 收到他人发言事件后纯规则计算自身 Aggro 刺激
    5. 行为锚点 (强心剂) —— 子类必须提供 MBTI 核心人格指令字符串

设计原则:
    - 模板方法模式 (Template Method): 基类掌控生命周期,子类填充细节
    - 抽象属性 + 抽象方法双管齐下: behavior_anchor / personal_keywords / ...
    - 完全无副作用的"读路径": should_attempt_speak / compute_passive_stimuli
    - 写路径全部走基类: 杜绝子类绕过 FSM/Arbitrator 直调

⚠️ 工程铁律:
    - 子类严禁直接调 LLMRouter (走 attempt_speak 模板)
    - 子类严禁绕过 FSM (基类已强制 transition)
    - 刺激计算钩子严禁调 LLM (纯规则,LISTENING 态算力节约的核心保证)
    - 严禁子类持有 Turn 副本 (按需通过 ContextBus 拉取)

==============================================================================
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final, Optional

from loguru import logger

from backend.core.aggro_engine import (
    AggroEngine,
    StimulusEvent,
    StimulusKind,
    get_aggro_engine,
)
from backend.core.arbitrator import (
    Arbitrator,
    CooldownActiveError,
    DebounceNotMetError,
    MicrophoneBusyError,
    MicrophoneToken,
    RequestKind,
    get_arbitrator,
)
from backend.core.context_bus import (
    ContextBus,
    TopicChangedEvent,
    TurnSealedEvent,
    get_context_bus,
)
from backend.core.fsm import (
    AgentFSM,
    FSMRegistry,
    FSMState,
    IllegalTransitionError,
    LLMInvocationForbiddenError,
    get_fsm_registry,
)
from backend.core.llm_router import (
    InvocationResult,
    LLMRouter,
    LLMRouterError,
    MicrophoneNotHeldError,
    OllamaConnectError,
    OllamaUpstreamError,
    TerminationCause,
    get_llm_router,
)
from backend.core.semaphore import LLMSemaphore, get_llm_semaphore
from backend.core.throttle import PacketKind, ThrottleHub, get_throttle_hub
from backend.core.watchdog import GlobalWatchdog, get_watchdog
from backend.memory.sliding_window import format_for_llm_prompt_dict
from backend.schemas.agent_state import (
    AgentDescriptorModel,
    Faction,
    MBTIType,
    faction_of,
)


# ──────────────────────────────────────────────────────────────────────────────
# 默认采样参数 (子类可重写)
# ──────────────────────────────────────────────────────────────────────────────

#: 默认 temperature
DEFAULT_TEMPERATURE: Final[float] = 0.85

#: 默认 top_p
DEFAULT_TOP_P: Final[float] = 0.9

#: 默认 max_tokens (单次发言)
DEFAULT_MAX_TOKENS: Final[int] = 320


# ──────────────────────────────────────────────────────────────────────────────
# 抢话窗口跟踪
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _AggroBreachTracker:
    """
    跟踪 Aggro 持续 ≥ 阈值的起点 (供 Arbitrator 去抖动判定)。

    规则:
        - Aggro 跨越阈值上行 → 记录 monotonic 起点
        - Aggro 跌回阈值下方 → 清空起点
        - 起点持续 ≥ debounce_sec → 申请才会被仲裁器接受
    """

    threshold: float
    started_at_monotonic: Optional[float] = None
    last_aggro_value: float = 0.0

    def update(self, current_aggro: float) -> None:
        if current_aggro >= self.threshold:
            if self.started_at_monotonic is None:
                self.started_at_monotonic = time.monotonic()
        else:
            self.started_at_monotonic = None
        self.last_aggro_value = current_aggro

    def reset(self) -> None:
        self.started_at_monotonic = None


# ──────────────────────────────────────────────────────────────────────────────
# 静态人格描述 (供子类填充)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentPersona:
    """
    Agent 静态描述 · 子类 __init__ 中提供。

    包含:
        - agent_id: 系统级唯一 ID (例: "INTJ_logician")
        - mbti / faction (派生)
        - display_name: HUD 中文显示名
        - archetype: 人格原型 (例: "逻辑硬核型")
        - decay_lambda: 个性化 Aggro 衰减系数 (1/秒)
        - has_chroma_access: 是否拥有 ChromaDB 检索权 (仅 ISFJ=True)
        - is_summarizer: 是否承担 Summarizer 职责 (仅 ESTP=True)
    """

    agent_id: str
    mbti: MBTIType
    display_name: str
    archetype: str
    decay_lambda: float
    has_chroma_access: bool = False
    is_summarizer: bool = False

    @property
    def faction(self) -> Faction:
        return faction_of(self.mbti)

    def to_descriptor_model(self, behavior_anchor: str) -> AgentDescriptorModel:
        return AgentDescriptorModel(
            agent_id=self.agent_id,
            mbti=self.mbti,
            faction=self.faction,
            display_name=self.display_name,
            archetype=self.archetype,
            behavior_anchor=behavior_anchor,
            decay_lambda=self.decay_lambda,
            has_chroma_access=self.has_chroma_access,
            is_summarizer=self.is_summarizer,
        )


# ──────────────────────────────────────────────────────────────────────────────
# 基类: BaseAgent
# ──────────────────────────────────────────────────────────────────────────────


class BaseAgent(ABC):
    """
    8 个 MBTI Agent 的共同抽象基类。

    子类必须实现:
        - persona (类属性或 __init__ 中赋值)
        - behavior_anchor (强心剂注入文本)
        - personal_keywords (个人关键词列表)
        - compute_passive_stimuli_on_other_speak (被动刺激规则)

    子类可选重写:
        - should_attempt_speak (申请麦克风条件)
        - should_attempt_interrupt (抢话条件)
        - aggro_weight_overrides (个性化刺激权重)
        - prepare_extra_system_prefix (Prompt 前置 system 消息)
        - temperature / top_p / max_tokens (采样参数)
        - on_own_turn_sealed (自己 Turn 封存后的回调)
        - on_other_turn_sealed (他人 Turn 封存后的回调)
        - on_topic_changed (议题变化回调)
    """

    #: 子类必须设置的人格描述
    persona: AgentPersona

    # ════════════════════════════════════════════════════════════
    # 抽象接口
    # ════════════════════════════════════════════════════════════

    @property
    @abstractmethod
    def behavior_anchor(self) -> str:
        """
        MBTI 核心人格行为锚点 · 强心剂注入文本。

        例 (INTJ):
            "保持冰冷的逻辑演绎,使用三段论结构,严禁感性煽情,
             直接指出对方推理链中的逻辑漏洞。"
        """

    @property
    @abstractmethod
    def personal_keywords(self) -> tuple[str, ...]:
        """
        个人敏感关键词列表。

        他人发言中命中这些关键词时会触发 KEYWORD_TRIGGER 刺激。
        例如 INTJ 的关键词可能是: ("演绎", "因果", "前提", "逻辑漏洞")。
        """

    @abstractmethod
    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        他人 Turn 封存时,本 Agent 应该收到哪些刺激事件 (纯规则)。

        ⚠️ 严禁调用 LLM,严禁引发任何外部 IO。

        参数 event 含:
            - event.turn: 已封存的 Turn (含影子后缀)
            - event.keyword_hits: 命中的关键词字典
            - event.named_targets: 被点名的 Agent ID 列表

        返回的 StimulusEvent 列表会通过 AggroEngine.apply_stimuli 应用到自身 Aggro。
        """

    # ════════════════════════════════════════════════════════════
    # 默认实现 (可重写)
    # ════════════════════════════════════════════════════════════

    @property
    def aggro_weight_overrides(self) -> dict[StimulusKind, float]:
        """
        个性化刺激权重覆盖 · 默认空,使用 DEFAULT_STIMULUS_WEIGHT。

        例 (ENTP): 对 STANCE_CONFLICT 翻倍 → {STANCE_CONFLICT: 24.0}
        """
        return {}

    @property
    def initial_aggro(self) -> Optional[float]:
        """初始 Aggro 值 · None = 取 AGGRO_MIN。"""
        return None

    @property
    def aliases(self) -> tuple[str, ...]:
        """
        Agent 在前缀拦截 [TARGET: ...] 中可被识别的别名。

        默认空 = 仅 agent_id 自身可被点名。
        """
        return ()

    @property
    def temperature(self) -> float:
        return DEFAULT_TEMPERATURE

    @property
    def top_p(self) -> float:
        return DEFAULT_TOP_P

    @property
    def max_tokens(self) -> int:
        return DEFAULT_MAX_TOKENS

    @property
    def model_override(self) -> Optional[str]:
        """覆盖默认模型 · 默认 None = 使用主推理模型。"""
        return None

    def should_attempt_speak(self) -> bool:
        """
        本 Agent 当前 tick 是否申请麦克风?

        默认: Aggro ≥ 抢话阈值 时返回 True。
        子类可重写实现更细致的策略 (例: ENFP 看全局沸腾度)。
        """
        if self._fsm.state != FSMState.LISTENING:
            return False
        if self._aggro.value < self._aggro_engine.interrupt_threshold:
            return False
        # 必须已经持续越线一段时间 (debounce 由 Arbitrator 校验,这里粗筛)
        return self._breach_tracker.started_at_monotonic is not None

    def should_attempt_interrupt(self) -> bool:
        """
        是否抢当前持麦者?

        默认: Aggro ≥ 阈值 + 当前 FSM 处于 LISTENING + 当前不是自己持麦
        子类可重写 (例: ENTP 阈值更低,ISTJ 仅在偏题时抢)
        """
        if self._fsm.state != FSMState.LISTENING:
            return False
        holder = self._arbitrator.current_holder()
        if holder is None or holder.holder_agent_id == self.persona.agent_id:
            return False
        if self._aggro.value < self._aggro_engine.interrupt_threshold:
            return False
        return self._breach_tracker.started_at_monotonic is not None

    def prepare_extra_system_prefix(self) -> Optional[str]:
        """
        在 Prompt 最前面追加的 system 消息。

        默认: 拼接当前议题 + 自身人格简要。
        子类可重写以注入更精细的上下文 (例: ISFJ 注入检索结果摘要)。
        """
        topic = self._bus.current_topic()
        topic_part = (
            f"当前议题:{topic.title}。{topic.description}".strip()
            if topic
            else "当前无明确议题。"
        )
        return (
            f"你是辩论沙盒中的 {self.persona.display_name} "
            f"({self.persona.archetype})。{topic_part} "
            f"在每次发言的开头,请使用一行结构化前缀 [TARGET: <对手 ID>] 标明你针对的对象,"
            f"若面向全员请使用 [TARGET: ALL]。"
        )

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        自己的 Turn 封存后回调 · 默认 no-op。

        子类可重写做"发言后冷静下"等自我刺激调整。
        """
        return

    async def on_other_turn_sealed(self, event: TurnSealedEvent) -> None:
        """他人 Turn 封存后回调 · 默认 no-op。"""
        return

    async def on_topic_changed(self, event: TopicChangedEvent) -> None:
        """议题栈变化回调 · 默认 no-op。"""
        return

    # ════════════════════════════════════════════════════════════
    # 构造 / 引用注入
    # ════════════════════════════════════════════════════════════

    def __init__(
        self,
        *,
        fsm_registry: Optional[FSMRegistry] = None,
        aggro_engine: Optional[AggroEngine] = None,
        arbitrator: Optional[Arbitrator] = None,
        context_bus: Optional[ContextBus] = None,
        watchdog: Optional[GlobalWatchdog] = None,
        throttle_hub: Optional[ThrottleHub] = None,
    ) -> None:
        # 子类必须在 __init__ 中先设置 self.persona,基类才能继续
        if not hasattr(self, "persona") or not isinstance(self.persona, AgentPersona):
            raise TypeError(
                f"{type(self).__name__}.__init__ 必须先设置 self.persona "
                f"(AgentPersona 实例) 才能调用 super().__init__"
            )

        self._fsm_registry: Final[FSMRegistry] = fsm_registry or get_fsm_registry()
        self._aggro_engine: Final[AggroEngine] = (
            aggro_engine or get_aggro_engine()
        )
        self._arbitrator: Final[Arbitrator] = arbitrator or get_arbitrator()
        self._bus: Final[ContextBus] = context_bus or get_context_bus()
        self._watchdog: Final[GlobalWatchdog] = watchdog or get_watchdog()
        self._throttle_hub: Final[ThrottleHub] = throttle_hub or get_throttle_hub()

        # 这些在 register_with_runtime 后绑定 (不可在 __init__ 提前 register,
        # 因为 FSMRegistry 等单例可能尚未在 lifespan 准备阶段就绪)
        self._fsm: Optional[AgentFSM] = None  # type: ignore[assignment]

        # 抢话去抖动跟踪器 (register 时 threshold 才确定)
        self._breach_tracker: _AggroBreachTracker = _AggroBreachTracker(
            threshold=0.0
        )

        # 已注册标志
        self._registered: bool = False

    # ════════════════════════════════════════════════════════════
    # 一站式注册
    # ════════════════════════════════════════════════════════════

    def register_with_runtime(self) -> None:
        """
        将本 Agent 注册到运行时各子系统:
            - FSMRegistry (创建 AgentFSM 实例)
            - AggroEngine (注册 Aggro 桶)
            - ContextBus (注册别名 + 关键词,订阅事件)

        ⚠️ 必须在所有 core 单例 ready 之后调用 (lifespan startup 阶段)。
        重复调用会抛 RuntimeError。
        """
        if self._registered:
            raise RuntimeError(
                f"Agent {self.persona.agent_id!r} 已注册,严禁重复"
            )

        # FSM 注册
        self._fsm = self._fsm_registry.register(
            agent_id=self.persona.agent_id,
            initial_state=FSMState.IDLE,
        )

        # Aggro 注册
        self._aggro_engine.register_agent(
            agent_id=self.persona.agent_id,
            decay_lambda=self.persona.decay_lambda,
            weight_overrides=dict(self.aggro_weight_overrides) or None,
            initial_value=self.initial_aggro,
        )

        # ContextBus 注册 (别名 + 关键词)
        self._bus.register_agent(
            agent_id=self.persona.agent_id,
            aliases=self.aliases,
            keywords=self.personal_keywords,
        )

        # 订阅事件
        self._bus.subscribe_sealed(self._on_turn_sealed_dispatcher)
        self._bus.subscribe_topic(self._on_topic_changed_dispatcher)

        # 设置抢话去抖动跟踪器阈值
        self._breach_tracker = _AggroBreachTracker(
            threshold=self._aggro_engine.interrupt_threshold,
        )

        self._registered = True
        logger.info(
            f"[Agent] 注册完成 · id={self.persona.agent_id!r} · "
            f"mbti={self.persona.mbti.value} · "
            f"faction={self.persona.faction.value} · "
            f"λ={self.persona.decay_lambda}"
        )

    # ════════════════════════════════════════════════════════════
    # 公开属性 / 视图
    # ════════════════════════════════════════════════════════════

    @property
    def agent_id(self) -> str:
        return self.persona.agent_id

    @property
    def descriptor(self) -> AgentDescriptorModel:
        return self.persona.to_descriptor_model(self.behavior_anchor)

    @property
    def fsm(self) -> AgentFSM:
        if self._fsm is None:
            raise RuntimeError(
                f"Agent {self.persona.agent_id!r} 未注册 · "
                f"请先调用 register_with_runtime"
            )
        return self._fsm

    @property
    def _aggro(self):  # 类型为 _AgentAggroState 但其内部受保护,这里用快照
        # 注: AggroEngine.snapshot 返回的是 AggroSnapshot;
        # 为避免暴露内部 state,这里返回 snapshot 的轻量代理
        return self._aggro_engine.snapshot(self.persona.agent_id)

    @property
    def is_registered(self) -> bool:
        return self._registered

    # ════════════════════════════════════════════════════════════
    # 模板方法: 单次发言尝试
    # ════════════════════════════════════════════════════════════

    async def attempt_speak(
        self,
        *,
        request_kind: RequestKind = RequestKind.AGGRO_THRESHOLD,
        force_token: Optional[MicrophoneToken] = None,
    ) -> Optional[InvocationResult]:
        """
        申请麦克风 → FSM 转 COMPUTING → invoke_chat_stream → 释放令牌。

        Args:
            request_kind: 申请类型 (AGGRO_THRESHOLD / INTERRUPT / VACUUM_FILL / SCHEDULED)
            force_token: 已经在外部拿到的 token (例如 ESTP 真空期分配后传入)
                         若提供,跳过 request_microphone 直接进入推理

        Returns:
            InvocationResult · None 表示申请未通过/被拒绝,无任何副作用
        """
        if not self._registered or self._fsm is None:
            raise RuntimeError(
                f"Agent {self.persona.agent_id!r} 未注册"
            )

        # ── 1. 拿令牌 (除非外部已拿) ──
        token: Optional[MicrophoneToken] = force_token
        if token is None:
            token = await self._acquire_token(request_kind)
            if token is None:
                return None

        # ── 2. FSM: LISTENING → COMPUTING ──
        try:
            await self._fsm.transition(
                FSMState.COMPUTING,
                reason=f"got_microphone:{request_kind.value}",
            )
        except IllegalTransitionError as exc:
            logger.warning(
                f"[Agent:{self.persona.agent_id}] 拿到令牌但 FSM 拒绝 "
                f"COMPUTING (state={self._fsm.state.value}) · 释放令牌: {exc}"
            )
            await self._arbitrator.release_token(token)
            return None

        # ── 3. 准备 Prompt messages ──
        try:
            messages = self._build_messages()
        except Exception as exc:
            logger.error(
                f"[Agent:{self.persona.agent_id}] _build_messages 异常 · "
                f"释放令牌: {exc!r}"
            )
            await self._safe_release(token, fsm_target=FSMState.LISTENING)
            return None

        # ── 4. 调推理 (含 watchdog) ──
        result: Optional[InvocationResult] = None
        try:
            router = await get_llm_router()
            current_task = asyncio.current_task()
            async with self._watchdog.watch(
                agent_id=self.persona.agent_id,
                task=current_task,
                arbitrator_token_holder_id=self.persona.agent_id,
                note=f"speak_kind={request_kind.value}",
            ) as wh:
                result = await router.invoke_chat_stream(
                    agent_id=self.persona.agent_id,
                    fsm=self._fsm,
                    token=token,
                    arbitrator=self._arbitrator,
                    context_bus=self._bus,
                    messages=messages,
                    behavior_anchor=self.behavior_anchor,
                    on_text_chunk=self._on_text_chunk,
                    model_override=self.model_override,
                    temperature=self.temperature,
                    top_p=self.top_p,
                    max_tokens=self.max_tokens,
                    watchdog_event=wh.watchdog_event,
                )
        except (
            MicrophoneNotHeldError,
            LLMInvocationForbiddenError,
            OllamaConnectError,
            OllamaUpstreamError,
            LLMRouterError,
        ) as exc:
            logger.warning(
                f"[Agent:{self.persona.agent_id}] invoke_chat_stream 拒绝 · "
                f"{exc!r} (令牌已由 router 内部释放)"
            )
            return None
        except asyncio.CancelledError:
            logger.info(
                f"[Agent:{self.persona.agent_id}] 推理被取消 (上层 cancel)"
            )
            raise
        except Exception as exc:
            logger.error(
                f"[Agent:{self.persona.agent_id}] invoke_chat_stream 未知异常 · "
                f"{exc!r}"
            )
            return None

        # ── 5. 广播 INTERRUPT 信号给前端 (仅打断路径) ──
        if result is not None and result.cause == TerminationCause.INTERRUPTED:
            try:
                await self._throttle_hub.broadcast_interrupt(
                    interrupter_agent_id=(
                        result.interrupter_agent_id or "UNKNOWN"
                    ),
                    interrupted_agent_id=self.persona.agent_id,
                )
            except Exception as exc:
                logger.debug(
                    f"[Agent:{self.persona.agent_id}] broadcast_interrupt "
                    f"失败 (忽略): {exc!r}"
                )

        # 注: FSM 已由 router 在 finally 路径转回 LISTENING,无需重复
        return result

    # ────────────────────────────────────────────────────────────
    # 内部: 拿令牌
    # ────────────────────────────────────────────────────────────

    async def _acquire_token(
        self,
        request_kind: RequestKind,
    ) -> Optional[MicrophoneToken]:
        """
        申请令牌 · 根据 request_kind 走不同路径。

        失败时返回 None (含义被仲裁器拒绝、抢话竞态等)。
        """
        agent_id = self.persona.agent_id
        breach_started = self._breach_tracker.started_at_monotonic
        current_aggro = self._aggro.current

        try:
            if request_kind == RequestKind.INTERRUPT:
                token = await self._arbitrator.request_interrupt(
                    agent_id=agent_id,
                    aggro=current_aggro,
                    aggro_above_threshold_since_monotonic=breach_started,
                )
            else:
                token = await self._arbitrator.request_microphone(
                    agent_id=agent_id,
                    kind=request_kind,
                    aggro=current_aggro,
                    aggro_above_threshold_since_monotonic=breach_started,
                )
        except (
            MicrophoneBusyError,
            CooldownActiveError,
            DebounceNotMetError,
        ) as exc:
            logger.debug(
                f"[Agent:{agent_id}] 申请令牌被拒 ({type(exc).__name__}): {exc}"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[Agent:{agent_id}] 申请令牌异常: {exc!r}"
            )
            return None

        return token

    async def _safe_release(
        self,
        token: MicrophoneToken,
        fsm_target: FSMState,
    ) -> None:
        """容错路径: 释放令牌 + 转 FSM,失败仅记录。"""
        try:
            await self._arbitrator.release_token(token)
        except Exception as exc:
            logger.debug(
                f"[Agent:{self.persona.agent_id}] release_token 异常 (忽略): {exc!r}"
            )
        try:
            if self._fsm is not None and self._fsm.state != fsm_target:
                await self._fsm.transition(
                    fsm_target,
                    reason="safe_release",
                )
        except Exception as exc:
            logger.debug(
                f"[Agent:{self.persona.agent_id}] safe_release transition "
                f"异常 (忽略): {exc!r}"
            )

    # ────────────────────────────────────────────────────────────
    # 内部: Prompt 构造
    # ────────────────────────────────────────────────────────────

    def _build_messages(self) -> list[dict[str, str]]:
        """
        基于短记忆窗口 + extra_system_prefix 构造 Ollama messages。

        子类可通过重写 prepare_extra_system_prefix 改变前置内容,
        无需重写整个 _build_messages。
        """
        prefix = self.prepare_extra_system_prefix()
        return format_for_llm_prompt_dict(
            extra_system_prefix=prefix,
        )

    # ────────────────────────────────────────────────────────────
    # 内部: 流式 text chunk 回调 (推送到 Throttle)
    # ────────────────────────────────────────────────────────────

    async def _on_text_chunk(self, text: str) -> None:
        """
        LLMRouter 每解析出一段纯文本就调用此回调。
        本基类实现: 通过 ThrottleHub 广播给所有订阅者。
        """
        if not text:
            return
        try:
            await self._throttle_hub.broadcast_text(
                text=text,
                agent_id=self.persona.agent_id,
            )
        except Exception as exc:
            logger.debug(
                f"[Agent:{self.persona.agent_id}] broadcast_text "
                f"失败 (忽略,不影响推理): {exc!r}"
            )

    # ════════════════════════════════════════════════════════════
    # 事件分发器 (订阅 ContextBus 后触发)
    # ════════════════════════════════════════════════════════════

    async def _on_turn_sealed_dispatcher(self, event: TurnSealedEvent) -> None:
        """
        ContextBus 广播 TurnSealedEvent 时触发。

        路径:
            - 若 turn.agent_id == self.agent_id → on_own_turn_sealed
            - 否则 → 计算被动刺激 (纯规则) → 应用到 AggroEngine →
              调 on_other_turn_sealed 钩子
        """
        if not self._registered:
            return

        try:
            if event.turn.agent_id == self.persona.agent_id:
                # 自己的 Turn
                await self.on_own_turn_sealed(event)
                # 自己发完话后,Aggro 自然衰减,这里不主动施加负刺激
                # (霸麦惩罚由 Arbitrator 的 cooldown + consecutive 处理)
                return

            # 他人的 Turn → 计算被动刺激
            stimuli = self.compute_passive_stimuli_on_other_speak(event)
            if stimuli:
                # 过滤非法 delta
                clean_stimuli = [
                    s for s in stimuli if s and isinstance(s, StimulusEvent)
                ]
                if clean_stimuli:
                    try:
                        await self._aggro_engine.apply_stimuli(
                            agent_id=self.persona.agent_id,
                            events=clean_stimuli,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[Agent:{self.persona.agent_id}] "
                            f"apply_stimuli 异常: {exc!r}"
                        )

            # 更新去抖动跟踪器 (Aggro 可能已变化)
            new_aggro = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
            self._breach_tracker.update(new_aggro)

            # 钩子
            await self.on_other_turn_sealed(event)

        except Exception as exc:
            # 订阅器异常隔离 (ContextBus 已 gather + return_exceptions,
            # 但本层兜底再保一道)
            logger.error(
                f"[Agent:{self.persona.agent_id}] "
                f"_on_turn_sealed_dispatcher 异常: {exc!r}"
            )

    async def _on_topic_changed_dispatcher(
        self,
        event: TopicChangedEvent,
    ) -> None:
        if not self._registered:
            return
        try:
            # 议题切换 → 重置去抖动跟踪器 (避免旧议题的 Aggro 越线状态遗留)
            self._breach_tracker.reset()
            await self.on_topic_changed(event)
        except Exception as exc:
            logger.error(
                f"[Agent:{self.persona.agent_id}] "
                f"_on_topic_changed_dispatcher 异常: {exc!r}"
            )

    # ════════════════════════════════════════════════════════════
    # 调度器协助接口 (供 registry / 调度循环调用)
    # ════════════════════════════════════════════════════════════

    def refresh_breach_tracker(self) -> None:
        """
        刷新去抖动跟踪器 · 调度循环周期调用。

        即使没有 TurnSealedEvent,Aggro 也可能因衰减跌回阈值下方,
        因此调度循环每 tick 应主动刷新。
        """
        if not self._registered:
            return
        try:
            current = self._aggro_engine.snapshot(
                self.persona.agent_id
            ).current
            self._breach_tracker.update(current)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "BaseAgent",
    "AgentPersona",
    "DEFAULT_TEMPERATURE",
    "DEFAULT_TOP_P",
    "DEFAULT_MAX_TOKENS",
]