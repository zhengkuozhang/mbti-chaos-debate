"""
==============================================================================
backend/core/throttle.py
------------------------------------------------------------------------------
WebSocket 状态包节流阀 · 10Hz 合并推送 · 多订阅者扇出

工程死局背景:
    1. Aggro 演化每 100ms 都在变化,FSM 转换、仲裁器授权同样高频
    2. 若每个变化都直接广播 → 前端被状态包风暴打爆,网络也会饱和
    3. 但文本流必须实时推送,否则打字机视觉抖动
    → 必须分轨: 状态包合并节流, 文本流旁路实时

设计原则:
    1. 周期性采集 (10Hz · 100ms):
       从 FSM / Aggro / Arbitrator / Watchdog 拉取快照合成 StateBundle
    2. 变化检测 (Hash 比对):
       与上次推送 hash 一致则跳过,静态会话零带宽消耗
    3. 多订阅者扇出:
       同一 StateBundle 序列化一次,并发分发给所有订阅者
    4. 背压控制 (Last-Write-Wins):
       每订阅者独立容量 1 槽位,新状态覆盖旧状态,
       前端慢就只看最新,杜绝堆积导致的 OOM
    5. 文本流旁路:
       send_text_to_subscriber() 不进合并通道, 实时推送
    6. 优雅关闭:
       stop() 时主动 flush 终态 + BYE 包

⚠️ 工程铁律:
    - 严禁在 FSM hook / Aggro 回调里直接推送 (走 throttle 合并)
    - 单订阅者发送队列绝不无界 (容量 1 + 覆盖策略)
    - Worker 顶层异常必须自动重启 (静默死亡 = 前端假死)
    - 文本流与状态包共用 WS 但走不同 API,互不阻塞

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Optional,
)

import orjson
from loguru import logger

from backend.core.aggro_engine import AggroEngine
from backend.core.arbitrator import Arbitrator
from backend.core.config import get_settings
from backend.core.fsm import FSMRegistry
from backend.core.watchdog import GlobalWatchdog


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 历史环容量 (诊断用,记录最近 N 次推送)
_HISTORY_RING_SIZE: Final[int] = 64

#: Worker 异常重启冷却
_WORKER_RESTART_BACKOFF_SEC: Final[float] = 1.0

#: 单订阅者发送超时 (秒) · 超过即视为掉线,不阻塞其他订阅者
_PER_SUBSCRIBER_SEND_TIMEOUT_SEC: Final[float] = 3.0


# ──────────────────────────────────────────────────────────────────────────────
# 包类型枚举 (与前端 protocol.d.ts 对齐)
# ──────────────────────────────────────────────────────────────────────────────


class PacketKind(str, enum.Enum):
    """WebSocket 推送包类型。"""

    STATE = "STATE"
    """合并状态包 (10Hz · FSM + Aggro + Arbitrator + Watchdog)"""

    TEXT = "TEXT"
    """文本流 chunk (实时旁路)"""

    INTERRUPT = "INTERRUPT"
    """打断信号 (前端触发 Shake 动效)"""

    TURN_SEALED = "TURN_SEALED"
    """Turn 封存通知 (含影子后缀信息)"""

    TOPIC = "TOPIC"
    """议题栈变化"""

    BYE = "BYE"
    """系统优雅关闭"""

    HEARTBEAT = "HEARTBEAT"
    """心跳 (防中间代理切断长连接)"""


# ──────────────────────────────────────────────────────────────────────────────
# 订阅者发送通道协议
# ──────────────────────────────────────────────────────────────────────────────


SubscriberSendFn = Callable[[bytes], Awaitable[None]]
"""
订阅者发送函数签名 · 接收已序列化的 bytes,返回 awaitable。

业务层 (api/websocket.py) 包装 fastapi.WebSocket.send_bytes 注入即可。
异常应由该函数抛出,throttle 会捕获并标记订阅者掉线。
"""


# ──────────────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class StateBundle:
    """
    单次 tick 合成的全量状态包。

    序列化为 JSON 后通过 PacketKind.STATE 推送给前端。
    """

    sequence_id: int
    """单调递增序号 (供前端去重 / 回放)"""

    at_monotonic: float
    at_wall_unix: float

    fsm: dict[str, dict[str, object]]
    aggro: dict[str, dict[str, object]]
    arbitrator: dict[str, object]
    watchdog: dict[str, object]
    semaphore: dict[str, object]
    """LLMSemaphore 指标 (held / waiting / capacity)"""

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence_id": self.sequence_id,
            "at_monotonic": round(self.at_monotonic, 6),
            "at_wall_unix": round(self.at_wall_unix, 6),
            "fsm": self.fsm,
            "aggro": self.aggro,
            "arbitrator": self.arbitrator,
            "watchdog": self.watchdog,
            "semaphore": self.semaphore,
        }


@dataclass(slots=True)
class SubscriberStats:
    """单订阅者运行指标。"""

    subscriber_id: str
    connected_at_monotonic: float
    pushed_total: int = 0
    dropped_backpressure_total: int = 0
    send_failures_total: int = 0
    last_push_at_monotonic: float = 0.0
    is_healthy: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "subscriber_id": self.subscriber_id,
            "connected_for_sec": round(
                time.monotonic() - self.connected_at_monotonic, 3
            ),
            "pushed_total": self.pushed_total,
            "dropped_backpressure_total": self.dropped_backpressure_total,
            "send_failures_total": self.send_failures_total,
            "is_healthy": self.is_healthy,
        }


@dataclass(slots=True)
class ThrottleMetrics:
    """节流阀全局指标。"""

    is_running: bool
    push_hz: float
    subscribers_count: int
    healthy_subscribers: int
    last_sequence_id: int
    ticks_total: int
    pushed_total: int
    skipped_unchanged_total: int
    text_passthrough_total: int

    def to_dict(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "push_hz": self.push_hz,
            "subscribers_count": self.subscribers_count,
            "healthy_subscribers": self.healthy_subscribers,
            "last_sequence_id": self.last_sequence_id,
            "ticks_total": self.ticks_total,
            "pushed_total": self.pushed_total,
            "skipped_unchanged_total": self.skipped_unchanged_total,
            "text_passthrough_total": self.text_passthrough_total,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class ThrottleError(Exception):
    pass


class ThrottleStoppedError(ThrottleError):
    """stop 后再操作。"""


class SubscriberNotFoundError(ThrottleError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# 单订阅者通道 (内部)
# ──────────────────────────────────────────────────────────────────────────────


class _SubscriberChannel:
    """
    单订阅者的发送通道 · 容量 1 的 last-write-wins 槽位。

    设计:
        - 状态包槽位: 容量 1, 新状态覆盖旧状态 (前端只关心最新)
        - 文本/信号优先队列: 容量 64, FIFO, 满则丢弃 (但记录 dropped)
        - 单 send_loop 协程串行消费,保证单连接发送顺序

    ⚠️ 状态包用覆盖语义: 前端慢一拍只是少看几帧 Aggro,无关键信息丢失
       文本/信号用 FIFO: 必须按顺序到达,但不允许无限堆积
    """

    #: 文本/信号 FIFO 队列容量
    _NON_STATE_QUEUE_MAX: Final[int] = 64

    __slots__ = (
        "_subscriber_id",
        "_send_fn",
        "_state_slot",
        "_state_lock",
        "_priority_queue",
        "_send_task",
        "_stop_signal",
        "_closed",
        "_stats",
        "_wakeup",
    )

    def __init__(
        self,
        subscriber_id: str,
        send_fn: SubscriberSendFn,
    ) -> None:
        self._subscriber_id: Final[str] = subscriber_id
        self._send_fn: Final[SubscriberSendFn] = send_fn

        # 状态包槽位 (None = 无待发送)
        self._state_slot: Optional[bytes] = None
        self._state_lock: asyncio.Lock = asyncio.Lock()

        # 高优先 FIFO (TEXT / INTERRUPT / TURN_SEALED / TOPIC / BYE / HEARTBEAT)
        self._priority_queue: deque[bytes] = deque()

        # 唤醒事件: state 或 priority 有新内容时 set
        self._wakeup: asyncio.Event = asyncio.Event()

        self._send_task: Optional[asyncio.Task[None]] = None
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._closed: bool = False

        self._stats: SubscriberStats = SubscriberStats(
            subscriber_id=subscriber_id,
            connected_at_monotonic=time.monotonic(),
        )

    @property
    def subscriber_id(self) -> str:
        return self._subscriber_id

    @property
    def stats(self) -> SubscriberStats:
        return self._stats

    @property
    def is_healthy(self) -> bool:
        return self._stats.is_healthy and not self._closed

    def start(self) -> None:
        """启动该订阅者的 send_loop。"""
        if self._send_task is not None:
            return
        self._send_task = asyncio.create_task(
            self._send_loop(),
            name=f"throttle_send_{self._subscriber_id}",
        )

    async def stop(self) -> None:
        """停止 send_loop · 不抛异常。"""
        if self._closed:
            return
        self._closed = True
        self._stop_signal.set()
        self._wakeup.set()
        if self._send_task is not None and not self._send_task.done():
            try:
                await asyncio.wait_for(self._send_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._send_task.cancel()
                try:
                    await self._send_task
                except (asyncio.CancelledError, Exception):
                    pass

    # ────────────────────────────────────────────────────────────
    # 入队 API
    # ────────────────────────────────────────────────────────────

    def enqueue_state(self, payload: bytes) -> bool:
        """
        将状态包放入槽位 (覆盖旧值)。

        Returns:
            True - 放入成功 (无论是否覆盖)
            False - 订阅者已关闭
        """
        if self._closed:
            return False
        # 容量 1 槽位: 直接覆盖
        was_present = self._state_slot is not None
        self._state_slot = payload
        if was_present:
            # 旧状态被覆盖, 计入背压指标
            self._stats.dropped_backpressure_total += 1
        self._wakeup.set()
        return True

    def enqueue_priority(self, payload: bytes) -> bool:
        """
        将文本/信号包入 FIFO 队列。

        Returns:
            True - 入队成功
            False - 队列已满 (FIFO 丢弃) 或订阅者关闭
        """
        if self._closed:
            return False
        if len(self._priority_queue) >= self._NON_STATE_QUEUE_MAX:
            self._stats.dropped_backpressure_total += 1
            logger.warning(
                f"[throttle] subscriber {self._subscriber_id!r} "
                f"priority_queue 已满 ({self._NON_STATE_QUEUE_MAX}),丢弃旧包"
            )
            # FIFO 丢弃最旧的,保留新的 (优先信号通常是最新的更重要)
            self._priority_queue.popleft()
        self._priority_queue.append(payload)
        self._wakeup.set()
        return True

    # ────────────────────────────────────────────────────────────
    # send_loop (单连接串行)
    # ────────────────────────────────────────────────────────────

    async def _send_loop(self) -> None:
        """
        消费循环 · 优先 priority_queue (FIFO),其次 state_slot (LWW)。

        异常 → 标记 unhealthy 并退出 (不重启,由上层移除订阅者)。
        """
        try:
            while not self._stop_signal.is_set():
                # 取一个待发包: 先 priority,再 state
                payload = self._next_payload()
                if payload is None:
                    self._wakeup.clear()
                    try:
                        await asyncio.wait_for(
                            self._wakeup.wait(),
                            timeout=1.0,  # 兜底,允许检查 stop_signal
                        )
                    except asyncio.TimeoutError:
                        continue
                    continue

                # 发送
                try:
                    await asyncio.wait_for(
                        self._send_fn(payload),
                        timeout=_PER_SUBSCRIBER_SEND_TIMEOUT_SEC,
                    )
                    self._stats.pushed_total += 1
                    self._stats.last_push_at_monotonic = time.monotonic()
                except asyncio.TimeoutError:
                    self._stats.send_failures_total += 1
                    self._stats.is_healthy = False
                    logger.warning(
                        f"[throttle] subscriber {self._subscriber_id!r} "
                        f"send 超时 ({_PER_SUBSCRIBER_SEND_TIMEOUT_SEC}s) → "
                        f"标记 unhealthy 并退出 send_loop"
                    )
                    return
                except Exception as exc:
                    self._stats.send_failures_total += 1
                    self._stats.is_healthy = False
                    logger.warning(
                        f"[throttle] subscriber {self._subscriber_id!r} "
                        f"send 失败 → 标记 unhealthy: {exc!r}"
                    )
                    return

        except asyncio.CancelledError:
            return

    def _next_payload(self) -> Optional[bytes]:
        """
        取下一个待发送包: priority 优先, state 次之。
        """
        if self._priority_queue:
            return self._priority_queue.popleft()
        if self._state_slot is not None:
            payload = self._state_slot
            self._state_slot = None
            return payload
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 主体: ThrottleHub
# ──────────────────────────────────────────────────────────────────────────────


class ThrottleHub:
    """
    WebSocket 状态节流中枢 (进程级单例)。

    生命周期:
        hub = get_throttle_hub()
        await hub.start()                       # lifespan startup

        sub_id = await hub.add_subscriber(send_fn)
        ...
        await hub.send_text(sub_id, text_chunk)
        await hub.broadcast_interrupt(...)
        ...
        await hub.remove_subscriber(sub_id)

        await hub.stop()                        # lifespan shutdown
    """

    def __init__(
        self,
        *,
        fsm_registry: FSMRegistry,
        aggro_engine: AggroEngine,
        arbitrator: Arbitrator,
        watchdog: GlobalWatchdog,
        push_hz: float,
        semaphore_metrics_provider: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        if push_hz <= 0:
            raise ValueError(f"push_hz 必须 > 0,当前: {push_hz}")

        self._fsm: Final[FSMRegistry] = fsm_registry
        self._aggro: Final[AggroEngine] = aggro_engine
        self._arbitrator: Final[Arbitrator] = arbitrator
        self._watchdog: Final[GlobalWatchdog] = watchdog
        self._sem_provider = semaphore_metrics_provider

        self._push_hz: Final[float] = push_hz
        self._tick_interval_sec: Final[float] = 1.0 / push_hz

        self._subscribers: dict[str, _SubscriberChannel] = {}
        self._sub_lock: asyncio.Lock = asyncio.Lock()

        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._stopped: bool = False

        self._sequence_id: int = 0
        self._last_state_hash: bytes = b""

        # 累计指标
        self._ticks_total: int = 0
        self._pushed_total: int = 0
        self._skipped_unchanged_total: int = 0
        self._text_passthrough_total: int = 0

        # 推送历史 (诊断)
        self._push_history: deque[float] = deque(maxlen=_HISTORY_RING_SIZE)

        logger.info(
            f"[ThrottleHub] 初始化 · push_hz={push_hz} · "
            f"tick_interval={self._tick_interval_sec * 1000:.1f}ms"
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        if self._stopped:
            raise ThrottleStoppedError(
                "ThrottleHub 已 stop,不可重启 (创建新实例)"
            )
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._stop_signal.clear()
        self._worker_task = asyncio.create_task(
            self._worker_supervisor(),
            name="throttle_worker",
        )
        logger.info("[ThrottleHub] Worker 已启动")

    async def stop(self) -> None:
        """
        停止 · 主动 flush 终态 + 推送 BYE + 关闭所有订阅者。
        """
        if self._stopped:
            return
        self._stopped = True
        self._stop_signal.set()

        # 1) 停止 worker
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        # 2) 推送最终 STATE + BYE
        try:
            await self._tick_once(force=True)
        except Exception as exc:
            logger.warning(f"[ThrottleHub] stop 时最终 tick 失败: {exc!r}")

        bye_payload = self._serialize_packet(
            kind=PacketKind.BYE,
            payload={"reason": "server_shutdown"},
        )
        async with self._sub_lock:
            subs = list(self._subscribers.values())
        for sub in subs:
            sub.enqueue_priority(bye_payload)

        # 3) 等待 BYE 消费完 + 关闭订阅者
        # 给一点时间让 send_loop 把 BYE 发出
        await asyncio.sleep(0.3)

        async with self._sub_lock:
            subs = list(self._subscribers.values())
            self._subscribers.clear()

        await asyncio.gather(
            *(sub.stop() for sub in subs),
            return_exceptions=True,
        )

        logger.info(
            f"[ThrottleHub] 已停止 · 累计 ticks={self._ticks_total} · "
            f"pushed={self._pushed_total} · "
            f"skipped_unchanged={self._skipped_unchanged_total}"
        )

    @property
    def is_running(self) -> bool:
        return (
            self._worker_task is not None
            and not self._worker_task.done()
            and not self._stopped
        )

    @property
    def push_hz(self) -> float:
        return self._push_hz

    # ════════════════════════════════════════════════════════════
    # 订阅者管理
    # ════════════════════════════════════════════════════════════

    async def add_subscriber(
        self,
        send_fn: SubscriberSendFn,
        subscriber_id: Optional[str] = None,
    ) -> str:
        """
        注册新订阅者。

        Args:
            send_fn: 异步发送函数 · 通常是 await ws.send_bytes
            subscriber_id: 自定义 ID; None 则自动生成

        Returns:
            订阅者 ID
        """
        if self._stopped:
            raise ThrottleStoppedError("ThrottleHub 已停止")

        sid = subscriber_id or str(uuid.uuid4())
        channel = _SubscriberChannel(subscriber_id=sid, send_fn=send_fn)
        channel.start()

        async with self._sub_lock:
            if sid in self._subscribers:
                # 重复 ID (人为指定时可能发生): 替换并关闭旧的
                old = self._subscribers[sid]
                logger.warning(
                    f"[ThrottleHub] subscriber {sid!r} 已存在,替换旧通道"
                )
                asyncio.create_task(old.stop())
            self._subscribers[sid] = channel

        logger.info(
            f"[ThrottleHub] 订阅者上线 · id={sid!r} · "
            f"total={len(self._subscribers)}"
        )

        # 立即推一次当前状态给新订阅者 (避免空白窗口)
        try:
            bundle = self._collect_bundle()
            payload = self._serialize_packet(
                kind=PacketKind.STATE,
                payload=bundle.to_dict(),
            )
            channel.enqueue_state(payload)
        except Exception as exc:
            logger.warning(
                f"[ThrottleHub] 新订阅者初始化推送失败: {exc!r}"
            )

        return sid

    async def remove_subscriber(self, subscriber_id: str) -> bool:
        async with self._sub_lock:
            channel = self._subscribers.pop(subscriber_id, None)

        if channel is None:
            return False

        await channel.stop()
        logger.info(
            f"[ThrottleHub] 订阅者下线 · id={subscriber_id!r}"
        )
        return True

    async def _prune_unhealthy_subscribers(self) -> int:
        """周期清理 send 失败的订阅者。"""
        async with self._sub_lock:
            to_remove = [
                sid
                for sid, ch in self._subscribers.items()
                if not ch.is_healthy
            ]
            channels = [self._subscribers.pop(sid) for sid in to_remove]

        if not channels:
            return 0

        await asyncio.gather(
            *(ch.stop() for ch in channels),
            return_exceptions=True,
        )
        logger.info(
            f"[ThrottleHub] 清理 unhealthy 订阅者 {len(channels)} 个"
        )
        return len(channels)

    # ════════════════════════════════════════════════════════════
    # 文本流旁路 (实时,不进节流)
    # ════════════════════════════════════════════════════════════

    async def send_text(
        self,
        subscriber_id: str,
        text: str,
        agent_id: Optional[str] = None,
    ) -> bool:
        """
        向单订阅者推送文本 chunk · 实时旁路。

        Returns:
            True 入队成功 / False 订阅者不存在
        """
        async with self._sub_lock:
            channel = self._subscribers.get(subscriber_id)
        if channel is None:
            return False

        payload = self._serialize_packet(
            kind=PacketKind.TEXT,
            payload={
                "agent_id": agent_id,
                "text": text,
            },
        )
        ok = channel.enqueue_priority(payload)
        if ok:
            self._text_passthrough_total += 1
        return ok

    async def broadcast_text(
        self,
        text: str,
        agent_id: Optional[str] = None,
    ) -> int:
        """向所有订阅者广播文本 chunk · 返回成功入队数。"""
        payload = self._serialize_packet(
            kind=PacketKind.TEXT,
            payload={
                "agent_id": agent_id,
                "text": text,
            },
        )
        async with self._sub_lock:
            channels = list(self._subscribers.values())

        success = 0
        for ch in channels:
            if ch.enqueue_priority(payload):
                success += 1
        if success > 0:
            self._text_passthrough_total += 1
        return success

    # ════════════════════════════════════════════════════════════
    # 信号广播 (INTERRUPT / TURN_SEALED / TOPIC / HEARTBEAT)
    # ════════════════════════════════════════════════════════════

    async def broadcast_signal(
        self,
        kind: PacketKind,
        payload: dict[str, object],
    ) -> int:
        """
        广播信号包 (优先级高于状态,FIFO 顺序到达)。

        Returns:
            成功入队数
        """
        if kind == PacketKind.STATE:
            raise ValueError("STATE 类型严禁通过 broadcast_signal,使用 worker tick")
        if kind == PacketKind.TEXT:
            raise ValueError("TEXT 类型请使用 send_text/broadcast_text")

        serialized = self._serialize_packet(kind=kind, payload=payload)
        async with self._sub_lock:
            channels = list(self._subscribers.values())

        success = 0
        for ch in channels:
            if ch.enqueue_priority(serialized):
                success += 1
        return success

    async def broadcast_interrupt(
        self,
        interrupter_agent_id: str,
        interrupted_agent_id: Optional[str],
    ) -> int:
        """便捷 API: 广播 INTERRUPT 信号 (前端 Shake 动效触发器)。"""
        return await self.broadcast_signal(
            kind=PacketKind.INTERRUPT,
            payload={
                "interrupter_agent_id": interrupter_agent_id,
                "interrupted_agent_id": interrupted_agent_id,
                "at_wall_unix": round(time.time(), 6),
            },
        )

    async def broadcast_turn_sealed(
        self,
        turn_dict: dict[str, object],
        keyword_hits: dict[str, list[str]],
        named_targets: tuple[str, ...],
    ) -> int:
        """便捷 API: 广播 Turn 封存事件。"""
        return await self.broadcast_signal(
            kind=PacketKind.TURN_SEALED,
            payload={
                "turn": turn_dict,
                "keyword_hits": {k: list(v) for k, v in keyword_hits.items()},
                "named_targets": list(named_targets),
            },
        )

    async def broadcast_topic(
        self,
        operation: str,
        topic_dict: dict[str, object],
        stack_depth: int,
    ) -> int:
        """便捷 API: 广播议题栈变化。"""
        return await self.broadcast_signal(
            kind=PacketKind.TOPIC,
            payload={
                "operation": operation,
                "topic": topic_dict,
                "stack_depth": stack_depth,
            },
        )

    # ════════════════════════════════════════════════════════════
    # 内部: Worker 主循环
    # ════════════════════════════════════════════════════════════

    async def _worker_supervisor(self) -> None:
        """异常自动重启的顶层守护。"""
        while not self._stop_signal.is_set():
            try:
                await self._worker_loop()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    f"[ThrottleHub] worker_loop 异常 · {exc!r} · "
                    f"{_WORKER_RESTART_BACKOFF_SEC}s 后重启"
                )
                try:
                    await asyncio.sleep(_WORKER_RESTART_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return

    async def _worker_loop(self) -> None:
        """主推送循环 · 周期 tick + 心跳。"""
        logger.info("[ThrottleHub] worker_loop 进入主循环")
        last_heartbeat = time.monotonic()
        last_prune = time.monotonic()
        heartbeat_interval = 25.0  # 与前端 useWebSocket 默认一致
        prune_interval = 5.0

        while not self._stop_signal.is_set():
            tick_start = time.monotonic()

            try:
                await self._tick_once(force=False)
            except Exception as exc:
                # tick_once 内部异常不应中断 supervisor (会被外层捕获重启)
                # 但记录日志,等同于推 supervisor 重启路径
                logger.error(f"[ThrottleHub] tick_once 异常: {exc!r}")
                raise

            # 心跳
            if (tick_start - last_heartbeat) >= heartbeat_interval:
                try:
                    await self.broadcast_signal(
                        kind=PacketKind.HEARTBEAT,
                        payload={"at_wall_unix": round(time.time(), 6)},
                    )
                except Exception as exc:
                    logger.warning(f"[ThrottleHub] heartbeat 异常: {exc!r}")
                last_heartbeat = tick_start

            # 清理 unhealthy 订阅者
            if (tick_start - last_prune) >= prune_interval:
                try:
                    await self._prune_unhealthy_subscribers()
                except Exception as exc:
                    logger.warning(f"[ThrottleHub] prune 异常: {exc!r}")
                last_prune = tick_start

            # 等待下一 tick
            elapsed = time.monotonic() - tick_start
            wait_sec = max(0.0, self._tick_interval_sec - elapsed)
            try:
                await asyncio.wait_for(
                    self._stop_signal.wait(),
                    timeout=wait_sec if wait_sec > 0 else 0.001,
                )
                return  # stop_signal set 退出
            except asyncio.TimeoutError:
                continue

    async def _tick_once(self, force: bool) -> None:
        """
        单次 tick: 采集 → 哈希比对 → 序列化 → 扇出。

        Args:
            force: True 跳过 hash 比对 (用于 stop 时强制 flush)
        """
        self._ticks_total += 1
        bundle = self._collect_bundle()
        payload_dict = bundle.to_dict()
        serialized = self._serialize_packet(
            kind=PacketKind.STATE,
            payload=payload_dict,
        )

        # 哈希比对 · 内容相同则跳过 (但 sequence_id 已递增)
        if not force:
            digest = hashlib.blake2b(serialized, digest_size=16).digest()
            if digest == self._last_state_hash:
                self._skipped_unchanged_total += 1
                return
            self._last_state_hash = digest
        else:
            self._last_state_hash = b""

        # 扇出
        async with self._sub_lock:
            channels = list(self._subscribers.values())

        if not channels:
            return  # 无订阅者,序列化 + hash 仍要做以维持 last_state_hash 准确

        for ch in channels:
            ch.enqueue_state(serialized)

        self._pushed_total += 1
        self._push_history.append(time.monotonic())

    # ════════════════════════════════════════════════════════════
    # 内部: 采集 + 序列化
    # ════════════════════════════════════════════════════════════

    def _collect_bundle(self) -> StateBundle:
        """从各 core 模块采集合成 StateBundle。"""
        self._sequence_id += 1
        if self._sequence_id > 2_147_483_647:
            self._sequence_id = 1  # 防止 32 位溢出

        # 信号量指标 (provider 注入,失败兜底)
        if self._sem_provider is not None:
            try:
                sem_metrics = self._sem_provider()
                if not isinstance(sem_metrics, dict):
                    sem_metrics = {}
            except Exception as exc:
                logger.debug(
                    f"[ThrottleHub] semaphore_metrics_provider 异常: {exc!r}"
                )
                sem_metrics = {}
        else:
            sem_metrics = {}

        return StateBundle(
            sequence_id=self._sequence_id,
            at_monotonic=time.monotonic(),
            at_wall_unix=time.time(),
            fsm=self._fsm.snapshot_dict(),
            aggro=self._aggro.snapshot_dict(),
            arbitrator=self._arbitrator.snapshot_dict(),
            watchdog=self._watchdog.snapshot_dict(),
            semaphore=sem_metrics,
        )

    @staticmethod
    def _serialize_packet(
        kind: PacketKind,
        payload: dict[str, object],
    ) -> bytes:
        """统一包封装: {kind, payload, server_at_wall_unix}"""
        envelope = {
            "kind": kind.value,
            "payload": payload,
            "server_at_wall_unix": round(time.time(), 6),
        }
        try:
            return orjson.dumps(envelope)
        except (TypeError, orjson.JSONEncodeError) as exc:
            logger.error(
                f"[ThrottleHub] orjson 序列化失败 · kind={kind.value} · {exc!r}"
            )
            # 兜底: 错误降级为最小包
            fallback = {
                "kind": kind.value,
                "payload": {"_serialization_error": str(exc)},
                "server_at_wall_unix": round(time.time(), 6),
            }
            return orjson.dumps(fallback)

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> ThrottleMetrics:
        healthy = sum(1 for ch in self._subscribers.values() if ch.is_healthy)
        return ThrottleMetrics(
            is_running=self.is_running,
            push_hz=self._push_hz,
            subscribers_count=len(self._subscribers),
            healthy_subscribers=healthy,
            last_sequence_id=self._sequence_id,
            ticks_total=self._ticks_total,
            pushed_total=self._pushed_total,
            skipped_unchanged_total=self._skipped_unchanged_total,
            text_passthrough_total=self._text_passthrough_total,
        )

    def subscribers_snapshot(self) -> list[dict[str, object]]:
        return [ch.stats.to_dict() for ch in self._subscribers.values()]

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "subscribers": self.subscribers_snapshot(),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_hub_singleton: Optional[ThrottleHub] = None


def get_throttle_hub() -> ThrottleHub:
    """
    获取全局 ThrottleHub 单例。

    使用模式 (lifespan):
        @asynccontextmanager
        async def lifespan(app):
            hub = get_throttle_hub()
            await hub.start()
            try:
                yield
            finally:
                await hub.stop()
    """
    global _hub_singleton
    if _hub_singleton is None:
        from backend.core.aggro_engine import get_aggro_engine
        from backend.core.arbitrator import get_arbitrator
        from backend.core.fsm import get_fsm_registry
        from backend.core.semaphore import get_llm_semaphore_sync
        from backend.core.watchdog import get_watchdog

        # 信号量 metrics 通过 closure 注入 (避免 throttle 直接依赖 semaphore 单例)
        def _sem_metrics_provider() -> dict[str, Any]:
            try:
                return get_llm_semaphore_sync().metrics().to_dict()
            except Exception:
                return {}

        s = get_settings()
        _hub_singleton = ThrottleHub(
            fsm_registry=get_fsm_registry(),
            aggro_engine=get_aggro_engine(),
            arbitrator=get_arbitrator(),
            watchdog=get_watchdog(),
            push_hz=s.WS_STATE_PUSH_HZ,
            semaphore_metrics_provider=_sem_metrics_provider,
        )
    return _hub_singleton


def reset_hub_for_testing() -> None:
    """[仅供测试] 重置全局单例 (不调 stop)。"""
    global _hub_singleton
    _hub_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 枚举
    "PacketKind",
    # 主体
    "ThrottleHub",
    # 数据类
    "StateBundle",
    "SubscriberStats",
    "ThrottleMetrics",
    # 异常
    "ThrottleError",
    "ThrottleStoppedError",
    "SubscriberNotFoundError",
    # 协议
    "SubscriberSendFn",
    # 单例
    "get_throttle_hub",
    "reset_hub_for_testing",
]