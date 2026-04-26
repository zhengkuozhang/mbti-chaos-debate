"""
==============================================================================
backend/main.py
------------------------------------------------------------------------------
FastAPI 应用入口 · 所有子系统的总装地

定位:
    把"基础设施 + 核心控制 + 契约 + 记忆 + 8 Agents + API 路由"
    像积木一样按正确顺序拼起来,让整个后端"开机即活"。

启动顺序 (lifespan startup):
    1.  Settings 加载 + 启动 banner
    2.  ChromaGateway 启动
    3.  LLMSemaphore 异步初始化 (capacity 由 settings 决定)
    4.  AggroEngine / Arbitrator (同步单例,无需 start)
    5.  Watchdog 启动
    6.  ThrottleHub 启动
    7.  8 Agents 装配 + register_with_runtime (按固定顺序)
    8.  ContextBus → ThrottleHub 桥接 (subscribe_sealed / subscribe_topic)
    9.  Summarizer 启动 (后台 Worker)
    10. DebateScheduler 启动 (状态保持 READY,等待议题注入)

停止顺序 (lifespan shutdown, 严格反向):
    1.  DebateScheduler stop (drain in-flight 任务)
    2.  Summarizer stop
    3.  ContextBus emergency_seal_all (紧急封存所有 ongoing turns)
    4.  Watchdog stop
    5.  ThrottleHub stop (广播 BYE)
    6.  LLMRouter shutdown (httpx clients close)
    7.  ChromaGateway stop

设计原则:
    1. 关键路径失败仍尝试挂起 HTTP (降级路径 + degraded health)
    2. 所有 stop 走 best-effort,中途错误不影响后续清理
    3. ContextBus 桥接必须在 ThrottleHub 启动后才注册
    4. 全局 Exception handler 防 5xx 泄漏到客户端
    5. Banner 仅在 startup 打印一次

⚠️ 工程铁律:
    - uvicorn 必须以 --workers 1 启动 (信号量是进程级)
    - lifespan 中任何启动失败都必须降级而非崩溃
    - stop 路径全部 try/except,绝不允许一个子系统的 stop 错误阻塞其他
    - 严禁直接 import 8 个 Agent 类 (走 build_xxx 工厂函数,避免循环依赖风险)

==============================================================================
"""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import orjson
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse
from loguru import logger

from backend.agents.enfp_jester import build_enfp_jester
from backend.agents.entp_debater import build_entp_debater
from backend.agents.estp_conductor import build_estp_conductor
from backend.agents.infp_empath import build_infp_empath
from backend.agents.intj_logician import build_intj_logician
from backend.agents.isfj_archivist import build_isfj_archivist
from backend.agents.isfp_chameleon import build_isfp_chameleon
from backend.agents.istj_guardian import build_istj_guardian
from backend.agents.registry import (
    AgentRegistry,
    DebateScheduler,
    SchedulerSessionStatus,
    get_agent_registry,
    get_scheduler,
)
from backend.api import control as control_router_module
from backend.api import snapshot as snapshot_router_module
from backend.api import websocket as websocket_router_module
from backend.core.aggro_engine import get_aggro_engine
from backend.core.arbitrator import get_arbitrator
from backend.core.config import get_settings
from backend.core.context_bus import (
    TopicChangedEvent,
    TurnSealedEvent,
    get_context_bus,
)
from backend.core.fsm import get_fsm_registry
from backend.core.llm_router import shutdown_llm_router
from backend.core.semaphore import get_llm_semaphore
from backend.core.throttle import get_throttle_hub
from backend.core.watchdog import get_watchdog
from backend.memory.chroma_gateway import get_chroma_gateway
from backend.memory.summarizer import get_summarizer


# ──────────────────────────────────────────────────────────────────────────────
# 应用元信息
# ──────────────────────────────────────────────────────────────────────────────

_APP_TITLE = "MBTI Chaos Debate Sandbox"
_APP_DESCRIPTION = (
    "本地多 Agent MBTI 辩论沙盒 · 完全离线 · "
    "Ollama (Metal) + FastAPI + ChromaDB + Vue 3 HUD"
)
_APP_VERSION = "1.0.0"


# ──────────────────────────────────────────────────────────────────────────────
# Loguru 配置 (启动时初始化)
# ──────────────────────────────────────────────────────────────────────────────


def _configure_logging(log_level: str) -> None:
    """统一 loguru 输出格式 + 等级。"""
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level.upper(),
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> "
            "<level>{level: <7}</level> "
            "<cyan>{name}:{line}</cyan> "
            "<level>{message}</level>"
        ),
        backtrace=False,
        diagnose=False,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan: startup + shutdown 完整编排
# ──────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    应用生命周期编排器。

    启动顺序中任何子系统失败会:
        - 记录 ERROR
        - 标记 app.state.startup_failures
        - 但不抛出 (允许 HTTP 挂起以暴露 /healthz degraded)

    停止顺序按反向依赖串行执行,best-effort。
    """
    settings = get_settings()
    _configure_logging(settings.LOG_LEVEL)

    # Banner
    try:
        banner = settings.render_startup_banner()
        for line in banner.splitlines():
            logger.info(line)
    except Exception as exc:
        logger.warning(f"[main] banner 渲染异常 (忽略): {exc!r}")

    app.state.startup_t0 = time.monotonic()
    app.state.startup_failures = []  # type: ignore[attr-defined]
    app.state.contextbus_subscriptions = []  # type: ignore[attr-defined]

    # ╔════════════════════════════════════════════════════════════╗
    # ║ STARTUP                                                    ║
    # ╚════════════════════════════════════════════════════════════╝

    # 1) ChromaGateway
    try:
        chroma = get_chroma_gateway()
        await chroma.start()
        logger.info("[main] ✅ ChromaGateway 已启动")
    except Exception as exc:
        logger.error(f"[main] ❌ ChromaGateway 启动失败: {exc!r}")
        app.state.startup_failures.append(
            f"chroma_gateway: {type(exc).__name__}"
        )

    # 2) LLMSemaphore
    try:
        sem = await get_llm_semaphore()
        logger.info(
            f"[main] ✅ LLMSemaphore 已就绪 · capacity={sem.capacity}"
        )
    except Exception as exc:
        logger.error(f"[main] ❌ LLMSemaphore 初始化失败: {exc!r}")
        app.state.startup_failures.append(
            f"llm_semaphore: {type(exc).__name__}"
        )

    # 3) AggroEngine / Arbitrator (同步单例,仅触发实例化)
    try:
        get_aggro_engine()
        get_arbitrator()
        get_fsm_registry()
        logger.info("[main] ✅ AggroEngine / Arbitrator / FSMRegistry 已就绪")
    except Exception as exc:
        logger.error(
            f"[main] ❌ AggroEngine/Arbitrator 单例化失败: {exc!r}"
        )
        app.state.startup_failures.append(
            f"core_singletons: {type(exc).__name__}"
        )

    # 4) Watchdog
    try:
        watchdog = get_watchdog()
        await watchdog.start()
        logger.info("[main] ✅ Watchdog 已启动")
    except Exception as exc:
        logger.error(f"[main] ❌ Watchdog 启动失败: {exc!r}")
        app.state.startup_failures.append(
            f"watchdog: {type(exc).__name__}"
        )

    # 5) ThrottleHub
    try:
        hub = get_throttle_hub()
        await hub.start()
        logger.info("[main] ✅ ThrottleHub 已启动")
    except Exception as exc:
        logger.error(f"[main] ❌ ThrottleHub 启动失败: {exc!r}")
        app.state.startup_failures.append(
            f"throttle_hub: {type(exc).__name__}"
        )

    # 6) 8 Agents 装配 + register_with_runtime
    try:
        registry = get_agent_registry()
        _build_and_register_all_agents(registry)
        logger.info(
            f"[main] ✅ 8 Agents 装配完成 · "
            f"total_registered={len(registry)}"
        )
    except Exception as exc:
        logger.error(f"[main] ❌ Agents 装配失败: {exc!r}")
        app.state.startup_failures.append(
            f"agents_registration: {type(exc).__name__}"
        )

    # 7) ContextBus → ThrottleHub 桥接钩子
    try:
        await _wire_contextbus_to_hub(app)
        logger.info("[main] ✅ ContextBus → ThrottleHub 桥接已注册")
    except Exception as exc:
        logger.error(f"[main] ❌ ContextBus 桥接失败: {exc!r}")
        app.state.startup_failures.append(
            f"contextbus_bridge: {type(exc).__name__}"
        )

    # 8) Summarizer
    try:
        summarizer = await get_summarizer()
        await summarizer.start()
        logger.info("[main] ✅ Summarizer 已启动")
    except Exception as exc:
        logger.error(f"[main] ❌ Summarizer 启动失败: {exc!r}")
        app.state.startup_failures.append(
            f"summarizer: {type(exc).__name__}"
        )

    # 9) DebateScheduler
    try:
        scheduler = get_scheduler()
        await scheduler.start()
        # 状态保持 READY · 等待用户注入议题后切到 RUNNING
        logger.info(
            f"[main] ✅ DebateScheduler 已启动 · "
            f"status={scheduler.status.value} (等待议题注入)"
        )
    except Exception as exc:
        logger.error(f"[main] ❌ DebateScheduler 启动失败: {exc!r}")
        app.state.startup_failures.append(
            f"scheduler: {type(exc).__name__}"
        )

    # ── Startup 总结 ──
    startup_elapsed_ms = (time.monotonic() - app.state.startup_t0) * 1000
    if app.state.startup_failures:
        logger.warning(
            f"[main] ⚠️ Startup 完成 (耗时 {startup_elapsed_ms:.0f}ms),"
            f"但有 {len(app.state.startup_failures)} 个子系统失败:"
        )
        for f in app.state.startup_failures:
            logger.warning(f"[main]    • {f}")
        logger.warning(
            "[main] /api/sandbox/healthz 将返回 degraded 状态"
        )
    else:
        logger.info(
            f"[main] 🎉 Startup 完成 (耗时 {startup_elapsed_ms:.0f}ms) · "
            f"全部子系统就绪"
        )

    logger.info(
        f"[main] 🎯 Health check: GET http://localhost:"
        f"{settings.BACKEND_PORT}/api/sandbox/healthz"
    )
    logger.info(
        f"[main] 🎯 Snapshot:    GET http://localhost:"
        f"{settings.BACKEND_PORT}/api/sandbox/snapshot"
    )
    logger.info(
        f"[main] 🎯 WebSocket:   WS  ws://localhost:"
        f"{settings.BACKEND_PORT}/ws/debate"
    )

    # ════════════════════════════════════════════════════════════
    # 让 yield 持续到 shutdown
    # ════════════════════════════════════════════════════════════
    try:
        yield
    finally:
        await _do_shutdown(app)


# ──────────────────────────────────────────────────────────────────────────────
# 内部: 8 Agents 装配
# ──────────────────────────────────────────────────────────────────────────────


def _build_and_register_all_agents(registry: AgentRegistry) -> None:
    """
    按固定顺序装配 8 个 Agent · 单 Agent 失败仅记录,继续装配其他。

    顺序: NT → NF → SJ → SP (与 HUD 阵营颜色顺序一致)
    """
    builders = [
        ("INTJ_logician", build_intj_logician),
        ("ENTP_debater", build_entp_debater),
        ("INFP_empath", build_infp_empath),
        ("ENFP_jester", build_enfp_jester),
        ("ISTJ_guardian", build_istj_guardian),
        ("ISFJ_archivist", build_isfj_archivist),
        ("ESTP_conductor", build_estp_conductor),
        ("ISFP_chameleon", build_isfp_chameleon),
    ]

    # Step 1: 实例化并加入 Registry
    for agent_id, builder in builders:
        try:
            agent = builder()
            registry.register(agent)
            logger.debug(f"[main] Agent 实例化: {agent_id}")
        except Exception as exc:
            logger.error(
                f"[main] Agent 实例化失败 · id={agent_id} · {exc!r}"
            )

    # Step 2: 一站式注册到 runtime (FSM/Aggro/Bus)
    success = registry.register_all_with_runtime()
    if success != len(registry):
        logger.warning(
            f"[main] register_all_with_runtime: "
            f"{success}/{len(registry)} 个 Agent 注册成功"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 内部: ContextBus → ThrottleHub 桥接
# ──────────────────────────────────────────────────────────────────────────────


async def _wire_contextbus_to_hub(app: FastAPI) -> None:
    """
    把 ContextBus 的领域事件桥接到 ThrottleHub 的 WebSocket 广播通道。

    桥接两类:
        1. TurnSealedEvent → hub.broadcast_turn_sealed
        2. TopicChangedEvent → hub.broadcast_topic
    """
    bus = get_context_bus()
    hub = get_throttle_hub()

    async def _on_turn_sealed(event: TurnSealedEvent) -> None:
        try:
            await hub.broadcast_turn_sealed(turn=event.turn)
        except Exception as exc:
            logger.debug(
                f"[main:bridge] broadcast_turn_sealed 异常 (忽略): {exc!r}"
            )

    async def _on_topic_changed(event: TopicChangedEvent) -> None:
        try:
            await hub.broadcast_topic(
                event_kind=event.kind,
                current_topic=event.current_topic,
                stack_depth=event.stack_depth,
            )
        except Exception as exc:
            logger.debug(
                f"[main:bridge] broadcast_topic 异常 (忽略): {exc!r}"
            )

    bus.subscribe_sealed(_on_turn_sealed)
    bus.subscribe_topic(_on_topic_changed)

    # 持有引用避免被 GC (subscribe 内部已 strong-ref,这里再保一道)
    app.state.contextbus_subscriptions.extend(  # type: ignore[attr-defined]
        [_on_turn_sealed, _on_topic_changed]
    )


# ──────────────────────────────────────────────────────────────────────────────
# 内部: Shutdown 编排 (best-effort,严格反向)
# ──────────────────────────────────────────────────────────────────────────────


async def _do_shutdown(app: FastAPI) -> None:
    """
    Shutdown 编排 · best-effort · 严格反向顺序。

    任一步失败仅记录,不影响后续步骤。
    """
    logger.info("[main] 🛑 lifespan SHUTDOWN 开始")
    t0 = time.monotonic()

    # 1) DebateScheduler stop (drain in-flight 任务)
    await _safe_run("DebateScheduler.stop", _stop_scheduler)

    # 2) Summarizer stop
    await _safe_run("Summarizer.stop", _stop_summarizer)

    # 3) ContextBus emergency_seal_all
    await _safe_run(
        "ContextBus.emergency_seal_all", _emergency_seal_contextbus
    )

    # 4) Watchdog stop
    await _safe_run("Watchdog.stop", _stop_watchdog)

    # 5) ThrottleHub stop (内部会广播 BYE)
    await _safe_run("ThrottleHub.stop", _stop_throttle_hub)

    # 6) LLMRouter shutdown (httpx clients close)
    await _safe_run("LLMRouter.shutdown", shutdown_llm_router)

    # 7) ChromaGateway stop
    await _safe_run("ChromaGateway.stop", _stop_chroma_gateway)

    elapsed_ms = (time.monotonic() - t0) * 1000
    logger.info(
        f"[main] 🛑 lifespan SHUTDOWN 完成 (耗时 {elapsed_ms:.0f}ms)"
    )


async def _safe_run(label: str, coro_factory) -> None:
    """容错调用 · coro_factory 是 0 参 async 函数 / 协程对象。"""
    try:
        result = coro_factory()
        if asyncio.iscoroutine(result):
            await result
        logger.info(f"[main] [shutdown] ✅ {label}")
    except Exception as exc:
        logger.warning(f"[main] [shutdown] ⚠️ {label} 异常 (继续): {exc!r}")


async def _stop_scheduler() -> None:
    sched: DebateScheduler = get_scheduler()
    sched.set_status(SchedulerSessionStatus.SHUTTING_DOWN)
    await sched.stop(drain_timeout_sec=5.0)


async def _stop_summarizer() -> None:
    summ = await get_summarizer()
    await summ.stop()


async def _emergency_seal_contextbus() -> None:
    bus = get_context_bus()
    await bus.emergency_seal_all()


async def _stop_watchdog() -> None:
    wd = get_watchdog()
    await wd.stop()


async def _stop_throttle_hub() -> None:
    hub = get_throttle_hub()
    await hub.stop()


async def _stop_chroma_gateway() -> None:
    gw = get_chroma_gateway()
    await gw.stop()


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI 应用工厂
# ──────────────────────────────────────────────────────────────────────────────


def create_app() -> FastAPI:
    """构造 FastAPI 应用实例。"""
    settings = get_settings()

    app = FastAPI(
        title=_APP_TITLE,
        description=_APP_DESCRIPTION,
        version=_APP_VERSION,
        lifespan=lifespan,
        default_response_class=ORJSONResponse,
        # 关闭交互式 docs 的密码保护 (本机部署)
        docs_url="/docs" if settings.ENABLE_DOCS else None,
        redoc_url="/redoc" if settings.ENABLE_DOCS else None,
    )

    # ── CORS ──
    cors_origins = list(settings.CORS_ALLOWED_ORIGINS)
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["*"],
            max_age=600,
        )
        logger.info(
            f"[main] CORS allowed origins: {cors_origins}"
        )

    # ── 路由挂载 ──
    app.include_router(snapshot_router_module.router)
    app.include_router(control_router_module.router)
    app.include_router(websocket_router_module.router)

    # ── 全局异常处理器 ──
    @app.exception_handler(Exception)
    async def _global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # WebSocket 异常会有自己的处理路径 · 此 handler 主要管 HTTP
        logger.error(
            f"[main] 全局异常处理器命中 · path={request.url.path} · "
            f"err={exc!r}"
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "internal_server_error",
                "type": type(exc).__name__,
                "message": str(exc)[:512],
                "path": str(request.url.path),
            },
        )

    # ── 根端点 (友好提示) ──
    @app.get("/", include_in_schema=False)
    async def _root() -> dict:
        return {
            "app": _APP_TITLE,
            "version": _APP_VERSION,
            "endpoints": {
                "health": "/api/sandbox/healthz",
                "snapshot": "/api/sandbox/snapshot",
                "diagnostic": "/api/sandbox/diagnostic",
                "websocket": "/ws/debate",
                "docs": "/docs" if settings.ENABLE_DOCS else None,
            },
        }

    return app


# ──────────────────────────────────────────────────────────────────────────────
# 模块级实例 (uvicorn 入口: backend.main:app)
# ──────────────────────────────────────────────────────────────────────────────


app: Optional[FastAPI] = None
try:
    app = create_app()
except Exception as exc:
    # create_app 失败 → 模块加载失败 · uvicorn 会终止
    logger.critical(
        f"[main] create_app 致命异常,服务无法启动: {exc!r}"
    )
    raise


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = ["app", "create_app", "lifespan"]