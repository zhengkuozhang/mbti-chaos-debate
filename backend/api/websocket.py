"""
==============================================================================
backend/api/websocket.py
------------------------------------------------------------------------------
WebSocket 双向通信路由 · /ws/debate

定位:
    HUD 大屏的实时通信通道。
    出站: 由 ThrottleHub 统一推送 STATE / TEXT / INTERRUPT / TURN_SEALED /
          TOPIC / BYE / HEARTBEAT / ERROR 8 类包
    入站: 控制指令 PING / PAUSE / RESUME / INJECT_TOPIC / POP_TOPIC /
          FORCE_INTERRUPT 6 类包

设计原则 (核心: 双向防护):
    1. 出站走 ThrottleHub 单一入口 (避免散点广播)
    2. 入站必须经 schemas/stream_packet.parse_inbound_packet 校验
    3. 畸形包返回 ERROR 而非断连 (给客户端修正机会)
    4. 单连接频率限速 (20Hz 上限,超出协议违规断连)
    5. 任何退出路径必走 finally 清理 ThrottleHub 订阅 (杜绝幽灵广播)
    6. send_fn 注入 ThrottleHub,广播由 ThrottleHub 的 send_loop 独立串行化

⚠️ 工程铁律:
    - 严禁 finally 之外清理订阅 (任何异常路径都走 finally)
    - 严禁畸形包立刻断连 (先发 ERROR,严重再断)
    - 严禁单异常路径吞掉所有错误 (按 Disconnect / Timeout / Schema / Other 分类)
    - 严禁阻塞主接收循环 (发送由 ThrottleHub 独立协程负责)
    - 严禁忘记限速 (令牌桶兜底)

==============================================================================
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Final, Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from loguru import logger

from backend.agents.registry import (
    DebateScheduler,
    SchedulerSessionStatus,
    get_scheduler,
)
from backend.core.arbitrator import Arbitrator, get_arbitrator
from backend.core.config import Settings, get_settings
from backend.core.context_bus import (
    ContextBus,
    EmptyTopicStackError,
    get_context_bus,
)
from backend.core.throttle import (
    PacketKind,
    ThrottleHub,
    ThrottleStoppedError,
    get_throttle_hub,
)
from backend.schemas.stream_packet import (
    ByeReason,
    ErrorSeverity,
    InboundForceInterruptPacket,
    InboundInjectTopicPacket,
    InboundKind,
    InboundPausePacket,
    InboundPingPacket,
    InboundPopTopicPacket,
    InboundResumePacket,
    PacketDecodeError,
    PacketParseError,
    PacketSchemaError,
    make_bye_packet,
    make_error_packet,
    make_heartbeat_packet,
    parse_inbound_packet,
    serialize_outbound_packet,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 单连接入站令牌桶上限 (包/秒)
_INBOUND_RATE_LIMIT_PER_SEC: Final[float] = 20.0

#: 令牌桶容量 (允许短突发,但平均速率受 _INBOUND_RATE_LIMIT_PER_SEC 限制)
_INBOUND_RATE_BUCKET_CAPACITY: Final[float] = 30.0

#: 单连接 receive 超时 (秒) · 防止挂死连接占用资源
#: 注意 ThrottleHub 心跳是 25s/次,此处 60s 与之冲突最小
_RECEIVE_IDLE_TIMEOUT_SEC: Final[float] = 60.0

#: 协议违规断连前的 ERROR 阈值 · 累计 ≥ 此值连续违规 → 断
_PROTOCOL_VIOLATION_TOLERANCE: Final[int] = 5


# ──────────────────────────────────────────────────────────────────────────────
# Router
# ──────────────────────────────────────────────────────────────────────────────


router = APIRouter(tags=["websocket"])


# ──────────────────────────────────────────────────────────────────────────────
# 依赖注入
# ──────────────────────────────────────────────────────────────────────────────


def _dep_throttle_hub() -> ThrottleHub:
    return get_throttle_hub()


def _dep_scheduler() -> DebateScheduler:
    return get_scheduler()


def _dep_context_bus() -> ContextBus:
    return get_context_bus()


def _dep_arbitrator() -> Arbitrator:
    return get_arbitrator()


def _dep_settings() -> Settings:
    return get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# 入站令牌桶 (轻量,内联实现)
# ──────────────────────────────────────────────────────────────────────────────


class _InboundTokenBucket:
    """
    简单令牌桶 · 单连接生命周期内独立持有。

    每秒补充 _INBOUND_RATE_LIMIT_PER_SEC 个令牌,容量上限
    _INBOUND_RATE_BUCKET_CAPACITY。每次入站包消耗 1 个令牌。
    令牌不足 → 视为超频。
    """

    __slots__ = ("_tokens", "_last_refill_at", "_rate", "_capacity")

    def __init__(self) -> None:
        self._tokens: float = _INBOUND_RATE_BUCKET_CAPACITY
        self._last_refill_at: float = time.monotonic()
        self._rate: float = _INBOUND_RATE_LIMIT_PER_SEC
        self._capacity: float = _INBOUND_RATE_BUCKET_CAPACITY

    def consume(self) -> bool:
        """尝试消耗 1 个令牌 · 返回 True/False。"""
        now = time.monotonic()
        elapsed = now - self._last_refill_at
        if elapsed > 0:
            self._tokens = min(
                self._capacity,
                self._tokens + elapsed * self._rate,
            )
            self._last_refill_at = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket 端点
# ──────────────────────────────────────────────────────────────────────────────


@router.websocket("/ws/debate")
async def debate_websocket(
    websocket: WebSocket,
    hub: ThrottleHub = Depends(_dep_throttle_hub),
    scheduler: DebateScheduler = Depends(_dep_scheduler),
    bus: ContextBus = Depends(_dep_context_bus),
    arbitrator: Arbitrator = Depends(_dep_arbitrator),
    settings: Settings = Depends(_dep_settings),
) -> None:
    """
    WebSocket 主端点 · 全双工。

    生命周期:
        1. accept
        2. 注入 send_fn → ThrottleHub.add_subscriber → 后台广播立即开始
        3. 主循环 receive_text/bytes → parse → dispatch
        4. 任何路径退出 → finally 清理订阅 + 主动 close (尽力而为)
    """
    # ── 1. 接受连接 ──
    await websocket.accept()

    # 客户端识别 (前端对应日志里能看到这个 ID)
    client_host = (
        websocket.client.host if websocket.client else "unknown"
    )
    client_port = (
        websocket.client.port if websocket.client else 0
    )
    subscriber_id = (
        f"{client_host}:{client_port}__{uuid.uuid4().hex[:8]}"
    )

    logger.info(
        f"[ws] CONNECT · sub_id={subscriber_id!r} · "
        f"hub_running={hub.is_running}"
    )

    # ── 2. 注入 send_fn 到 ThrottleHub ──
    # 闭包封装 send_bytes,异常向上抛由 ThrottleHub 的 send_loop 标记 unhealthy
    async def _send_fn(payload: bytes) -> None:
        # WebSocket.send_bytes 在断连后会抛 RuntimeError / ConnectionClosed
        # 这里直接抛给 ThrottleHub 让它感知
        await websocket.send_bytes(payload)

    try:
        await hub.add_subscriber(
            send_fn=_send_fn,
            subscriber_id=subscriber_id,
        )
    except ThrottleStoppedError:
        # ThrottleHub 已关闭,直接拒绝连接
        logger.warning(
            f"[ws] ThrottleHub 已停止,拒绝新连接 sub_id={subscriber_id!r}"
        )
        await _try_close(websocket, code=1011, reason="hub_stopped")
        return
    except Exception as exc:
        logger.error(
            f"[ws] add_subscriber 异常 · sub_id={subscriber_id!r} · {exc!r}"
        )
        await _try_close(websocket, code=1011, reason="subscribe_failed")
        return

    # ── 3. 主接收循环 ──
    bucket = _InboundTokenBucket()
    consecutive_violations = 0
    max_msg_bytes = max(1024, settings.WS_MAX_MESSAGE_BYTES)

    try:
        while True:
            # 3a) 限时接收 (idle 超时 → 断连)
            try:
                msg_bytes = await asyncio.wait_for(
                    _receive_one_frame(websocket, max_msg_bytes),
                    timeout=_RECEIVE_IDLE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.info(
                    f"[ws] 入站空闲超时 ({_RECEIVE_IDLE_TIMEOUT_SEC}s) · "
                    f"sub_id={subscriber_id!r}"
                )
                await _send_bye_safely(
                    websocket,
                    reason=ByeReason.SERVER_SHUTDOWN,
                    detail="idle_timeout",
                )
                break
            except WebSocketDisconnect as exc:
                logger.info(
                    f"[ws] DISCONNECT (recv) · sub_id={subscriber_id!r} · "
                    f"code={exc.code}"
                )
                break
            except _OversizedFrameError as exc:
                logger.warning(
                    f"[ws] 入站超大帧 · sub_id={subscriber_id!r} · "
                    f"size={exc.size}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.ERROR,
                    code="PAYLOAD_TOO_LARGE",
                    message=f"frame too large ({exc.size} bytes)",
                )
                consecutive_violations += 1
                if consecutive_violations >= _PROTOCOL_VIOLATION_TOLERANCE:
                    break
                continue

            if msg_bytes is None:
                # 心跳帧或客户端发送了空 close,继续
                continue

            # 3b) 限速
            if not bucket.consume():
                consecutive_violations += 1
                logger.warning(
                    f"[ws] 入站限速触发 · sub_id={subscriber_id!r} · "
                    f"violations={consecutive_violations}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.WARNING,
                    code="RATE_LIMITED",
                    message=(
                        f"inbound rate limit "
                        f"{_INBOUND_RATE_LIMIT_PER_SEC}/sec exceeded"
                    ),
                )
                if consecutive_violations >= _PROTOCOL_VIOLATION_TOLERANCE:
                    await _send_bye_safely(
                        websocket,
                        reason=ByeReason.PROTOCOL_VIOLATION,
                        detail="rate_limit_repeated",
                    )
                    break
                continue

            # 3c) 解析
            try:
                inbound = parse_inbound_packet(msg_bytes)
            except PacketDecodeError as exc:
                consecutive_violations += 1
                logger.debug(
                    f"[ws] 解码失败 · sub_id={subscriber_id!r} · "
                    f"code={exc.code} · {exc}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.WARNING,
                    code=exc.code,
                    message=str(exc)[:256],
                )
                if consecutive_violations >= _PROTOCOL_VIOLATION_TOLERANCE:
                    break
                continue
            except PacketSchemaError as exc:
                consecutive_violations += 1
                logger.debug(
                    f"[ws] schema 校验失败 · sub_id={subscriber_id!r} · "
                    f"code={exc.code} · {exc}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.WARNING,
                    code=exc.code,
                    message=str(exc)[:256],
                )
                if consecutive_violations >= _PROTOCOL_VIOLATION_TOLERANCE:
                    break
                continue
            except PacketParseError as exc:
                # 兜底基类 (理论上不会到这里,但保险)
                consecutive_violations += 1
                logger.warning(
                    f"[ws] 解析未知错误 · sub_id={subscriber_id!r} · {exc!r}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.WARNING,
                    code="PARSE_FAILED",
                    message=str(exc)[:256],
                )
                if consecutive_violations >= _PROTOCOL_VIOLATION_TOLERANCE:
                    break
                continue

            # 解析成功 → 重置违规计数
            consecutive_violations = 0

            # 3d) 路由分发
            try:
                await _dispatch_inbound(
                    websocket=websocket,
                    inbound=inbound,
                    subscriber_id=subscriber_id,
                    hub=hub,
                    scheduler=scheduler,
                    bus=bus,
                    arbitrator=arbitrator,
                )
            except Exception as exc:
                # 路由处理异常: 仅记录,发回 ERROR,不断连
                logger.error(
                    f"[ws] dispatch 异常 · sub_id={subscriber_id!r} · "
                    f"kind={inbound.kind.value} · {exc!r}"
                )
                await _send_error_safely(
                    websocket,
                    severity=ErrorSeverity.ERROR,
                    code="DISPATCH_FAILED",
                    message=f"{type(exc).__name__}: {str(exc)[:200]}",
                )

    except WebSocketDisconnect as exc:
        logger.info(
            f"[ws] DISCONNECT (loop) · sub_id={subscriber_id!r} · "
            f"code={exc.code}"
        )
    except asyncio.CancelledError:
        logger.info(
            f"[ws] 主循环被取消 · sub_id={subscriber_id!r} (lifespan shutdown?)"
        )
        raise
    except Exception as exc:
        logger.error(
            f"[ws] 主循环未知异常 · sub_id={subscriber_id!r} · {exc!r}"
        )
    finally:
        # ── 4. 清理订阅 (无论何种退出路径) ──
        try:
            await hub.remove_subscriber(subscriber_id)
        except Exception as exc:
            logger.debug(
                f"[ws] remove_subscriber 异常 (忽略) · "
                f"sub_id={subscriber_id!r} · {exc!r}"
            )

        # 主动关闭 (若还活着)
        await _try_close(websocket, code=1000, reason="bye")
        logger.info(
            f"[ws] CLEANUP DONE · sub_id={subscriber_id!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 入站接收: 统一返回 bytes,过滤 idle/text/oversized
# ──────────────────────────────────────────────────────────────────────────────


class _OversizedFrameError(Exception):
    def __init__(self, size: int) -> None:
        super().__init__(f"frame size {size} exceeds limit")
        self.size = size


async def _receive_one_frame(
    websocket: WebSocket,
    max_bytes: int,
) -> Optional[bytes]:
    """
    接收一帧 · 返回 bytes (UTF-8 编码后 / 二进制原样)。

    返回 None 表示该帧应被忽略 (ping/keepalive 等)。
    超大帧抛 _OversizedFrameError。
    断连抛 WebSocketDisconnect。
    """
    # FastAPI 的 receive 协议: text + bytes 二选一
    # websocket.receive() 返回 dict: {"type": "websocket.receive", "text": str | None, "bytes": bytes | None}
    msg = await websocket.receive()

    msg_type = msg.get("type")
    if msg_type == "websocket.disconnect":
        # 这种情况下 fastapi 应该已经抛 WebSocketDisconnect,但保险起见
        raise WebSocketDisconnect(
            code=int(msg.get("code", 1000)),
        )
    if msg_type != "websocket.receive":
        # 未知帧类型 → 静默忽略
        return None

    if "bytes" in msg and msg["bytes"] is not None:
        data: bytes = msg["bytes"]
    elif "text" in msg and msg["text"] is not None:
        text: str = msg["text"]
        data = text.encode("utf-8", errors="replace")
    else:
        return None

    if len(data) > max_bytes:
        raise _OversizedFrameError(size=len(data))

    return data


# ──────────────────────────────────────────────────────────────────────────────
# 入站包路由分发
# ──────────────────────────────────────────────────────────────────────────────


async def _dispatch_inbound(
    *,
    websocket: WebSocket,
    inbound,  # InboundPacket (Union, 不写联合类型避免 import 噪声)
    subscriber_id: str,
    hub: ThrottleHub,
    scheduler: DebateScheduler,
    bus: ContextBus,
    arbitrator: Arbitrator,
) -> None:
    """根据 inbound.kind 分发到对应 handler。"""
    kind = inbound.kind

    if kind == InboundKind.PING:
        await _handle_ping(
            websocket=websocket,
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
        )
    elif kind == InboundKind.PAUSE_SESSION:
        await _handle_pause(
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
            scheduler=scheduler,
            hub=hub,
        )
    elif kind == InboundKind.RESUME_SESSION:
        await _handle_resume(
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
            scheduler=scheduler,
            hub=hub,
        )
    elif kind == InboundKind.INJECT_TOPIC:
        await _handle_inject_topic(
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
            bus=bus,
        )
    elif kind == InboundKind.POP_TOPIC:
        await _handle_pop_topic(
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
            bus=bus,
        )
    elif kind == InboundKind.FORCE_INTERRUPT:
        await _handle_force_interrupt(
            packet=inbound,  # type: ignore[arg-type]
            subscriber_id=subscriber_id,
            arbitrator=arbitrator,
            hub=hub,
        )
    else:
        # 不应到达 (Discriminated Union 已覆盖全部 InboundKind)
        logger.warning(
            f"[ws] 未知入站 kind · sub_id={subscriber_id!r} · "
            f"kind={kind!r}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 单条 Handler
# ──────────────────────────────────────────────────────────────────────────────


async def _handle_ping(
    *,
    websocket: WebSocket,
    packet: InboundPingPacket,
    subscriber_id: str,
) -> None:
    """
    PING → 立即回 HEARTBEAT (直接通过当前连接发送,不走 ThrottleHub)。

    走直发是因为 PING 是端到端 RTT 探测,经过 ThrottleHub 队列会失真。
    """
    logger.debug(f"[ws] PING from sub_id={subscriber_id!r}")
    hb = make_heartbeat_packet()
    try:
        await websocket.send_bytes(serialize_outbound_packet(hb))
    except Exception as exc:
        logger.debug(
            f"[ws] PING 回包失败 (忽略,主循环会感知断连): {exc!r}"
        )


async def _handle_pause(
    *,
    packet: InboundPausePacket,
    subscriber_id: str,
    scheduler: DebateScheduler,
    hub: ThrottleHub,
) -> None:
    logger.info(f"[ws] PAUSE_SESSION from sub_id={subscriber_id!r}")
    try:
        scheduler.set_status(SchedulerSessionStatus.PAUSED)
    except Exception as exc:
        logger.warning(f"[ws] PAUSE_SESSION 失败: {exc!r}")
        return
    # 不显式回 ACK · 状态变化会通过下一次 STATE 包透出


async def _handle_resume(
    *,
    packet: InboundResumePacket,
    subscriber_id: str,
    scheduler: DebateScheduler,
    hub: ThrottleHub,
) -> None:
    logger.info(f"[ws] RESUME_SESSION from sub_id={subscriber_id!r}")
    try:
        # 仅在当前 PAUSED 时切回 RUNNING (避免 INITIALIZING 被误踢)
        if scheduler.status == SchedulerSessionStatus.PAUSED:
            scheduler.set_status(SchedulerSessionStatus.RUNNING)
        else:
            logger.debug(
                f"[ws] RESUME 但 scheduler 状态非 PAUSED "
                f"(当前: {scheduler.status.value}),忽略"
            )
    except Exception as exc:
        logger.warning(f"[ws] RESUME_SESSION 失败: {exc!r}")


async def _handle_inject_topic(
    *,
    packet: InboundInjectTopicPacket,
    subscriber_id: str,
    bus: ContextBus,
) -> None:
    title = packet.payload.title
    description = packet.payload.description

    logger.info(
        f"[ws] INJECT_TOPIC from sub_id={subscriber_id!r} · "
        f"title={title!r}"
    )
    try:
        await bus.push_topic(title=title, description=description)
        # 注: ContextBus.push_topic 会通过 topic_subscribers 自动广播
        # 该广播应由 main.py 的 lifespan 在启动时桥接到 ThrottleHub.broadcast_topic
    except ValueError as exc:
        logger.warning(f"[ws] INJECT_TOPIC 校验失败: {exc!r}")
    except Exception as exc:
        logger.error(f"[ws] INJECT_TOPIC 异常: {exc!r}")


async def _handle_pop_topic(
    *,
    packet: InboundPopTopicPacket,
    subscriber_id: str,
    bus: ContextBus,
) -> None:
    logger.info(f"[ws] POP_TOPIC from sub_id={subscriber_id!r}")
    try:
        await bus.pop_topic()
    except EmptyTopicStackError:
        logger.debug("[ws] POP_TOPIC 失败: 议题栈为空")
    except Exception as exc:
        logger.error(f"[ws] POP_TOPIC 异常: {exc!r}")


async def _handle_force_interrupt(
    *,
    packet: InboundForceInterruptPacket,
    subscriber_id: str,
    arbitrator: Arbitrator,
    hub: ThrottleHub,
) -> None:
    """
    FORCE_INTERRUPT (运维/调试):
        - 若当前有持麦者,强制释放令牌
        - 通过 interrupt_event 通知 LLMRouter 优雅断流 (router 已监听)
        - 广播 INTERRUPT 信号到所有订阅者 (前端 Shake)
    """
    reason = packet.payload.reason
    logger.warning(
        f"[ws] FORCE_INTERRUPT from sub_id={subscriber_id!r} · "
        f"reason={reason!r}"
    )
    try:
        holder = arbitrator.current_holder()
        if holder is None:
            logger.info("[ws] FORCE_INTERRUPT 时无持麦者,忽略")
            return
        # 1) set interrupt_event → LLMRouter 自然走 INTERRUPTED 路径
        try:
            holder.interrupt_event.set()
        except Exception as exc:
            logger.debug(
                f"[ws] FORCE_INTERRUPT set event 异常 (忽略): {exc!r}"
            )
        # 2) 广播 INTERRUPT 给前端
        try:
            await hub.broadcast_interrupt(
                interrupter_agent_id="__operator__",
                interrupted_agent_id=holder.holder_agent_id,
            )
        except Exception as exc:
            logger.debug(f"[ws] broadcast_interrupt 异常 (忽略): {exc!r}")
        # 3) 兜底: 若 router 没在合理时间内释放,强制 force_release
        # (这里不主动 force_release_if_held_by · 让 router 走 aclose 路径更优雅)
        # 仅当 _PROTOCOL_VIOLATION_TOLERANCE 内仍未释放时,运维可再发一次 FORCE_INTERRUPT
    except Exception as exc:
        logger.error(f"[ws] FORCE_INTERRUPT 异常: {exc!r}")


# ──────────────────────────────────────────────────────────────────────────────
# 安全发送辅助 (失败仅记录,不抛)
# ──────────────────────────────────────────────────────────────────────────────


async def _send_error_safely(
    websocket: WebSocket,
    *,
    severity: ErrorSeverity,
    code: str,
    message: str,
) -> None:
    """直接通过当前 WebSocket 发送 ERROR 包 (绕过 ThrottleHub)。"""
    try:
        pkt = make_error_packet(
            severity=severity,
            code=code,
            message=message[:512],
        )
        await websocket.send_bytes(serialize_outbound_packet(pkt))
    except Exception as exc:
        logger.debug(
            f"[ws] 发送 ERROR 失败 (忽略,主循环会感知断连): {exc!r}"
        )


async def _send_bye_safely(
    websocket: WebSocket,
    *,
    reason: ByeReason,
    detail: str = "",
) -> None:
    """直接通过当前 WebSocket 发送 BYE 包。"""
    try:
        pkt = make_bye_packet(reason=reason, detail=detail[:256])
        await websocket.send_bytes(serialize_outbound_packet(pkt))
    except Exception as exc:
        logger.debug(f"[ws] 发送 BYE 失败 (忽略): {exc!r}")


async def _try_close(
    websocket: WebSocket,
    *,
    code: int = 1000,
    reason: str = "bye",
) -> None:
    """容错关闭 · 失败仅记录。"""
    try:
        await websocket.close(code=code, reason=reason[:120])
    except Exception as exc:
        logger.debug(f"[ws] close 异常 (忽略): {exc!r}")


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = ["router"]