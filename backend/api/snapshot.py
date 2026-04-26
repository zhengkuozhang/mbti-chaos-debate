"""
==============================================================================
backend/api/snapshot.py
------------------------------------------------------------------------------
REST 路由 · /api/sandbox/snapshot · 全量水合视图

定位:
    前端 HUD 大屏首次连接时调用此端点完成全量初始化水合。
    返回 CompleteSandboxSnapshot,聚合 8 大子系统快照:
        AgentRegistry / FSMRegistry / AggroEngine / Arbitrator /
        Watchdog / LLMSemaphore / ContextBus / DebateScheduler

设计原则:
    1. 任一子系统失败 → 该字段降级,不导致整个端点 500
    2. 路由层零阻塞 IO (所有快照均同步快路径)
    3. 严格按 schemas/debate_context.CompleteSandboxSnapshot 模型构造
    4. 字段与前端 protocol.d.ts 严格一一对齐
    5. session_id 进程级单例 · 启动时生成,后续不变
    6. /healthz 与 /api/sandbox/snapshot 分离,前者轻量,
       供 Docker / nginx healthcheck 复用

⚠️ 工程铁律:
    - 严禁路由层调 LLM 或阻塞 IO
    - 严禁单子系统失败连累全量响应
    - 严禁 session_id 漂移
    - 全部 Depends 拿单例,便于测试 mock

==============================================================================
"""

from __future__ import annotations

import time
import uuid
from functools import lru_cache
from typing import Final, Optional

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from loguru import logger

from backend.agents.registry import (
    AgentRegistry,
    DebateScheduler,
    SchedulerSessionStatus,
    get_agent_registry,
    get_scheduler,
)
from backend.core.aggro_engine import AggroEngine, get_aggro_engine
from backend.core.arbitrator import Arbitrator, get_arbitrator
from backend.core.context_bus import ContextBus, get_context_bus
from backend.core.fsm import FSMRegistry, get_fsm_registry
from backend.core.semaphore import LLMSemaphore, get_llm_semaphore_sync
from backend.core.watchdog import GlobalWatchdog, get_watchdog
from backend.memory.chroma_gateway import ChromaGateway, get_chroma_gateway
from backend.schemas.agent_state import (
    AgentDescriptorModel,
    AgentStateModel,
    ArbitratorMetricsModel,
    LLMSemaphoreMetricsModel,
    WatchdogMetricsModel,
)
from backend.schemas.debate_context import (
    CompleteSandboxSnapshot,
    DebateSnapshotModel,
    HealthCheckResponse,
    SessionInfoModel,
    SessionStatus,
    ShortMemoryViewModel,
    TopicStackModel,
    TurnModel,
)


# ──────────────────────────────────────────────────────────────────────────────
# 进程级会话元信息单例
# ──────────────────────────────────────────────────────────────────────────────

#: 后端版本字符串 (统一从此读取,避免散落)
_BACKEND_VERSION: Final[str] = "1.0.0"

#: 会话 ID 与启动时间 (进程级单例 · 不可重置)
_SESSION_ID: Final[str] = f"sess_{uuid.uuid4().hex[:16]}"
_SESSION_STARTED_AT_WALL_UNIX: Final[float] = time.time()
_SESSION_STARTED_AT_MONOTONIC: Final[float] = time.monotonic()


def get_session_id() -> str:
    """供其他模块查询会话 ID。"""
    return _SESSION_ID


def get_session_uptime_sec() -> float:
    """计算当前会话运行秒数。"""
    return max(0.0, time.monotonic() - _SESSION_STARTED_AT_MONOTONIC)


# ──────────────────────────────────────────────────────────────────────────────
# Scheduler 状态 → schemas SessionStatus 映射
# ──────────────────────────────────────────────────────────────────────────────


_SCHEDULER_TO_SCHEMA_STATUS: Final[
    dict[SchedulerSessionStatus, SessionStatus]
] = {
    SchedulerSessionStatus.INITIALIZING: SessionStatus.INITIALIZING,
    SchedulerSessionStatus.READY: SessionStatus.READY,
    SchedulerSessionStatus.RUNNING: SessionStatus.RUNNING,
    SchedulerSessionStatus.PAUSED: SessionStatus.PAUSED,
    SchedulerSessionStatus.SHUTTING_DOWN: SessionStatus.SHUTTING_DOWN,
}


def _map_status(s: SchedulerSessionStatus) -> SessionStatus:
    return _SCHEDULER_TO_SCHEMA_STATUS.get(s, SessionStatus.READY)


# ──────────────────────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/api/sandbox",
    tags=["sandbox"],
)


# ──────────────────────────────────────────────────────────────────────────────
# 依赖注入 · 集中管理 (便于测试 mock)
# ──────────────────────────────────────────────────────────────────────────────


def _dep_agent_registry() -> AgentRegistry:
    return get_agent_registry()


def _dep_scheduler() -> DebateScheduler:
    return get_scheduler()


def _dep_fsm_registry() -> FSMRegistry:
    return get_fsm_registry()


def _dep_aggro_engine() -> AggroEngine:
    return get_aggro_engine()


def _dep_arbitrator() -> Arbitrator:
    return get_arbitrator()


def _dep_watchdog() -> GlobalWatchdog:
    return get_watchdog()


def _dep_semaphore() -> LLMSemaphore:
    # 同步版本仅在已知无并发的路径调用 (FastAPI 路由本身是异步,
    # 但本依赖只读取已存在单例引用,不创建新实例)
    return get_llm_semaphore_sync()


def _dep_context_bus() -> ContextBus:
    return get_context_bus()


def _dep_chroma_gateway() -> ChromaGateway:
    return get_chroma_gateway()


# ──────────────────────────────────────────────────────────────────────────────
# /healthz · 轻量探活端点
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/healthz",
    response_model=HealthCheckResponse,
    summary="健康检查 · Docker / nginx healthcheck 复用",
    description=(
        "极简端点,仅返回 status + session_id + uptime。"
        "不聚合任何重型子系统,严禁阻塞 IO。"
    ),
    status_code=status.HTTP_200_OK,
)
async def healthz(
    scheduler: DebateScheduler = Depends(_dep_scheduler),
) -> HealthCheckResponse:
    sched_status = "ok"
    try:
        if scheduler.status == SchedulerSessionStatus.SHUTTING_DOWN:
            sched_status = "shutting_down"
        elif not scheduler.is_running:
            # Scheduler 没在跑,但其他子系统可能仍正常 → degraded
            sched_status = "degraded"
    except Exception:
        sched_status = "degraded"

    # 类型对齐: HealthCheckResponse.status 是 Literal["ok"|"degraded"|"shutting_down"]
    return HealthCheckResponse(
        status=sched_status,  # type: ignore[arg-type]
        session_id=_SESSION_ID,
        uptime_seconds=round(get_session_uptime_sec(), 3),
        backend_version=_BACKEND_VERSION,
    )


# ──────────────────────────────────────────────────────────────────────────────
# /api/sandbox/snapshot · 全量水合
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/snapshot",
    response_model=CompleteSandboxSnapshot,
    summary="HUD 全量水合视图",
    description=(
        "聚合 AgentRegistry / FSM / Aggro / Arbitrator / Watchdog / "
        "Semaphore / ContextBus 等子系统快照,前端 HUD 启动时一次性拉取,"
        "后续增量通过 WebSocket /ws/debate 维护。"
    ),
    status_code=status.HTTP_200_OK,
)
async def get_sandbox_snapshot(
    registry: AgentRegistry = Depends(_dep_agent_registry),
    scheduler: DebateScheduler = Depends(_dep_scheduler),
    fsm_registry: FSMRegistry = Depends(_dep_fsm_registry),
    aggro_engine: AggroEngine = Depends(_dep_aggro_engine),
    arbitrator: Arbitrator = Depends(_dep_arbitrator),
    watchdog: GlobalWatchdog = Depends(_dep_watchdog),
    semaphore: LLMSemaphore = Depends(_dep_semaphore),
    bus: ContextBus = Depends(_dep_context_bus),
) -> CompleteSandboxSnapshot:
    """
    全量水合 · 单子系统失败降级,绝不让整个端点 500。
    """
    # ── 1. SessionInfoModel ──
    session_info = _build_session_info(scheduler=scheduler, bus=bus)

    # ── 2. Agents ──
    agents = _build_agent_state_list(
        registry=registry,
        fsm_registry=fsm_registry,
        aggro_engine=aggro_engine,
        arbitrator=arbitrator,
    )

    # ── 3. Arbitrator ──
    arb_model = _build_arbitrator_metrics(arbitrator)

    # ── 4. Watchdog ──
    wd_model = _build_watchdog_metrics(watchdog)

    # ── 5. Semaphore ──
    sem_model = _build_semaphore_metrics(semaphore)

    # ── 6. Debate (议题栈 + 短记忆) ──
    debate_model = _build_debate_snapshot(bus)

    return CompleteSandboxSnapshot(
        session=session_info,
        debate=debate_model,
        agents=agents,
        arbitrator=arb_model,
        watchdog=wd_model,
        semaphore=sem_model,
        server_at_wall_unix=round(time.time(), 6),
    )


# ──────────────────────────────────────────────────────────────────────────────
# /api/sandbox/diagnostic · 隐藏诊断端点
# ──────────────────────────────────────────────────────────────────────────────


@router.get(
    "/diagnostic",
    summary="所有子系统的原始 snapshot_dict (诊断专用)",
    description=(
        "返回所有子系统未经契约层包装的原始诊断信息。"
        "供运维 / 故障复盘使用,前端 HUD *不应* 依赖此端点。"
    ),
    response_class=JSONResponse,
)
async def get_diagnostic(
    registry: AgentRegistry = Depends(_dep_agent_registry),
    scheduler: DebateScheduler = Depends(_dep_scheduler),
    fsm_registry: FSMRegistry = Depends(_dep_fsm_registry),
    aggro_engine: AggroEngine = Depends(_dep_aggro_engine),
    arbitrator: Arbitrator = Depends(_dep_arbitrator),
    watchdog: GlobalWatchdog = Depends(_dep_watchdog),
    semaphore: LLMSemaphore = Depends(_dep_semaphore),
    bus: ContextBus = Depends(_dep_context_bus),
    chroma: ChromaGateway = Depends(_dep_chroma_gateway),
) -> JSONResponse:
    """
    各子系统失败 → 字段为 {"error": str}。绝不抛 500。
    """
    payload: dict[str, object] = {
        "session_id": _SESSION_ID,
        "session_uptime_sec": round(get_session_uptime_sec(), 3),
        "server_at_wall_unix": round(time.time(), 6),
    }

    payload["scheduler"] = _safe_dict(
        lambda: scheduler.snapshot_dict(),
        fallback_label="scheduler",
    )
    payload["fsm"] = _safe_dict(
        lambda: fsm_registry.snapshot_dict(),
        fallback_label="fsm",
    )
    payload["aggro"] = _safe_dict(
        lambda: aggro_engine.snapshot_dict(),
        fallback_label="aggro",
    )
    payload["arbitrator"] = _safe_dict(
        lambda: arbitrator.snapshot_dict(),
        fallback_label="arbitrator",
    )
    payload["watchdog"] = _safe_dict(
        lambda: watchdog.snapshot_dict(),
        fallback_label="watchdog",
    )
    payload["semaphore"] = _safe_dict(
        lambda: semaphore.metrics().to_dict(),
        fallback_label="semaphore",
    )
    payload["context_bus"] = _safe_dict(
        lambda: bus.snapshot_dict(),
        fallback_label="context_bus",
    )
    payload["chroma_gateway"] = _safe_dict(
        lambda: chroma.snapshot_dict(),
        fallback_label="chroma_gateway",
    )
    payload["agents_descriptors"] = _safe_dict(
        lambda: {
            aid: desc.model_dump(mode="json")
            for aid, desc in registry.descriptors_dict().items()
        },
        fallback_label="agents_descriptors",
    )

    # Summarizer 单独走 (其单例需 await 获取,这里同步路径只能尝试同步引用)
    payload["summarizer"] = _safe_dict(
        lambda: _try_get_summarizer_snapshot(),
        fallback_label="summarizer",
    )

    return JSONResponse(content=payload, status_code=200)


# ──────────────────────────────────────────────────────────────────────────────
# 内部装配函数 (单子系统失败降级)
# ──────────────────────────────────────────────────────────────────────────────


def _build_session_info(
    *,
    scheduler: DebateScheduler,
    bus: ContextBus,
) -> SessionInfoModel:
    """构造 SessionInfoModel · 失败降级到 INITIALIZING 状态。"""
    try:
        sched_status = scheduler.status
        schema_status = _map_status(sched_status)
    except Exception as exc:
        logger.warning(f"[snapshot] 读取 Scheduler 状态失败: {exc!r}")
        schema_status = SessionStatus.INITIALIZING

    # 累计指标 (来自 Arbitrator + ContextBus)
    total_turns: int = 0
    total_interrupts: int = 0
    try:
        # 累计 Turn 数 = 短记忆容量 + 溢出已写出量 (近似)
        # ContextBus 没暴露 cumulative,只能用 short_memory_size + overflow
        total_turns = bus.short_memory_size() + bus.overflow_queue_size()
    except Exception:
        pass
    try:
        from backend.core.arbitrator import get_arbitrator as _ga

        m = _ga().metrics()
        total_interrupts = m.interrupt_granted_total
    except Exception:
        pass

    return SessionInfoModel(
        session_id=_SESSION_ID,
        status=schema_status,
        started_at_wall_unix=round(_SESSION_STARTED_AT_WALL_UNIX, 6),
        uptime_seconds=round(get_session_uptime_sec(), 3),
        total_turns_sealed=total_turns,
        total_interrupts=total_interrupts,
        backend_version=_BACKEND_VERSION,
    )


def _build_agent_state_list(
    *,
    registry: AgentRegistry,
    fsm_registry: FSMRegistry,
    aggro_engine: AggroEngine,
    arbitrator: Arbitrator,
) -> list[AgentStateModel]:
    """
    装配 8 个 AgentStateModel · 单 Agent 失败 → 跳过,记录日志。

    使用 AgentStateModel.assemble 工厂方法集中处理"内部快照 → 对外卡片"映射。
    """
    out: list[AgentStateModel] = []
    descriptors = _safe_get_descriptors(registry)
    if not descriptors:
        return out

    current_token = None
    aggro_min = 0.0
    aggro_max = 100.0
    interrupt_th = 85.0

    try:
        current_token = arbitrator.current_holder()
    except Exception:
        current_token = None

    try:
        aggro_min = aggro_engine.aggro_min
        aggro_max = aggro_engine.aggro_max
        interrupt_th = aggro_engine.interrupt_threshold
    except Exception:
        pass

    for agent_id, desc in descriptors.items():
        try:
            fsm_snap = fsm_registry.get(agent_id).snapshot()
            aggro_snap = aggro_engine.snapshot(agent_id)
            state_model = AgentStateModel.assemble(
                descriptor=desc,
                fsm_snap=fsm_snap,
                aggro_snap=aggro_snap,
                aggro_min=aggro_min,
                aggro_max=aggro_max,
                interrupt_threshold=interrupt_th,
                current_token=current_token,
            )
            out.append(state_model)
        except Exception as exc:
            logger.warning(
                f"[snapshot] 装配 Agent {agent_id!r} 失败 (跳过): {exc!r}"
            )
            continue

    return out


def _safe_get_descriptors(
    registry: AgentRegistry,
) -> dict[str, AgentDescriptorModel]:
    try:
        return registry.descriptors_dict()
    except Exception as exc:
        logger.warning(f"[snapshot] descriptors_dict 失败: {exc!r}")
        return {}


def _build_arbitrator_metrics(
    arbitrator: Arbitrator,
) -> ArbitratorMetricsModel:
    """ArbitratorMetricsModel · 失败降级到全 0 默认。"""
    try:
        m = arbitrator.metrics()
        return ArbitratorMetricsModel.from_internal(m)
    except Exception as exc:
        logger.warning(f"[snapshot] arbitrator.metrics 失败: {exc!r}")
        return ArbitratorMetricsModel(
            current_holder_id=None,
            current_epoch=0,
            current_token_age_sec=0.0,
            granted_total=0,
            interrupt_granted_total=0,
            rejected_total=0,
            debounce_drop_total=0,
            last_speaker_id=None,
            cooldown_remaining_sec=0.0,
            seconds_since_any_speech=0.0,
        )


def _build_watchdog_metrics(
    watchdog: GlobalWatchdog,
) -> WatchdogMetricsModel:
    try:
        m = watchdog.metrics()
        return WatchdogMetricsModel.from_internal(m)
    except Exception as exc:
        logger.warning(f"[snapshot] watchdog.metrics 失败: {exc!r}")
        return WatchdogMetricsModel(
            is_running=False,
            in_flight_count=0,
            soft_evict_total=0,
            hard_evict_total=0,
            cancelled_total=0,
            last_eviction=None,
        )


def _build_semaphore_metrics(
    semaphore: LLMSemaphore,
) -> LLMSemaphoreMetricsModel:
    try:
        m = semaphore.metrics()
        return LLMSemaphoreMetricsModel.from_internal(m)
    except Exception as exc:
        logger.warning(f"[snapshot] semaphore.metrics 失败: {exc!r}")
        # 降级: capacity 至少 1,其他 0
        return LLMSemaphoreMetricsModel(
            capacity=1,
            held_count=0,
            waiting_count=0,
            acquired_total=0,
            released_total=0,
            timeout_total=0,
            cancelled_total=0,
            last_acquire_wait_ms=0.0,
            holders=[],
        )


def _build_debate_snapshot(bus: ContextBus) -> DebateSnapshotModel:
    """构造 DebateSnapshotModel · 任何子项失败降级到空。"""
    # 议题栈
    try:
        topic_stack = TopicStackModel.from_internal_stack(
            bus.topic_stack_snapshot()
        )
    except Exception as exc:
        logger.warning(f"[snapshot] topic_stack 装配失败: {exc!r}")
        topic_stack = TopicStackModel(current=None, stack=[], depth=0)

    # 短记忆窗口
    short_memory = _build_short_memory_view(bus)

    # 进行中 Agent
    try:
        ongoing_ids: list[str] = []
        # ContextBus 没暴露 ongoing_agent_ids 的公开方法,通过 snapshot_dict 间接拿
        snap = bus.snapshot_dict()
        ongoing = snap.get("ongoing_agents", [])
        if isinstance(ongoing, list):
            ongoing_ids = [str(x) for x in ongoing if isinstance(x, str)]
    except Exception as exc:
        logger.warning(f"[snapshot] ongoing_agents 装配失败: {exc!r}")
        ongoing_ids = []

    return DebateSnapshotModel(
        topic_stack=topic_stack,
        short_memory=short_memory,
        ongoing_agent_ids=ongoing_ids,
    )


def _build_short_memory_view(bus: ContextBus) -> ShortMemoryViewModel:
    try:
        turns_internal = bus.short_memory_snapshot()
        # 装配 TurnModel,单条失败跳过
        turn_models: list[TurnModel] = []
        for t in turns_internal:
            try:
                turn_models.append(TurnModel.from_internal(t))
            except Exception as exc:
                logger.debug(
                    f"[snapshot] TurnModel.from_internal 失败 (跳过): {exc!r}"
                )
                continue
        size = len(turns_internal)
        # 取 window_size: 通过 snapshot_dict 间接读取
        window_size = _read_window_size(bus)
        overflow_size = bus.overflow_queue_size()
        return ShortMemoryViewModel(
            turns=turn_models,
            window_size=window_size,
            current_size=size,
            overflow_queue_size=overflow_size,
        )
    except Exception as exc:
        logger.warning(f"[snapshot] short_memory 装配失败: {exc!r}")
        return ShortMemoryViewModel(
            turns=[],
            window_size=8,
            current_size=0,
            overflow_queue_size=0,
        )


@lru_cache(maxsize=1)
def _read_window_size(bus: ContextBus) -> int:
    """缓存读取 window_size (settings.SHORT_MEMORY_WINDOW_TURNS)。"""
    try:
        from backend.core.config import get_settings

        return get_settings().SHORT_MEMORY_WINDOW_TURNS
    except Exception:
        return 8


# ──────────────────────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────────────────────


def _safe_dict(
    factory,
    *,
    fallback_label: str,
) -> dict[str, object]:
    """
    安全调用一个返回 dict 的 factory · 失败返回 {"error": str}。
    """
    try:
        out = factory()
        if not isinstance(out, dict):
            return {"error": f"non-dict from {fallback_label}"}
        return out
    except Exception as exc:
        logger.debug(
            f"[snapshot] safe_dict({fallback_label}) 失败: {exc!r}"
        )
        return {"error": f"{type(exc).__name__}: {exc}"}


def _try_get_summarizer_snapshot() -> dict[str, object]:
    """
    尝试同步获取 Summarizer 单例的 snapshot_dict。

    Summarizer 单例创建是 async 的,但 snapshot_dict 本身是同步方法。
    我们用一个轻量探测:模块全局单例若存在则返回其 snapshot,否则返回未启动。
    """
    try:
        from backend.memory.summarizer import _summarizer_singleton  # type: ignore

        if _summarizer_singleton is None:
            return {"is_running": False, "note": "summarizer not initialized"}
        return _summarizer_singleton.snapshot_dict()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "router",
    "get_session_id",
    "get_session_uptime_sec",
]