"""
==============================================================================
backend/core/watchdog.py
------------------------------------------------------------------------------
全局超时看门狗 · 僵尸任务清扫器 · Independent Watchdog Worker

设计动机 (纵深防御):
    llm_router.py 内已有内部计时器 (_watchdog_timer),但若该任务被
    上游 cancel / loop 调度异常 / 内部 await 卡死 → 计时器可能失效。
    本模块作为 *独立后台 Worker*,周期扫描所有 in-flight 调用,
    形成纵深防御的第二层。

核心机制:
    1. 全局任务注册表 (WatchEntry):
       业务层每次 LLM 调用注册一条 entry · 含 deadline / event / token / task
    2. 周期扫描 Worker (默认 0.5 Hz · 即每 0.5 秒一次):
       发现超 deadline → set 该 entry 的 watchdog_event (软驱逐)
       内部计时器与本 Worker 共享同一 event,谁先到都触发熔断
    3. 二级强制驱逐 (硬驱逐):
       若 event.set 后仍超过 HARD_EVICT_GRACE_SEC 仍未 unregister →
       说明 llm_router 收尾路径也卡死 → 直接 task.cancel() +
       arbitrator.force_release_if_held_by() 强行解锁仲裁器
    4. lifespan 集成:
       start() / stop() 由 FastAPI lifespan 钩入,
       stop 时调用 emergency_evict_all 主动驱逐所有 in-flight
    5. 上下文管理器 watch():
       async with watchdog.watch(...) as handle: ...
       自动注册 + 异常路径自动 unregister · 杜绝泄漏

⚠️ 工程铁律:
    - register 必须配对 unregister (强制使用 watch() 上下文管理器)
    - 扫描 Worker 顶层 try/except + 自动重启 · 严禁静默死亡
    - 硬驱逐路径必须同时通知 arbitrator,否则仲裁器永远卡死
    - stop() 时主动 emergency_evict_all,杜绝 lifespan shutdown 泄漏

==============================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Final, Optional

from loguru import logger

from backend.core.arbitrator import Arbitrator
from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 物理常量
# ──────────────────────────────────────────────────────────────────────────────

#: Worker 扫描周期 (秒) · 频率不必太高,看门狗本身允许有 sub-second 级延迟
_SCAN_INTERVAL_SEC: Final[float] = 0.5

#: 软驱逐 (event.set) 后的宽限期 · 超过仍未 unregister 即触发硬驱逐
_HARD_EVICT_GRACE_SEC: Final[float] = 8.0

#: 历史环容量
_HISTORY_RING_SIZE: Final[int] = 256

#: Worker 异常崩溃后的重启冷却 (秒) · 避免热循环
_WORKER_RESTART_BACKOFF_SEC: Final[float] = 1.0


# ──────────────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class WatchEntry:
    """
    单个被监视的 LLM 调用记录。

    业务层不直接构造,通过 watchdog.watch() 上下文管理器获取 handle。
    """

    entry_id: str
    agent_id: str
    deadline_monotonic: float
    """到达此 monotonic 时间即触发软驱逐"""

    watchdog_event: asyncio.Event
    """软驱逐通道 · 共享给 llm_router 的三路监听"""

    task: Optional[asyncio.Task[object]]
    """承载 LLM 调用的协程任务 · 硬驱逐时会被 cancel"""

    arbitrator_token_holder_id: Optional[str]
    """该调用持有麦克风的 Agent ID (供硬驱逐时通知仲裁器)"""

    registered_at_monotonic: float
    soft_evicted: bool = False
    soft_evicted_at_monotonic: float = 0.0
    note: str = ""

    def is_overdue(self, now: float) -> bool:
        return now >= self.deadline_monotonic

    def needs_hard_evict(self, now: float) -> bool:
        if not self.soft_evicted:
            return False
        return (now - self.soft_evicted_at_monotonic) >= _HARD_EVICT_GRACE_SEC

    def to_dict(self) -> dict[str, object]:
        now = time.monotonic()
        return {
            "entry_id": self.entry_id,
            "agent_id": self.agent_id,
            "registered_for_seconds": round(
                now - self.registered_at_monotonic, 3
            ),
            "deadline_in_seconds": round(self.deadline_monotonic - now, 3),
            "soft_evicted": self.soft_evicted,
            "soft_evicted_seconds_ago": (
                round(now - self.soft_evicted_at_monotonic, 3)
                if self.soft_evicted
                else None
            ),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class EvictionRecord:
    """单次驱逐 (软或硬) 的不可变记录,供历史环复盘。"""

    entry_id: str
    agent_id: str
    severity: str
    """"soft" | "hard" """
    reason: str
    at_monotonic: float
    at_wall_unix: float
    overdue_seconds: float

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "agent_id": self.agent_id,
            "severity": self.severity,
            "reason": self.reason,
            "at_monotonic": round(self.at_monotonic, 6),
            "at_wall_unix": round(self.at_wall_unix, 6),
            "overdue_seconds": round(self.overdue_seconds, 4),
        }


@dataclass(slots=True)
class WatchdogMetrics:
    """看门狗运行指标 · 透出到 /api/sandbox/snapshot。"""

    is_running: bool
    in_flight_count: int
    soft_evict_total: int
    hard_evict_total: int
    cancelled_total: int
    last_scan_at_monotonic: float
    last_eviction: Optional[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "in_flight_count": self.in_flight_count,
            "soft_evict_total": self.soft_evict_total,
            "hard_evict_total": self.hard_evict_total,
            "cancelled_total": self.cancelled_total,
            "last_scan_at_monotonic": round(self.last_scan_at_monotonic, 6),
            "last_eviction": self.last_eviction,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 业务层 Handle (从上下文管理器返回)
# ──────────────────────────────────────────────────────────────────────────────


class WatchHandle:
    """
    业务层在 async with watchdog.watch(...) 中持有的句柄。

    关键 API:
        - watchdog_event: 业务层在三路监听中等待的事件
        - extend(seconds): 动态延长 deadline (例: 流稳定输出时延长)
        - mark_alive(): 重置 deadline 到 now + original_window
                        (即"我还活着,别熔断")
    """

    __slots__ = (
        "_entry_id",
        "_watchdog",
        "_event",
        "_original_window_sec",
    )

    def __init__(
        self,
        entry_id: str,
        watchdog_ref: "GlobalWatchdog",
        event: asyncio.Event,
        original_window_sec: float,
    ) -> None:
        self._entry_id = entry_id
        self._watchdog = watchdog_ref
        self._event = event
        self._original_window_sec = original_window_sec

    @property
    def entry_id(self) -> str:
        return self._entry_id

    @property
    def watchdog_event(self) -> asyncio.Event:
        return self._event

    def extend(self, seconds: float) -> bool:
        """延长 deadline · 仅在 entry 仍存活时生效。"""
        if seconds <= 0:
            raise ValueError(f"extend seconds 必须 > 0,当前: {seconds}")
        return self._watchdog._extend_entry(self._entry_id, seconds)

    def mark_alive(self) -> bool:
        """重置 deadline 到 now + original_window_sec。"""
        return self._watchdog._reset_deadline(
            self._entry_id, self._original_window_sec
        )


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class WatchdogError(Exception):
    pass


class WatchdogNotRunningError(WatchdogError):
    """register 时 Worker 未启动。"""


class WatchdogStoppedError(WatchdogError):
    """stop() 后再 register。"""


# ──────────────────────────────────────────────────────────────────────────────
# 主体: GlobalWatchdog
# ──────────────────────────────────────────────────────────────────────────────


class GlobalWatchdog:
    """
    进程级全局看门狗 · 后台 Worker 周期扫描超时任务。

    生命周期:
        watchdog = get_watchdog()
        await watchdog.start()              # lifespan startup
        async with watchdog.watch(...) as h:
            ...
        await watchdog.stop()               # lifespan shutdown
    """

    def __init__(
        self,
        arbitrator: Arbitrator,
        scan_interval_sec: float = _SCAN_INTERVAL_SEC,
        hard_evict_grace_sec: float = _HARD_EVICT_GRACE_SEC,
    ) -> None:
        if scan_interval_sec <= 0.0:
            raise ValueError(
                f"scan_interval_sec 必须 > 0,当前: {scan_interval_sec}"
            )
        if hard_evict_grace_sec < 1.0:
            raise ValueError(
                f"hard_evict_grace_sec 必须 ≥ 1,当前: {hard_evict_grace_sec}"
            )

        self._arbitrator: Final[Arbitrator] = arbitrator
        self._scan_interval_sec: Final[float] = scan_interval_sec
        self._hard_evict_grace_sec: Final[float] = hard_evict_grace_sec

        self._entries: dict[str, WatchEntry] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._stopped: bool = False

        # 累计指标
        self._soft_evict_total: int = 0
        self._hard_evict_total: int = 0
        self._cancelled_total: int = 0
        self._last_scan_at_monotonic: float = 0.0

        # 历史环
        self._history: deque[EvictionRecord] = deque(
            maxlen=_HISTORY_RING_SIZE
        )

        logger.info(
            f"[Watchdog] 初始化 · scan_interval={scan_interval_sec}s · "
            f"hard_evict_grace={hard_evict_grace_sec}s"
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """启动后台扫描 Worker · 幂等。"""
        if self._stopped:
            raise WatchdogStoppedError(
                "Watchdog 已 stop,不可重启 (重启请创建新实例)"
            )
        if self._worker_task is not None and not self._worker_task.done():
            logger.debug("[Watchdog] start 被重复调用 (已运行)")
            return
        self._stop_signal.clear()
        self._worker_task = asyncio.create_task(
            self._scan_loop_supervisor(),
            name="watchdog_scan_loop",
        )
        logger.info("[Watchdog] 后台 Worker 已启动")

    async def stop(self, evict_timeout_sec: float = 5.0) -> None:
        """
        停止 Worker · 主动驱逐所有 in-flight。

        Args:
            evict_timeout_sec: 等待 in-flight 自然完成的最大时长,
                               超过则强制硬驱逐
        """
        if self._stopped:
            return
        self._stopped = True

        # 1) 通知 Worker 停止扫描
        self._stop_signal.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        # 2) 紧急驱逐所有 in-flight
        await self.emergency_evict_all(reason="watchdog_stop")

        # 3) 等待 in-flight 自然消解 (最多 evict_timeout_sec)
        deadline = time.monotonic() + evict_timeout_sec
        while time.monotonic() < deadline:
            async with self._lock:
                if not self._entries:
                    break
            await asyncio.sleep(0.1)

        async with self._lock:
            still_alive = list(self._entries.values())

        if still_alive:
            logger.warning(
                f"[Watchdog] stop 时仍有 {len(still_alive)} 个 entry 未清理 · "
                f"agents={[e.agent_id for e in still_alive]}"
            )

        logger.info(
            f"[Watchdog] 已停止 · 累计 soft={self._soft_evict_total} · "
            f"hard={self._hard_evict_total} · cancelled={self._cancelled_total}"
        )

    @property
    def is_running(self) -> bool:
        return (
            self._worker_task is not None
            and not self._worker_task.done()
            and not self._stopped
        )

    # ════════════════════════════════════════════════════════════
    # 业务接口: watch 上下文管理器
    # ════════════════════════════════════════════════════════════

    @contextlib.asynccontextmanager
    async def watch(
        self,
        agent_id: str,
        timeout_sec: Optional[float] = None,
        task: Optional[asyncio.Task[object]] = None,
        arbitrator_token_holder_id: Optional[str] = None,
        note: str = "",
    ) -> AsyncIterator[WatchHandle]:
        """
        注册一个被监视的调用,返回 handle。退出时自动 unregister。

        Args:
            agent_id: 关联的 Agent
            timeout_sec: 超时阈值 · None 取 settings.LLM_REQUEST_TIMEOUT_SEC
            task: 承载该调用的 asyncio.Task (硬驱逐时会被 cancel)
                  None 表示无法硬驱逐 (仅依赖软驱逐 event)
            arbitrator_token_holder_id: 该调用持有麦克风的 Agent ID
                  (硬驱逐时通知 arbitrator force_release_if_held_by)
            note: 诊断备注

        典型用法 (在 agents/*.py 或 api/websocket.py):
            async with watchdog.watch(
                agent_id="INTJ_logician",
                task=current_task,
                arbitrator_token_holder_id="INTJ_logician",
            ) as wh:
                result = await router.invoke_chat_stream(
                    ...,
                    watchdog_event=wh.watchdog_event,
                )
        """
        if self._stopped:
            raise WatchdogStoppedError("Watchdog 已停止")
        if not self.is_running:
            raise WatchdogNotRunningError(
                "Watchdog Worker 未启动 · 请先在 lifespan startup 调用 start()"
            )
        if timeout_sec is None:
            timeout_sec = get_settings().LLM_REQUEST_TIMEOUT_SEC
        if timeout_sec <= 0:
            raise ValueError(
                f"timeout_sec 必须 > 0,当前: {timeout_sec}"
            )

        handle = await self._register(
            agent_id=agent_id,
            timeout_sec=timeout_sec,
            task=task,
            arbitrator_token_holder_id=arbitrator_token_holder_id,
            note=note,
        )

        try:
            yield handle
        finally:
            await self._unregister(handle.entry_id)

    # ════════════════════════════════════════════════════════════
    # 内部: 注册 / 注销
    # ════════════════════════════════════════════════════════════

    async def _register(
        self,
        agent_id: str,
        timeout_sec: float,
        task: Optional[asyncio.Task[object]],
        arbitrator_token_holder_id: Optional[str],
        note: str,
    ) -> WatchHandle:
        entry_id = str(uuid.uuid4())
        now = time.monotonic()
        event = asyncio.Event()
        entry = WatchEntry(
            entry_id=entry_id,
            agent_id=agent_id,
            deadline_monotonic=now + timeout_sec,
            watchdog_event=event,
            task=task,
            arbitrator_token_holder_id=arbitrator_token_holder_id,
            registered_at_monotonic=now,
            note=note,
        )

        async with self._lock:
            self._entries[entry_id] = entry

        logger.debug(
            f"[Watchdog] register · entry={entry_id} · "
            f"agent={agent_id!r} · deadline_in={timeout_sec}s · note={note!r}"
        )
        return WatchHandle(
            entry_id=entry_id,
            watchdog_ref=self,
            event=event,
            original_window_sec=timeout_sec,
        )

    async def _unregister(self, entry_id: str) -> bool:
        async with self._lock:
            entry = self._entries.pop(entry_id, None)
        if entry is not None:
            logger.debug(
                f"[Watchdog] unregister · entry={entry_id} · "
                f"agent={entry.agent_id!r}"
            )
            return True
        return False

    def _extend_entry(self, entry_id: str, seconds: float) -> bool:
        """延长 deadline (同步方法,锁外读写,但容忍 race)。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.deadline_monotonic += seconds
        return True

    def _reset_deadline(self, entry_id: str, window_sec: float) -> bool:
        """重置 deadline 到 now + window_sec。"""
        entry = self._entries.get(entry_id)
        if entry is None:
            return False
        entry.deadline_monotonic = time.monotonic() + window_sec
        return True

    # ════════════════════════════════════════════════════════════
    # 内部: 后台扫描 Worker
    # ════════════════════════════════════════════════════════════

    async def _scan_loop_supervisor(self) -> None:
        """
        Worker 顶层守护循环 · 异常自动重启。

        严禁让 Worker 静默死亡。
        """
        while not self._stop_signal.is_set():
            try:
                await self._scan_loop()
            except asyncio.CancelledError:
                logger.debug("[Watchdog] scan_loop 被取消 · 退出")
                return
            except Exception as exc:  # 任何未知异常都不应让 Worker 死亡
                logger.error(
                    f"[Watchdog] scan_loop 异常 · {exc!r} · "
                    f"{_WORKER_RESTART_BACKOFF_SEC}s 后重启"
                )
                try:
                    await asyncio.sleep(_WORKER_RESTART_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return

    async def _scan_loop(self) -> None:
        """
        主扫描循环 · 每 scan_interval_sec 跑一遍。
        """
        logger.info("[Watchdog] scan_loop 进入主循环")
        while not self._stop_signal.is_set():
            await self._scan_once()
            try:
                await asyncio.wait_for(
                    self._stop_signal.wait(),
                    timeout=self._scan_interval_sec,
                )
                # stop_signal set 了,退出主循环
                return
            except asyncio.TimeoutError:
                continue  # 正常: 到了下一周期

    async def _scan_once(self) -> None:
        now = time.monotonic()
        self._last_scan_at_monotonic = now

        # 拷贝列表 (持锁) → 锁外执行驱逐 (避免与 _register/_unregister 死锁)
        async with self._lock:
            snapshot = list(self._entries.values())

        for entry in snapshot:
            try:
                if entry.needs_hard_evict(now):
                    await self._hard_evict(entry, reason="grace_exceeded")
                elif not entry.soft_evicted and entry.is_overdue(now):
                    await self._soft_evict(entry, reason="deadline_passed")
            except Exception as exc:
                logger.error(
                    f"[Watchdog] 扫描时驱逐异常 · entry={entry.entry_id} · "
                    f"agent={entry.agent_id!r} · err={exc!r}"
                )

    # ════════════════════════════════════════════════════════════
    # 内部: 软/硬驱逐
    # ════════════════════════════════════════════════════════════

    async def _soft_evict(self, entry: WatchEntry, reason: str) -> None:
        """
        软驱逐: set 该 entry 的 watchdog_event。

        llm_router 的三路监听会在下个 wait 醒来 → seal_with_shadow_suffix
        → release_token → unregister · 整套流程在 grace 窗口内完成。
        """
        if entry.soft_evicted:
            return
        now = time.monotonic()
        entry.soft_evicted = True
        entry.soft_evicted_at_monotonic = now
        entry.watchdog_event.set()
        self._soft_evict_total += 1

        overdue = max(0.0, now - entry.deadline_monotonic)
        record = EvictionRecord(
            entry_id=entry.entry_id,
            agent_id=entry.agent_id,
            severity="soft",
            reason=reason,
            at_monotonic=now,
            at_wall_unix=time.time(),
            overdue_seconds=overdue,
        )
        self._history.append(record)

        logger.warning(
            f"[Watchdog] SOFT EVICT · entry={entry.entry_id} · "
            f"agent={entry.agent_id!r} · overdue={overdue:.2f}s · "
            f"reason={reason}"
        )

    async def _hard_evict(self, entry: WatchEntry, reason: str) -> None:
        """
        硬驱逐: cancel 任务 + 通知仲裁器 + 强制 unregister。

        当软驱逐后 grace 期内仍未 unregister 时触发,
        说明 llm_router 的收尾路径也卡死了。
        """
        now = time.monotonic()
        self._hard_evict_total += 1

        # 1) cancel 任务
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
            self._cancelled_total += 1
            logger.error(
                f"[Watchdog] HARD EVICT · cancel task · "
                f"entry={entry.entry_id} · agent={entry.agent_id!r}"
            )
        else:
            logger.error(
                f"[Watchdog] HARD EVICT · 无可 cancel 的 task · "
                f"entry={entry.entry_id} · agent={entry.agent_id!r}"
            )

        # 2) 通知仲裁器强制释放令牌 (若该 Agent 仍持有)
        if entry.arbitrator_token_holder_id is not None:
            try:
                await self._arbitrator.force_release_if_held_by(
                    agent_id=entry.arbitrator_token_holder_id,
                    reason=f"watchdog_hard_evict:{reason}",
                )
            except Exception as exc:
                logger.error(
                    f"[Watchdog] force_release_if_held_by 异常 · "
                    f"agent={entry.arbitrator_token_holder_id!r} · "
                    f"err={exc!r}"
                )

        # 3) 强制从注册表移除 (即使 task cancel 后协程还没真正回收)
        async with self._lock:
            self._entries.pop(entry.entry_id, None)

        overdue = max(0.0, now - entry.deadline_monotonic)
        record = EvictionRecord(
            entry_id=entry.entry_id,
            agent_id=entry.agent_id,
            severity="hard",
            reason=reason,
            at_monotonic=now,
            at_wall_unix=time.time(),
            overdue_seconds=overdue,
        )
        self._history.append(record)

    # ════════════════════════════════════════════════════════════
    # 紧急驱逐 (lifespan stop)
    # ════════════════════════════════════════════════════════════

    async def emergency_evict_all(
        self,
        reason: str = "emergency",
    ) -> int:
        """
        紧急驱逐所有 in-flight · 先软后硬。

        Returns:
            被驱逐的条目数
        """
        async with self._lock:
            snapshot = list(self._entries.values())

        # 第一轮: 全部软驱逐
        for entry in snapshot:
            try:
                if not entry.soft_evicted:
                    await self._soft_evict(entry, reason=reason)
            except Exception as exc:
                logger.error(
                    f"[Watchdog] emergency soft_evict 异常 · "
                    f"agent={entry.agent_id!r} · err={exc!r}"
                )

        # 给软驱逐路径一点时间走完 (但不能太长,防 stop 卡住)
        await asyncio.sleep(min(2.0, self._hard_evict_grace_sec / 2))

        # 第二轮: 仍存活的强行硬驱逐
        async with self._lock:
            stragglers = list(self._entries.values())

        for entry in stragglers:
            try:
                await self._hard_evict(entry, reason=f"{reason}_stragglers")
            except Exception as exc:
                logger.error(
                    f"[Watchdog] emergency hard_evict 异常 · "
                    f"agent={entry.agent_id!r} · err={exc!r}"
                )

        total = len(snapshot)
        logger.warning(
            f"[Watchdog] emergency_evict_all 完成 · "
            f"reason={reason} · evicted={total}"
        )
        return total

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> WatchdogMetrics:
        last = (
            self._history[-1].to_dict() if self._history else None
        )
        return WatchdogMetrics(
            is_running=self.is_running,
            in_flight_count=len(self._entries),
            soft_evict_total=self._soft_evict_total,
            hard_evict_total=self._hard_evict_total,
            cancelled_total=self._cancelled_total,
            last_scan_at_monotonic=self._last_scan_at_monotonic,
            last_eviction=last,
        )

    def history(self, last_n: Optional[int] = None) -> list[EvictionRecord]:
        records = list(self._history)
        if last_n is not None:
            if last_n < 0:
                raise ValueError("last_n 必须 ≥ 0")
            records = records[-last_n:] if last_n else []
        return records

    def in_flight_snapshot(self) -> list[dict[str, object]]:
        """当前所有 in-flight entry 的诊断快照。"""
        return [e.to_dict() for e in self._entries.values()]

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "in_flight": self.in_flight_snapshot(),
            "recent_evictions": [
                r.to_dict() for r in list(self._history)[-16:]
            ],
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_watchdog_singleton: Optional[GlobalWatchdog] = None


def get_watchdog() -> GlobalWatchdog:
    """
    获取全局 Watchdog 单例。

    使用模式 (lifespan):
        @asynccontextmanager
        async def lifespan(app):
            wd = get_watchdog()
            await wd.start()
            try:
                yield
            finally:
                await wd.stop()
    """
    global _watchdog_singleton
    if _watchdog_singleton is None:
        from backend.core.arbitrator import get_arbitrator
        _watchdog_singleton = GlobalWatchdog(arbitrator=get_arbitrator())
    return _watchdog_singleton


def reset_watchdog_for_testing() -> None:
    """[仅供测试] 重置全局单例 (不调 stop)。"""
    global _watchdog_singleton
    _watchdog_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 主体
    "GlobalWatchdog",
    "WatchHandle",
    # 数据类
    "WatchEntry",
    "EvictionRecord",
    "WatchdogMetrics",
    # 异常
    "WatchdogError",
    "WatchdogNotRunningError",
    "WatchdogStoppedError",
    # 单例
    "get_watchdog",
    "reset_watchdog_for_testing",
]