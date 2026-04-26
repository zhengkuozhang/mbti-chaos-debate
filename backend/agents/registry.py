"""
==============================================================================
backend/agents/registry.py
------------------------------------------------------------------------------
Agent 注册表 + 辩论调度循环驱动器

定位:
    1. AgentRegistry      - 8 个 Agent 实例的统一容器与查询入口
    2. DebateScheduler    - 后台调度循环,周期性驱动 8 个 Agent 的发言申请

调度循环核心职责 (默认 4Hz · 250ms tick):
    1. 刷新所有 Agent 的去抖动跟踪器 (refresh_breach_tracker)
    2. 收集"想说话"的 Agent · 按优先级排序
    3. 优先级:
         (a) Aggro 越线且当前有持麦者 != 自己 → INTERRUPT 抢话
         (b) Aggro 越线且当前无持麦者 → AGGRO_THRESHOLD 正常申请
         (c) 静默 ≥ 真空期阈值 → ESTP 真空期补位 (调用其 nominate hook)
    4. 一次 tick 只派发一个发言任务 (asyncio.create_task)
    5. 任务派发后立即返回,继续下一 tick (不阻塞)

设计原则:
    1. 调度器永远不持有令牌 - 让 Agent.attempt_speak 走 Arbitrator 全套规则
    2. 失败容错 - 单 Agent 异常不影响整体调度
    3. 会话状态守门 - PAUSED / SHUTTING_DOWN 时空跑
    4. Supervisor 自动重启 - 严禁静默死亡

⚠️ 工程铁律:
    - 调度层只做 *排序与派发*,具体的令牌争夺交给 Arbitrator
    - 调度循环严禁 await 推理任务 (用 create_task)
    - 同一 Agent 不允许并发 attempt_speak (基类已守门,但调度层也要避免)
    - 异常隔离: gather + return_exceptions 兜底所有钩子调用

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import Final, Iterable, Optional

from loguru import logger

from backend.agents.base_agent import BaseAgent
from backend.core.aggro_engine import AggroEngine, get_aggro_engine
from backend.core.arbitrator import (
    Arbitrator,
    MicrophoneToken,
    RequestKind,
    get_arbitrator,
)
from backend.core.fsm import FSMRegistry, FSMState, get_fsm_registry
from backend.schemas.agent_state import (
    AgentDescriptorModel,
    Faction,
    MBTIType,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 调度循环 tick 频率 (Hz) · 默认 4Hz = 每 250ms 一次
_DEFAULT_TICK_HZ: Final[float] = 4.0

#: 真空期最小静默秒数 (秒) · 超过则尝试 ESTP 控场补位
_VACUUM_MIN_SILENCE_SEC: Final[float] = 6.0

#: ESTP 真空期补位时使用的人格选择策略 · 这里采用"最近未发言且 Aggro 中等者"
_VACUUM_PICK_AGGRO_LOWER_BOUND: Final[float] = 30.0
_VACUUM_PICK_AGGRO_UPPER_BOUND: Final[float] = 70.0

#: Worker supervisor 异常重启 backoff
_WORKER_RESTART_BACKOFF_SEC: Final[float] = 1.0


# ──────────────────────────────────────────────────────────────────────────────
# 会话状态
# ──────────────────────────────────────────────────────────────────────────────


class SchedulerSessionStatus(str, enum.Enum):
    """
    调度器视角的会话状态 (与 schemas/debate_context.SessionStatus 对齐)。
    """

    INITIALIZING = "INITIALIZING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    SHUTTING_DOWN = "SHUTTING_DOWN"


# ──────────────────────────────────────────────────────────────────────────────
# 调度候选项 (内部数据结构)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Candidate:
    """单 tick 中一个想发言的候选 Agent。"""

    agent: BaseAgent
    aggro_value: float
    breach_started_at: Optional[float]
    is_interrupt: bool
    """True = 走 INTERRUPT 路径,False = 走 AGGRO_THRESHOLD"""


# ──────────────────────────────────────────────────────────────────────────────
# 观测指标
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SchedulerMetrics:
    is_running: bool
    status: SchedulerSessionStatus
    tick_hz: float
    ticks_total: int
    dispatches_total: int
    interrupt_dispatches_total: int
    vacuum_dispatches_total: int
    paused_skips_total: int
    candidates_seen_total: int
    last_dispatch_agent_id: Optional[str]
    last_dispatch_kind: Optional[str]
    last_dispatch_at_wall_unix: float
    active_speak_tasks: int

    def to_dict(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "status": self.status.value,
            "tick_hz": self.tick_hz,
            "ticks_total": self.ticks_total,
            "dispatches_total": self.dispatches_total,
            "interrupt_dispatches_total": self.interrupt_dispatches_total,
            "vacuum_dispatches_total": self.vacuum_dispatches_total,
            "paused_skips_total": self.paused_skips_total,
            "candidates_seen_total": self.candidates_seen_total,
            "last_dispatch_agent_id": self.last_dispatch_agent_id,
            "last_dispatch_kind": self.last_dispatch_kind,
            "last_dispatch_at_wall_unix": round(
                self.last_dispatch_at_wall_unix, 6
            ),
            "active_speak_tasks": self.active_speak_tasks,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class AgentRegistryError(Exception):
    pass


class DuplicateAgentError(AgentRegistryError):
    pass


class UnknownAgentError(AgentRegistryError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# 主体 1: AgentRegistry
# ──────────────────────────────────────────────────────────────────────────────


class AgentRegistry:
    """
    8 个 Agent 实例的集中容器 (进程级单例)。

    职责:
        - 实例注册 / 查询 (按 ID / MBTI / 阵营)
        - 一站式 register_all_with_runtime · 把所有 Agent 注册到 FSM/Aggro/Bus
        - descriptors_dict / agents_iter · 供 HUD 与调度器消费
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    # ────────────────────────────────────────────────────────────
    # 注册 / 查询
    # ────────────────────────────────────────────────────────────

    def register(self, agent: BaseAgent) -> None:
        """注册 Agent 实例 (尚未注册到 runtime,需后续 register_all_with_runtime)。"""
        if not isinstance(agent, BaseAgent):
            raise TypeError(
                f"agent 必须为 BaseAgent 子类,当前类型: {type(agent).__name__}"
            )

        aid = agent.agent_id
        if aid in self._agents:
            raise DuplicateAgentError(
                f"Agent {aid!r} 已注册到 AgentRegistry"
            )
        self._agents[aid] = agent
        logger.info(
            f"[AgentRegistry] register · id={aid!r} · "
            f"mbti={agent.persona.mbti.value} · total={len(self._agents)}"
        )

    def get(self, agent_id: str) -> BaseAgent:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise UnknownAgentError(f"Agent {agent_id!r} 未注册") from exc

    def __contains__(self, agent_id: object) -> bool:
        return isinstance(agent_id, str) and agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def all_ids(self) -> tuple[str, ...]:
        return tuple(self._agents.keys())

    def agents_iter(self) -> Iterable[BaseAgent]:
        """按注册顺序迭代所有 Agent (Python 3.7+ dict 保序)。"""
        return self._agents.values()

    def by_mbti(self, mbti: MBTIType) -> list[BaseAgent]:
        return [a for a in self._agents.values() if a.persona.mbti == mbti]

    def by_faction(self, faction: Faction) -> list[BaseAgent]:
        return [
            a
            for a in self._agents.values()
            if a.persona.faction == faction
        ]

    def find_summarizer(self) -> Optional[BaseAgent]:
        """返回承担 Summarizer 职责的 Agent (通常是 ESTP)。"""
        for a in self._agents.values():
            if a.persona.is_summarizer:
                return a
        return None

    def find_archivist(self) -> Optional[BaseAgent]:
        """返回拥有 ChromaDB 检索权的 Agent (通常是 ISFJ)。"""
        for a in self._agents.values():
            if a.persona.has_chroma_access:
                return a
        return None

    # ────────────────────────────────────────────────────────────
    # runtime 注册
    # ────────────────────────────────────────────────────────────

    def register_all_with_runtime(self) -> int:
        """
        把所有已注册 Agent 串行 register_with_runtime。

        单 Agent 失败仅记录,继续注册其他,杜绝"一个崩 = 全崩"。

        Returns:
            成功注册的 Agent 数
        """
        success = 0
        for agent in self._agents.values():
            if agent.is_registered:
                continue
            try:
                agent.register_with_runtime()
                success += 1
            except Exception as exc:
                logger.error(
                    f"[AgentRegistry] register_with_runtime 失败 · "
                    f"id={agent.agent_id!r} · err={exc!r}"
                )
        logger.info(
            f"[AgentRegistry] 一站式注册完成 · "
            f"success={success}/{len(self._agents)}"
        )
        return success

    # ────────────────────────────────────────────────────────────
    # 描述视图 (供 HUD)
    # ────────────────────────────────────────────────────────────

    def descriptors_dict(self) -> dict[str, AgentDescriptorModel]:
        """全量 descriptor 视图 (供 /api/sandbox/snapshot)。"""
        return {a.agent_id: a.descriptor for a in self._agents.values()}


# ──────────────────────────────────────────────────────────────────────────────
# 主体 2: DebateScheduler
# ──────────────────────────────────────────────────────────────────────────────


class DebateScheduler:
    """
    辩论调度循环驱动器 (进程级单例)。

    生命周期:
        sched = get_scheduler()
        await sched.start()                # lifespan startup
        sched.set_status(SchedulerSessionStatus.RUNNING)
        ...
        await sched.stop()                 # lifespan shutdown
    """

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        fsm_registry: FSMRegistry,
        aggro_engine: AggroEngine,
        arbitrator: Arbitrator,
        tick_hz: float = _DEFAULT_TICK_HZ,
        vacuum_silence_sec: float = _VACUUM_MIN_SILENCE_SEC,
    ) -> None:
        if tick_hz <= 0:
            raise ValueError(f"tick_hz 必须 > 0,当前: {tick_hz}")
        if vacuum_silence_sec <= 0:
            raise ValueError(
                f"vacuum_silence_sec 必须 > 0,当前: {vacuum_silence_sec}"
            )

        self._registry: Final[AgentRegistry] = registry
        self._fsm_registry: Final[FSMRegistry] = fsm_registry
        self._aggro: Final[AggroEngine] = aggro_engine
        self._arbitrator: Final[Arbitrator] = arbitrator
        self._tick_hz: Final[float] = tick_hz
        self._tick_interval_sec: Final[float] = 1.0 / tick_hz
        self._vacuum_silence_sec: Final[float] = vacuum_silence_sec

        self._status: SchedulerSessionStatus = (
            SchedulerSessionStatus.INITIALIZING
        )

        # 后台 Worker
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._stopped: bool = False

        # 当前正在派发的发言任务集合
        # (允许并发,Arbitrator 自己会拦下抢话冲突)
        self._active_speak_tasks: set[asyncio.Task[None]] = set()

        # 累计指标
        self._ticks_total: int = 0
        self._dispatches_total: int = 0
        self._interrupt_dispatches_total: int = 0
        self._vacuum_dispatches_total: int = 0
        self._paused_skips_total: int = 0
        self._candidates_seen_total: int = 0
        self._last_dispatch_agent_id: Optional[str] = None
        self._last_dispatch_kind: Optional[str] = None
        self._last_dispatch_at_wall_unix: float = 0.0

        logger.info(
            f"[DebateScheduler] 初始化 · tick={tick_hz}Hz · "
            f"vacuum_silence={vacuum_silence_sec}s"
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        if self._stopped:
            raise RuntimeError(
                "DebateScheduler 已 stop · 不可重启 (创建新实例)"
            )
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_signal.clear()
        self._worker_task = asyncio.create_task(
            self._supervisor(),
            name="debate_scheduler_loop",
        )
        # 状态从 INITIALIZING → READY (由调用方决定何时 → RUNNING)
        if self._status == SchedulerSessionStatus.INITIALIZING:
            self._status = SchedulerSessionStatus.READY
        logger.info("[DebateScheduler] 已启动")

    async def stop(self, drain_timeout_sec: float = 5.0) -> None:
        """
        停止调度 · 主动等待 in-flight 发言任务完成 (带超时兜底)。
        """
        if self._stopped:
            return
        self._stopped = True
        self._status = SchedulerSessionStatus.SHUTTING_DOWN
        self._stop_signal.set()

        # 1) 停 Worker
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        # 2) 等待 in-flight 发言任务自然完成 (有看门狗兜底,不会无限等)
        deadline = time.monotonic() + drain_timeout_sec
        while self._active_speak_tasks and time.monotonic() < deadline:
            # 清理已结束的任务
            done = {t for t in self._active_speak_tasks if t.done()}
            self._active_speak_tasks -= done
            if not self._active_speak_tasks:
                break
            await asyncio.sleep(0.1)

        # 3) 强行 cancel 残留
        if self._active_speak_tasks:
            logger.warning(
                f"[DebateScheduler] stop 时仍有 "
                f"{len(self._active_speak_tasks)} 个发言任务 · 强行 cancel"
            )
            for t in self._active_speak_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(
                *self._active_speak_tasks, return_exceptions=True
            )
            self._active_speak_tasks.clear()

        logger.info(
            f"[DebateScheduler] 已停止 · ticks={self._ticks_total} · "
            f"dispatches={self._dispatches_total}"
        )

    @property
    def is_running(self) -> bool:
        return (
            not self._stopped
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    # ════════════════════════════════════════════════════════════
    # 状态管理
    # ════════════════════════════════════════════════════════════

    @property
    def status(self) -> SchedulerSessionStatus:
        return self._status

    def set_status(self, new_status: SchedulerSessionStatus) -> None:
        """切换会话状态。SHUTTING_DOWN 与 stop() 内部联动。"""
        if not isinstance(new_status, SchedulerSessionStatus):
            raise TypeError(
                f"new_status 必须为 SchedulerSessionStatus,当前: "
                f"{type(new_status).__name__}"
            )
        old = self._status
        self._status = new_status
        logger.info(
            f"[DebateScheduler] 状态切换 · {old.value} → {new_status.value}"
        )

    # ════════════════════════════════════════════════════════════
    # Supervisor + 主循环
    # ════════════════════════════════════════════════════════════

    async def _supervisor(self) -> None:
        while not self._stop_signal.is_set():
            try:
                await self._tick_loop()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    f"[DebateScheduler] tick_loop 异常 · {exc!r} · "
                    f"{_WORKER_RESTART_BACKOFF_SEC}s 后重启"
                )
                try:
                    await asyncio.sleep(_WORKER_RESTART_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return

    async def _tick_loop(self) -> None:
        logger.info("[DebateScheduler] tick_loop 进入主循环")
        while not self._stop_signal.is_set():
            tick_start = time.monotonic()
            self._ticks_total += 1

            try:
                await self._tick_once()
            except Exception as exc:
                logger.error(
                    f"[DebateScheduler] tick_once 异常: {exc!r}"
                )

            # 等待下一 tick (允许提前停止)
            elapsed = time.monotonic() - tick_start
            wait_sec = max(0.0, self._tick_interval_sec - elapsed)
            try:
                await asyncio.wait_for(
                    self._stop_signal.wait(),
                    timeout=wait_sec if wait_sec > 0 else 0.001,
                )
                return  # stop_signal set,退出主循环
            except asyncio.TimeoutError:
                continue

    # ════════════════════════════════════════════════════════════
    # 单次 tick: 候选收集 + 派发
    # ════════════════════════════════════════════════════════════

    async def _tick_once(self) -> None:
        # 0) 清理已完成的发言任务
        self._reap_finished_speak_tasks()

        # 1) 状态守门: 仅 RUNNING 调度
        if self._status != SchedulerSessionStatus.RUNNING:
            self._paused_skips_total += 1
            return

        # 2) 刷新所有 Agent 的去抖动跟踪
        for agent in self._registry.agents_iter():
            try:
                agent.refresh_breach_tracker()
            except Exception as exc:
                logger.debug(
                    f"[DebateScheduler] refresh_breach 异常 · "
                    f"id={agent.agent_id!r} · {exc!r}"
                )

        # 3) 收集候选 (想发言/想抢话的 Agent)
        candidates = self._collect_candidates()
        self._candidates_seen_total += len(candidates)

        # 4) 派发 (优先抢话 → 正常申请;同优先级按 Aggro 倒序)
        if candidates:
            await self._dispatch_candidates(candidates)
            return  # 一旦有候选派发,本 tick 不再尝试真空期

        # 5) 真空期补位
        await self._maybe_dispatch_vacuum_fill()

    def _collect_candidates(self) -> list[_Candidate]:
        """
        遍历所有 Agent,收集"想说话"的候选。

        ⚠️ 此方法是同步纯计算,不应触发任何 IO / 协程切换。
        """
        candidates: list[_Candidate] = []
        holder_token = self._arbitrator.current_holder()
        holder_id = (
            holder_token.holder_agent_id if holder_token is not None else None
        )

        for agent in self._registry.agents_iter():
            if not agent.is_registered:
                continue
            # FSM 必须处于 LISTENING 才允许申请
            try:
                fsm_state = agent.fsm.state
            except Exception:
                continue
            if fsm_state != FSMState.LISTENING:
                continue

            # 持麦者本人不参与本轮 (他在说话)
            if holder_id is not None and holder_id == agent.agent_id:
                continue

            # Aggro 越线判定
            try:
                aggro_snap = self._aggro.snapshot(agent.agent_id)
            except Exception:
                continue

            if aggro_snap.current < self._arbitrator.interrupt_threshold:
                continue

            # 调用子类决策钩子
            is_interrupt = (
                holder_id is not None
                and holder_id != agent.agent_id
                and self._safe_call_decision(agent, "should_attempt_interrupt")
            )
            wants_speak = (
                holder_id is None
                and self._safe_call_decision(agent, "should_attempt_speak")
            )

            if not (is_interrupt or wants_speak):
                continue

            # 取出 breach tracker 起点 (供仲裁器去抖动判定)
            breach_started = agent._breach_tracker.started_at_monotonic
            candidates.append(
                _Candidate(
                    agent=agent,
                    aggro_value=aggro_snap.current,
                    breach_started_at=breach_started,
                    is_interrupt=is_interrupt,
                )
            )

        return candidates

    @staticmethod
    def _safe_call_decision(agent: BaseAgent, method_name: str) -> bool:
        """容错地调用 Agent 决策钩子 (should_attempt_speak/interrupt)。"""
        try:
            method = getattr(agent, method_name)
            return bool(method())
        except Exception as exc:
            logger.debug(
                f"[DebateScheduler] {agent.agent_id} {method_name} 异常: {exc!r}"
            )
            return False

    async def _dispatch_candidates(
        self,
        candidates: list[_Candidate],
    ) -> None:
        """
        从候选中选出最高优先级的一个派发。

        优先级: INTERRUPT > AGGRO_THRESHOLD,同类按 Aggro 倒序 (≈ priority 倒序)
        """
        # INTERRUPT 优先
        interrupt_candidates = [c for c in candidates if c.is_interrupt]
        normal_candidates = [c for c in candidates if not c.is_interrupt]

        # Aggro 倒序
        interrupt_candidates.sort(key=lambda c: c.aggro_value, reverse=True)
        normal_candidates.sort(key=lambda c: c.aggro_value, reverse=True)

        ordered = interrupt_candidates + normal_candidates

        # 一次只派一个,失败的 Agent 留待下一 tick
        for cand in ordered:
            request_kind = (
                RequestKind.INTERRUPT
                if cand.is_interrupt
                else RequestKind.AGGRO_THRESHOLD
            )
            if self._spawn_speak_task(cand.agent, request_kind):
                if cand.is_interrupt:
                    self._interrupt_dispatches_total += 1
                self._dispatches_total += 1
                self._last_dispatch_agent_id = cand.agent.agent_id
                self._last_dispatch_kind = request_kind.value
                self._last_dispatch_at_wall_unix = time.time()
                logger.info(
                    f"[DebateScheduler] DISPATCH · "
                    f"agent={cand.agent.agent_id!r} · "
                    f"kind={request_kind.value} · "
                    f"aggro={cand.aggro_value:.2f}"
                )
                return

    def _spawn_speak_task(
        self,
        agent: BaseAgent,
        request_kind: RequestKind,
    ) -> bool:
        """
        派发一个 attempt_speak 任务到事件循环。

        Returns:
            True - 成功派发 (任务已 create)
            False - 该 Agent 已有 in-flight 任务,跳过本次
        """
        # 防止同 Agent 并发派发
        for t in self._active_speak_tasks:
            if t.done():
                continue
            t_name = t.get_name()
            if t_name.startswith(f"speak_{agent.agent_id}__"):
                logger.debug(
                    f"[DebateScheduler] {agent.agent_id} 已有 in-flight "
                    f"任务,跳过本次派发"
                )
                return False

        task_name = f"speak_{agent.agent_id}__{int(time.time() * 1000)}"
        task = asyncio.create_task(
            self._safe_attempt_speak(agent, request_kind),
            name=task_name,
        )
        self._active_speak_tasks.add(task)
        # done callback: 自动从集合中移除
        task.add_done_callback(self._on_speak_task_done)
        return True

    @staticmethod
    async def _safe_attempt_speak(
        agent: BaseAgent,
        request_kind: RequestKind,
    ) -> None:
        """对 attempt_speak 的异常隔离封装。"""
        try:
            await agent.attempt_speak(request_kind=request_kind)
        except asyncio.CancelledError:
            logger.info(
                f"[DebateScheduler] speak 任务被取消 · "
                f"agent={agent.agent_id!r}"
            )
            raise
        except Exception as exc:
            logger.error(
                f"[DebateScheduler] attempt_speak 异常 · "
                f"agent={agent.agent_id!r} · {exc!r}"
            )

    def _on_speak_task_done(self, task: asyncio.Task[None]) -> None:
        """发言任务结束的回调 · 从 active 集合移除。"""
        self._active_speak_tasks.discard(task)
        # 若任务异常未被消费 (例如 CancelledError 被 raise),记录但不再处理
        try:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                logger.debug(
                    f"[DebateScheduler] speak 任务结束有异常 · "
                    f"name={task.get_name()} · {exc!r}"
                )
        except (asyncio.CancelledError, asyncio.InvalidStateError):
            pass

    def _reap_finished_speak_tasks(self) -> None:
        """每 tick 头部清理已完成任务 (兜底,正常情况下回调已清)。"""
        done = {t for t in self._active_speak_tasks if t.done()}
        self._active_speak_tasks -= done

    # ════════════════════════════════════════════════════════════
    # 真空期补位
    # ════════════════════════════════════════════════════════════

    async def _maybe_dispatch_vacuum_fill(self) -> None:
        """
        检测真空期 · 让 ESTP 控场调用 nominate_vacuum_speaker。

        若没有 ESTP 控场,或没有合适候选,本方法静默返回。
        """
        # 仍有持麦者 → 不算真空期
        if self._arbitrator.current_holder() is not None:
            return

        silence = self._arbitrator.seconds_since_last_speech()
        if silence < self._vacuum_silence_sec:
            return

        # 找控场 Agent
        nominator = self._registry.find_summarizer()
        if nominator is None or not nominator.is_registered:
            logger.debug(
                "[DebateScheduler] 真空期但找不到控场 Agent,跳过"
            )
            return

        # 选择被指派的 Agent: 在 Aggro 中等区间 + 不是上次发言者 + FSM=LISTENING
        last_speaker = self._arbitrator.metrics().last_speaker_id
        target = self._pick_vacuum_target(exclude=last_speaker)
        if target is None:
            logger.debug(
                "[DebateScheduler] 真空期但无合适补位候选,跳过"
            )
            return

        try:
            target_aggro = self._aggro.snapshot(target.agent_id).current
        except Exception:
            return

        # 调用 Arbitrator 的真空期分配
        token = await self._arbitrator.nominate_vacuum_speaker(
            agent_id=target.agent_id,
            nominator_id=nominator.agent_id,
            aggro=target_aggro,
        )
        if token is None:
            return

        # 拿到 token 后直接派发 attempt_speak (force_token 路径)
        self._spawn_speak_task_with_token(
            agent=target,
            token=token,
        )

        self._vacuum_dispatches_total += 1
        self._dispatches_total += 1
        self._last_dispatch_agent_id = target.agent_id
        self._last_dispatch_kind = RequestKind.VACUUM_FILL.value
        self._last_dispatch_at_wall_unix = time.time()

        logger.info(
            f"[DebateScheduler] VACUUM dispatch · "
            f"nominator={nominator.agent_id!r} → target={target.agent_id!r} · "
            f"silence={silence:.2f}s · aggro={target_aggro:.2f}"
        )

    def _pick_vacuum_target(
        self,
        exclude: Optional[str],
    ) -> Optional[BaseAgent]:
        """
        选择真空期补位目标:
            - 排除 exclude (上次发言者)
            - FSM 必须 LISTENING
            - Aggro 落在 [LOWER, UPPER] 区间 (中等活跃度)
            - 多人候选时挑 Aggro 最高的
        """
        candidates: list[tuple[float, BaseAgent]] = []
        for agent in self._registry.agents_iter():
            if not agent.is_registered:
                continue
            if exclude is not None and agent.agent_id == exclude:
                continue
            try:
                if agent.fsm.state != FSMState.LISTENING:
                    continue
                aggro_v = self._aggro.snapshot(agent.agent_id).current
            except Exception:
                continue

            if (
                _VACUUM_PICK_AGGRO_LOWER_BOUND
                <= aggro_v
                <= _VACUUM_PICK_AGGRO_UPPER_BOUND
            ):
                candidates.append((aggro_v, agent))

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _spawn_speak_task_with_token(
        self,
        agent: BaseAgent,
        token: MicrophoneToken,
    ) -> None:
        """与 _spawn_speak_task 类似,但带预先获取的 token。"""
        task_name = f"speak_{agent.agent_id}__vacuum_{int(time.time() * 1000)}"

        async def runner() -> None:
            try:
                await agent.attempt_speak(
                    request_kind=RequestKind.VACUUM_FILL,
                    force_token=token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    f"[DebateScheduler] vacuum attempt_speak 异常 · "
                    f"agent={agent.agent_id!r} · {exc!r}"
                )

        task = asyncio.create_task(runner(), name=task_name)
        self._active_speak_tasks.add(task)
        task.add_done_callback(self._on_speak_task_done)

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> SchedulerMetrics:
        return SchedulerMetrics(
            is_running=self.is_running,
            status=self._status,
            tick_hz=self._tick_hz,
            ticks_total=self._ticks_total,
            dispatches_total=self._dispatches_total,
            interrupt_dispatches_total=self._interrupt_dispatches_total,
            vacuum_dispatches_total=self._vacuum_dispatches_total,
            paused_skips_total=self._paused_skips_total,
            candidates_seen_total=self._candidates_seen_total,
            last_dispatch_agent_id=self._last_dispatch_agent_id,
            last_dispatch_kind=self._last_dispatch_kind,
            last_dispatch_at_wall_unix=self._last_dispatch_at_wall_unix,
            active_speak_tasks=len(self._active_speak_tasks),
        )

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "active_task_names": [
                t.get_name() for t in self._active_speak_tasks if not t.done()
            ],
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例 (Registry + Scheduler 共用)
# ──────────────────────────────────────────────────────────────────────────────


_registry_singleton: Optional[AgentRegistry] = None
_scheduler_singleton: Optional[DebateScheduler] = None


def get_agent_registry() -> AgentRegistry:
    global _registry_singleton
    if _registry_singleton is None:
        _registry_singleton = AgentRegistry()
    return _registry_singleton


def get_scheduler() -> DebateScheduler:
    global _scheduler_singleton
    if _scheduler_singleton is None:
        _scheduler_singleton = DebateScheduler(
            registry=get_agent_registry(),
            fsm_registry=get_fsm_registry(),
            aggro_engine=get_aggro_engine(),
            arbitrator=get_arbitrator(),
        )
    return _scheduler_singleton


def reset_singletons_for_testing() -> None:
    """[仅供测试] 重置 Registry + Scheduler 单例。"""
    global _registry_singleton, _scheduler_singleton
    _registry_singleton = None
    _scheduler_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 主体
    "AgentRegistry",
    "DebateScheduler",
    # 数据类 / 枚举
    "SchedulerSessionStatus",
    "SchedulerMetrics",
    # 异常
    "AgentRegistryError",
    "DuplicateAgentError",
    "UnknownAgentError",
    # 单例
    "get_agent_registry",
    "get_scheduler",
    "reset_singletons_for_testing",
]