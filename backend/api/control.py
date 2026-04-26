"""
==============================================================================
backend/api/control.py
------------------------------------------------------------------------------
REST 控制 API · 与 WebSocket 控制路径互补

定位:
    提供 curl / Postman / 前端按钮的 HTTP 入口,功能与 WS 入站指令完全对齐:

        WS 入站 InboundKind            REST 端点
        ───────────────────────────    ─────────────────────────────
        INJECT_TOPIC                   POST /api/control/topic/inject
        POP_TOPIC                      POST /api/control/topic/pop
        PAUSE_SESSION                  POST /api/control/action  (action=PAUSE)
        RESUME_SESSION                 POST /api/control/action  (action=RESUME)
        FORCE_INTERRUPT                POST /api/control/action  (action=FORCE_INTERRUPT)
        (无对应)                        POST /api/control/action  (action=EMERGENCY_STOP)

设计原则:
    1. 严格按 schemas/debate_context 的请求/响应模型构造
    2. 状态机校验前置 (幂等成功 OR 4xx 冲突)
    3. 审计日志 (所有动作记录 reason · info 级别)
    4. 错误隔离 · 5xx 必走 ControlActionResponse 包装,不裸抛
    5. EMERGENCY_STOP 仅切 SHUTTING_DOWN 状态,*不调用 sys.exit*

⚠️ 工程铁律:
    - 严禁绕过 ContextBus 直接操作议题栈
    - 严禁 EMERGENCY_STOP 触发硬关进程 (lifespan 才是关闭主路径)
    - 严禁议题注入后忘记切到 RUNNING (auto_start_debate 参数 + 状态校验)
    - 状态切换必须经 set_status,绝不直接赋值

==============================================================================
"""

from __future__ import annotations

import time
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger

from backend.agents.registry import (
    DebateScheduler,
    SchedulerSessionStatus,
    get_scheduler,
)
from backend.core.arbitrator import Arbitrator, get_arbitrator
from backend.core.context_bus import (
    ContextBus,
    EmptyTopicStackError,
    get_context_bus,
)
from backend.core.throttle import ThrottleHub, get_throttle_hub
from backend.schemas.debate_context import (
    ControlActionKind,
    ControlActionRequest,
    ControlActionResponse,
    InjectTopicRequest,
    InjectTopicResponse,
    PopTopicResponse,
    SessionStatus,
    TopicModel,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: Scheduler 状态 → schemas SessionStatus 映射
_SCHEDULER_TO_SCHEMA: Final[
    dict[SchedulerSessionStatus, SessionStatus]
] = {
    SchedulerSessionStatus.INITIALIZING: SessionStatus.INITIALIZING,
    SchedulerSessionStatus.READY: SessionStatus.READY,
    SchedulerSessionStatus.RUNNING: SessionStatus.RUNNING,
    SchedulerSessionStatus.PAUSED: SessionStatus.PAUSED,
    SchedulerSessionStatus.SHUTTING_DOWN: SessionStatus.SHUTTING_DOWN,
}


def _map_status(s: SchedulerSessionStatus) -> SessionStatus:
    return _SCHEDULER_TO_SCHEMA.get(s, SessionStatus.READY)


def _now() -> float:
    return round(time.time(), 6)


# ──────────────────────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/api/control",
    tags=["control"],
)


# ──────────────────────────────────────────────────────────────────────────────
# 依赖注入
# ──────────────────────────────────────────────────────────────────────────────


def _dep_scheduler() -> DebateScheduler:
    return get_scheduler()


def _dep_context_bus() -> ContextBus:
    return get_context_bus()


def _dep_arbitrator() -> Arbitrator:
    return get_arbitrator()


def _dep_throttle_hub() -> ThrottleHub:
    return get_throttle_hub()


# ──────────────────────────────────────────────────────────────────────────────
# /api/control/topic/inject
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/topic/inject",
    response_model=InjectTopicResponse,
    summary="注入新议题层",
    description=(
        "推入新议题到议题栈顶。若 auto_start_debate=true 且当前 Scheduler "
        "处于 READY / PAUSED 状态,则自动切到 RUNNING 启动辩论。"
    ),
    status_code=status.HTTP_200_OK,
)
async def inject_topic(
    request: InjectTopicRequest,
    bus: ContextBus = Depends(_dep_context_bus),
    scheduler: DebateScheduler = Depends(_dep_scheduler),
) -> InjectTopicResponse:
    """注入议题主入口。"""
    logger.info(
        f"[control] INJECT_TOPIC · title={request.title!r} · "
        f"auto_start={request.auto_start_debate}"
    )

    # 状态守门: SHUTTING_DOWN 拒绝
    sched_status = scheduler.status
    if sched_status == SchedulerSessionStatus.SHUTTING_DOWN:
        return InjectTopicResponse(
            accepted=False,
            topic=None,
            message=(
                "系统正在关闭,拒绝注入议题"
            ),
        )

    # 推入议题
    try:
        topic_internal = await bus.push_topic(
            title=request.title,
            description=request.description,
        )
    except ValueError as exc:
        logger.warning(f"[control] 议题校验失败: {exc!r}")
        return InjectTopicResponse(
            accepted=False,
            topic=None,
            message=f"议题校验失败: {exc}",
        )
    except Exception as exc:
        logger.error(f"[control] push_topic 异常: {exc!r}")
        return InjectTopicResponse(
            accepted=False,
            topic=None,
            message=f"push_topic 异常: {type(exc).__name__}",
        )

    # 自动启动辩论
    auto_started = False
    if request.auto_start_debate:
        if sched_status in (
            SchedulerSessionStatus.READY,
            SchedulerSessionStatus.PAUSED,
        ):
            try:
                scheduler.set_status(SchedulerSessionStatus.RUNNING)
                auto_started = True
                logger.info(
                    f"[control] 议题注入后自动启动辩论 · "
                    f"prev={sched_status.value}"
                )
            except Exception as exc:
                logger.warning(
                    f"[control] 议题注入后自动启动失败: {exc!r}"
                )
        else:
            logger.debug(
                f"[control] 议题注入但未自动启动 · "
                f"scheduler={sched_status.value}"
            )

    msg_parts: list[str] = [f"议题已注入: {topic_internal.title!r}"]
    if auto_started:
        msg_parts.append("辩论已自动启动")

    return InjectTopicResponse(
        accepted=True,
        topic=TopicModel.from_internal(topic_internal),
        message=" · ".join(msg_parts),
    )


# ──────────────────────────────────────────────────────────────────────────────
# /api/control/topic/pop
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/topic/pop",
    response_model=PopTopicResponse,
    summary="弹出栈顶议题",
    description="弹出当前议题层。若议题栈为空,返回 accepted=false。",
    status_code=status.HTTP_200_OK,
)
async def pop_topic(
    bus: ContextBus = Depends(_dep_context_bus),
    scheduler: DebateScheduler = Depends(_dep_scheduler),
) -> PopTopicResponse:
    logger.info("[control] POP_TOPIC")

    if scheduler.status == SchedulerSessionStatus.SHUTTING_DOWN:
        return PopTopicResponse(
            accepted=False,
            popped_topic=None,
            remaining_depth=bus.topic_stack_depth(),
            message="系统正在关闭,拒绝议题操作",
        )

    try:
        popped = await bus.pop_topic()
    except EmptyTopicStackError:
        logger.debug("[control] POP_TOPIC: 议题栈为空")
        return PopTopicResponse(
            accepted=False,
            popped_topic=None,
            remaining_depth=0,
            message="议题栈为空,无可弹出层",
        )
    except Exception as exc:
        logger.error(f"[control] POP_TOPIC 异常: {exc!r}")
        return PopTopicResponse(
            accepted=False,
            popped_topic=None,
            remaining_depth=bus.topic_stack_depth(),
            message=f"pop_topic 异常: {type(exc).__name__}",
        )

    return PopTopicResponse(
        accepted=True,
        popped_topic=TopicModel.from_internal(popped),
        remaining_depth=bus.topic_stack_depth(),
        message=f"议题已弹出: {popped.title!r}",
    )


# ──────────────────────────────────────────────────────────────────────────────
# /api/control/action
# ──────────────────────────────────────────────────────────────────────────────


@router.post(
    "/action",
    response_model=ControlActionResponse,
    summary="通用控制动作",
    description=(
        "支持 PAUSE / RESUME / FORCE_INTERRUPT / EMERGENCY_STOP 四种动作。"
        " EMERGENCY_STOP 仅切换 SHUTTING_DOWN 状态,实际进程关闭由 lifespan / "
        "运维信号驱动,不会触发 sys.exit。"
    ),
    status_code=status.HTTP_200_OK,
)
async def control_action(
    request: ControlActionRequest,
    scheduler: DebateScheduler = Depends(_dep_scheduler),
    arbitrator: Arbitrator = Depends(_dep_arbitrator),
    hub: ThrottleHub = Depends(_dep_throttle_hub),
) -> ControlActionResponse:
    """通用控制入口。"""
    action = request.action
    reason = request.reason or "(no_reason)"

    logger.info(
        f"[control] ACTION={action.value} · reason={reason!r}"
    )

    if action == ControlActionKind.PAUSE:
        return await _handle_pause(scheduler=scheduler, reason=reason)

    if action == ControlActionKind.RESUME:
        return await _handle_resume(scheduler=scheduler, reason=reason)

    if action == ControlActionKind.FORCE_INTERRUPT:
        return await _handle_force_interrupt(
            arbitrator=arbitrator,
            hub=hub,
            scheduler=scheduler,
            reason=reason,
        )

    if action == ControlActionKind.EMERGENCY_STOP:
        return await _handle_emergency_stop(
            scheduler=scheduler,
            reason=reason,
        )

    # 不应到达 (Pydantic Literal 已校验)
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"unknown action: {action!r}",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Action Handlers
# ──────────────────────────────────────────────────────────────────────────────


async def _handle_pause(
    *,
    scheduler: DebateScheduler,
    reason: str,
) -> ControlActionResponse:
    """
    PAUSE: 仅在 RUNNING / READY 状态下生效。
    幂等: 已 PAUSED 返回 accepted=true 无副作用。
    冲突: SHUTTING_DOWN / INITIALIZING → 409。
    """
    cur = scheduler.status

    if cur == SchedulerSessionStatus.PAUSED:
        return ControlActionResponse(
            accepted=True,
            action=ControlActionKind.PAUSE,
            new_status=_map_status(cur),
            message="已处于 PAUSED 状态 (幂等成功)",
            server_at_wall_unix=_now(),
        )

    if cur not in (
        SchedulerSessionStatus.RUNNING,
        SchedulerSessionStatus.READY,
    ):
        # 状态冲突: 抛 409
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"PAUSE 仅在 RUNNING/READY/PAUSED 时合法,"
                f"当前: {cur.value}"
            ),
        )

    try:
        scheduler.set_status(SchedulerSessionStatus.PAUSED)
    except Exception as exc:
        logger.error(f"[control] PAUSE set_status 异常: {exc!r}")
        return ControlActionResponse(
            accepted=False,
            action=ControlActionKind.PAUSE,
            new_status=_map_status(scheduler.status),
            message=f"PAUSE 失败: {type(exc).__name__}",
            server_at_wall_unix=_now(),
        )

    return ControlActionResponse(
        accepted=True,
        action=ControlActionKind.PAUSE,
        new_status=SessionStatus.PAUSED,
        message=f"已暂停 (reason={reason!r})",
        server_at_wall_unix=_now(),
    )


async def _handle_resume(
    *,
    scheduler: DebateScheduler,
    reason: str,
) -> ControlActionResponse:
    """
    RESUME: 仅在 PAUSED 状态下从暂停恢复; READY 也允许直接进 RUNNING。
    幂等: 已 RUNNING 返回 accepted=true 无副作用。
    冲突: SHUTTING_DOWN / INITIALIZING → 409。
    """
    cur = scheduler.status

    if cur == SchedulerSessionStatus.RUNNING:
        return ControlActionResponse(
            accepted=True,
            action=ControlActionKind.RESUME,
            new_status=_map_status(cur),
            message="已处于 RUNNING 状态 (幂等成功)",
            server_at_wall_unix=_now(),
        )

    if cur not in (
        SchedulerSessionStatus.PAUSED,
        SchedulerSessionStatus.READY,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"RESUME 仅在 PAUSED/READY/RUNNING 时合法,"
                f"当前: {cur.value}"
            ),
        )

    try:
        scheduler.set_status(SchedulerSessionStatus.RUNNING)
    except Exception as exc:
        logger.error(f"[control] RESUME set_status 异常: {exc!r}")
        return ControlActionResponse(
            accepted=False,
            action=ControlActionKind.RESUME,
            new_status=_map_status(scheduler.status),
            message=f"RESUME 失败: {type(exc).__name__}",
            server_at_wall_unix=_now(),
        )

    return ControlActionResponse(
        accepted=True,
        action=ControlActionKind.RESUME,
        new_status=SessionStatus.RUNNING,
        message=f"已恢复辩论 (reason={reason!r})",
        server_at_wall_unix=_now(),
    )


async def _handle_force_interrupt(
    *,
    arbitrator: Arbitrator,
    hub: ThrottleHub,
    scheduler: DebateScheduler,
    reason: str,
) -> ControlActionResponse:
    """
    FORCE_INTERRUPT (运维/调试):
        - 若有持麦者 → set interrupt_event 触发 LLMRouter 优雅断流
        - 同时通过 ThrottleHub 广播 INTERRUPT 给前端 (Shake 动效)
        - 不主动 force_release_if_held_by · 让 router 走 aclose 路径
    """
    holder = arbitrator.current_holder()
    if holder is None:
        return ControlActionResponse(
            accepted=True,  # 视为幂等成功
            action=ControlActionKind.FORCE_INTERRUPT,
            new_status=_map_status(scheduler.status),
            message="当前无持麦者,无需中断",
            server_at_wall_unix=_now(),
        )

    interrupted_id = holder.holder_agent_id

    # 1) set event
    try:
        holder.interrupt_event.set()
    except Exception as exc:
        logger.warning(
            f"[control] FORCE_INTERRUPT set_event 异常 (继续): {exc!r}"
        )

    # 2) 广播 INTERRUPT
    try:
        await hub.broadcast_interrupt(
            interrupter_agent_id="__operator__",
            interrupted_agent_id=interrupted_id,
        )
    except Exception as exc:
        logger.warning(
            f"[control] FORCE_INTERRUPT broadcast 异常 (继续): {exc!r}"
        )

    return ControlActionResponse(
        accepted=True,
        action=ControlActionKind.FORCE_INTERRUPT,
        new_status=_map_status(scheduler.status),
        message=(
            f"已触发强制中断 · interrupted={interrupted_id!r} · "
            f"reason={reason!r}"
        ),
        server_at_wall_unix=_now(),
    )


async def _handle_emergency_stop(
    *,
    scheduler: DebateScheduler,
    reason: str,
) -> ControlActionResponse:
    """
    EMERGENCY_STOP:
        - 仅切换 Scheduler 状态到 SHUTTING_DOWN
        - 不调用 sys.exit / os.kill / signal —— 真正的关闭由 lifespan 与
          uvicorn 信号驱动 (例: SIGTERM)
        - 状态切换后,Scheduler 会在下一 tick 自动跳过所有发言派发
    """
    cur = scheduler.status
    if cur == SchedulerSessionStatus.SHUTTING_DOWN:
        return ControlActionResponse(
            accepted=True,
            action=ControlActionKind.EMERGENCY_STOP,
            new_status=SessionStatus.SHUTTING_DOWN,
            message="已处于 SHUTTING_DOWN 状态 (幂等成功)",
            server_at_wall_unix=_now(),
        )

    try:
        scheduler.set_status(SchedulerSessionStatus.SHUTTING_DOWN)
    except Exception as exc:
        logger.error(f"[control] EMERGENCY_STOP 异常: {exc!r}")
        return ControlActionResponse(
            accepted=False,
            action=ControlActionKind.EMERGENCY_STOP,
            new_status=_map_status(scheduler.status),
            message=f"EMERGENCY_STOP 失败: {type(exc).__name__}",
            server_at_wall_unix=_now(),
        )

    logger.warning(
        f"[control] EMERGENCY_STOP 已切到 SHUTTING_DOWN · "
        f"reason={reason!r} · 实际进程关闭等 lifespan 接管"
    )

    return ControlActionResponse(
        accepted=True,
        action=ControlActionKind.EMERGENCY_STOP,
        new_status=SessionStatus.SHUTTING_DOWN,
        message=(
            f"已切到 SHUTTING_DOWN · reason={reason!r} · "
            f"等待 lifespan 完成清理"
        ),
        server_at_wall_unix=_now(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = ["router"]