"""
==============================================================================
backend/core/semaphore.py
------------------------------------------------------------------------------
全局算力信号量 · LLM Concurrency Gatekeeper

这是防 OOM 死锁的最后一道闸门。Ollama 内部并不做精细队列,过高并发会触发
metal allocator 抖动并最终 OOM。本模块通过进程级单例信号量,把"同时推理数"
死死压在 GLOBAL_LLM_CONCURRENCY 之内。

设计原则:
    1. 进程级单例: 通过模块级懒加载 + asyncio.Lock 双重检查锁定,
       杜绝多协程并发初始化时创建多个信号量实例的竞态。
    2. async with 异常安全: 任何路径 (正常 / 异常 / CancelledError) 都
       保证 release。严禁暴露裸 acquire/release,防止配对错误。
    3. 全链路可观测: 实时暴露 held / waiting / acquired_total / ...
       六维指标,供 /api/sandbox/snapshot 透出给 HUD 监控。
    4. 优雅关闭: drain() 阻塞等待所有持有者归还,带兜底超时,
       SIGTERM 后系统不留僵尸推理任务。
    5. 持有者追踪: 每个 token 携带 holder_name (Agent ID),
       便于诊断"是谁卡住了信号量"。

⚠️ 铁律:
    - 信号量是 *进程级* 资源,uvicorn 必须 --workers 1 单进程运行,
      多 worker 场景下本模块完全失效 (Dockerfile CMD 已强约束)。
    - 严禁直接 import 全局变量,必须通过 get_llm_semaphore() 获取。

==============================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from types import TracebackType
from typing import AsyncIterator, Optional, Type

from loguru import logger

from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 观测指标数据类
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SemaphoreMetrics:
    """
    信号量运行时指标快照 (供 HUD / 诊断 API 序列化暴露)。

    所有字段都是单调累计或瞬时值,前端可基于差分计算速率。
    """

    capacity: int = 0
    """配置的最大并发数 (= GLOBAL_LLM_CONCURRENCY)"""

    held_count: int = 0
    """当前正在持有信号量的协程数 (∈ [0, capacity])"""

    waiting_count: int = 0
    """当前正在 acquire() 中阻塞等待的协程数"""

    acquired_total: int = 0
    """累计成功获取次数 (单调递增)"""

    released_total: int = 0
    """累计释放次数 (单调递增)"""

    timeout_total: int = 0
    """累计超时失败次数 (try_acquire 返回 False)"""

    cancelled_total: int = 0
    """累计被 CancelledError 中断的获取次数"""

    last_acquire_wait_ms: float = 0.0
    """最近一次 acquire 的等待耗时 (毫秒) · 用于 SLO 监控"""

    holders: tuple[str, ...] = field(default_factory=tuple)
    """当前持有者名称列表 (Agent ID),按获取顺序"""

    def to_dict(self) -> dict[str, object]:
        """序列化为 JSON 友好字典 (供 Pydantic / orjson 透传)。"""
        return {
            "capacity": self.capacity,
            "held_count": self.held_count,
            "waiting_count": self.waiting_count,
            "acquired_total": self.acquired_total,
            "released_total": self.released_total,
            "timeout_total": self.timeout_total,
            "cancelled_total": self.cancelled_total,
            "last_acquire_wait_ms": round(self.last_acquire_wait_ms, 3),
            "holders": list(self.holders),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 持有令牌 (Acquired Token)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _SemaphoreToken:
    """
    单次持有的内部记录。

    严禁外部直接构造,由 LLMSemaphore._track_holder() 内部生成。
    """

    holder_name: str
    acquired_at_monotonic: float
    sequence_id: int


# ──────────────────────────────────────────────────────────────────────────────
# 主体: LLMSemaphore
# ──────────────────────────────────────────────────────────────────────────────


class LLMSemaphore:
    """
    全局 LLM 算力信号量。

    核心 API:
        - guard(holder_name)          : async context manager (推荐使用)
        - try_guard(holder_name, ...) : async context manager,带超时,失败抛错
        - try_acquire(holder, timeout): 非阻塞尝试,返回 bool
        - drain(timeout)              : 优雅关闭,等所有持有者归还
        - metrics()                   : 快照导出

    严禁暴露的接口:
        - acquire() / release() 裸调用 —— 强制走 async with 路径
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(
                f"LLMSemaphore capacity 必须 ≥ 1,当前: {capacity}"
            )
        if capacity > 3:
            # 工程约束: > 3 极易引发 Metal allocator 抖动,直接拒绝
            raise ValueError(
                f"LLMSemaphore capacity 严禁超过 3 (当前: {capacity}) —— "
                f"Ollama 内部不做精细队列,过高并发会触发 OOM"
            )

        self._capacity: int = capacity
        # asyncio.Semaphore 必须在事件循环就绪后构造,
        # 但 asyncio.Semaphore 在 Py3.11+ 已支持 lazy loop 绑定。
        self._inner: asyncio.Semaphore = asyncio.Semaphore(capacity)

        # 观测指标 (所有写入都在事件循环单线程内,无需额外锁)
        self._held_count: int = 0
        self._waiting_count: int = 0
        self._acquired_total: int = 0
        self._released_total: int = 0
        self._timeout_total: int = 0
        self._cancelled_total: int = 0
        self._last_acquire_wait_ms: float = 0.0

        # 持有者追踪: sequence_id -> token
        self._holders: dict[int, _SemaphoreToken] = {}
        self._sequence_counter: int = 0

        # 优雅关闭事件: drain() 时切到 True,新 acquire 立即抛错
        self._closing: bool = False

        # 全部归还信号: 每次 release 都 set 一次,drain() 据此 wait
        self._all_released_event: asyncio.Event = asyncio.Event()
        self._all_released_event.set()  # 初始无持有者,直接 set

        logger.info(
            f"[LLMSemaphore] 初始化完成 · capacity={capacity} · "
            f"严禁 uvicorn 多 worker 部署"
        )

    # ────────────────────────────────────────────────────────────
    # 公开属性
    # ────────────────────────────────────────────────────────────

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def held_count(self) -> int:
        return self._held_count

    @property
    def waiting_count(self) -> int:
        return self._waiting_count

    @property
    def is_closing(self) -> bool:
        return self._closing

    # ────────────────────────────────────────────────────────────
    # 核心: 上下文管理器 (推荐使用方式)
    # ────────────────────────────────────────────────────────────

    @contextlib.asynccontextmanager
    async def guard(
        self,
        holder_name: str,
    ) -> AsyncIterator[None]:
        """
        无超时获取信号量,async with 风格,异常安全。

        典型用法:
            async with llm_semaphore.guard(agent_id):
                async for chunk in ollama_stream(...):
                    ...

        异常路径:
            - SemaphoreClosingError: 系统正在 drain,拒绝新获取
            - asyncio.CancelledError: 等待期间被取消,会向上抛出 (信号量已释放)
        """
        if not holder_name or not isinstance(holder_name, str):
            raise ValueError(
                f"holder_name 必须为非空字符串,当前: {holder_name!r}"
            )

        if self._closing:
            raise SemaphoreClosingError(
                f"信号量正在优雅关闭中,拒绝新获取 (holder={holder_name!r})"
            )

        token: Optional[_SemaphoreToken] = None
        wait_start = time.monotonic()
        self._waiting_count += 1
        try:
            try:
                await self._inner.acquire()
            except asyncio.CancelledError:
                # 等待期间被取消,inner 不会泄漏 (asyncio.Semaphore 自身处理)
                self._cancelled_total += 1
                logger.debug(
                    f"[LLMSemaphore] acquire 被取消 · holder={holder_name!r}"
                )
                raise
            finally:
                self._waiting_count -= 1

            # ── 已获取 ──
            wait_ms = (time.monotonic() - wait_start) * 1000.0
            self._last_acquire_wait_ms = wait_ms
            self._acquired_total += 1
            token = self._track_holder(holder_name)
            logger.debug(
                f"[LLMSemaphore] acquired · holder={holder_name!r} · "
                f"wait={wait_ms:.1f}ms · held={self._held_count}/{self._capacity}"
            )

            yield  # ← 业务代码在此执行

        finally:
            # ── 无论何种路径都必须释放 ──
            if token is not None:
                self._release_token(token)

    @contextlib.asynccontextmanager
    async def try_guard(
        self,
        holder_name: str,
        timeout_sec: float,
    ) -> AsyncIterator[None]:
        """
        带超时的 async with 获取。超时则抛 SemaphoreTimeoutError。

        典型用法 (例如 ENFP 破冰申请,愿意快速放弃):
            try:
                async with llm_semaphore.try_guard("ENFP_jester", timeout_sec=2.0):
                    await ollama_stream(...)
            except SemaphoreTimeoutError:
                logger.info("ENFP 放弃破冰,等待下一窗口")
        """
        if timeout_sec <= 0:
            raise ValueError(
                f"timeout_sec 必须 > 0,当前: {timeout_sec}"
            )
        if self._closing:
            raise SemaphoreClosingError(
                f"信号量正在优雅关闭中,拒绝新获取 (holder={holder_name!r})"
            )

        token: Optional[_SemaphoreToken] = None
        wait_start = time.monotonic()
        self._waiting_count += 1
        try:
            try:
                await asyncio.wait_for(self._inner.acquire(), timeout=timeout_sec)
            except asyncio.TimeoutError as exc:
                self._timeout_total += 1
                self._waiting_count -= 1
                raise SemaphoreTimeoutError(
                    f"获取信号量超时 ({timeout_sec}s) · holder={holder_name!r}"
                ) from exc
            except asyncio.CancelledError:
                self._cancelled_total += 1
                self._waiting_count -= 1
                raise
            else:
                self._waiting_count -= 1

            wait_ms = (time.monotonic() - wait_start) * 1000.0
            self._last_acquire_wait_ms = wait_ms
            self._acquired_total += 1
            token = self._track_holder(holder_name)
            logger.debug(
                f"[LLMSemaphore] try_guard acquired · holder={holder_name!r} · "
                f"wait={wait_ms:.1f}ms"
            )

            yield

        finally:
            if token is not None:
                self._release_token(token)

    async def try_acquire(
        self,
        holder_name: str,
        timeout_sec: float,
    ) -> bool:
        """
        非上下文管理器版本 · 仅在确实需要"返回布尔值后异步调度"的场景使用。

        ⚠️ 调用方必须在成功后通过 _release_external_token() 释放,
           因此本方法标记为内部使用。常规业务一律使用 guard / try_guard。
        """
        # 当前实现仅暴露布尔语义,不返回 token —— 强制走 try_guard。
        # 此方法保留作为未来扩展的占位,默认抛 NotImplementedError 防误用。
        raise NotImplementedError(
            "请使用 try_guard(holder, timeout_sec) 上下文管理器,"
            "杜绝 acquire/release 配对错误"
        )

    # ────────────────────────────────────────────────────────────
    # 优雅关闭
    # ────────────────────────────────────────────────────────────

    async def drain(self, timeout_sec: float = 30.0) -> bool:
        """
        切换到关闭模式,阻塞等待所有持有者归还。

        Returns:
            True  - 所有持有者已归还
            False - 超时,仍有持有者 (调用方需强制中断)

        典型用法 (FastAPI lifespan shutdown):
            @asynccontextmanager
            async def lifespan(app):
                yield
                await llm_semaphore.drain(timeout_sec=30)
        """
        if self._closing:
            logger.warning("[LLMSemaphore] drain 已被调用,忽略重复请求")
            return self._held_count == 0

        self._closing = True
        logger.info(
            f"[LLMSemaphore] 进入优雅关闭模式 · "
            f"held={self._held_count} · waiting={self._waiting_count} · "
            f"timeout={timeout_sec}s"
        )

        if self._held_count == 0:
            return True

        try:
            await asyncio.wait_for(
                self._all_released_event.wait(),
                timeout=timeout_sec,
            )
            logger.info("[LLMSemaphore] 所有持有者已归还,drain 完成")
            return True
        except asyncio.TimeoutError:
            stuck = list(self._holders.values())
            stuck_names = [t.holder_name for t in stuck]
            logger.error(
                f"[LLMSemaphore] drain 超时 · 仍有 {len(stuck)} 个持有者: "
                f"{stuck_names}"
            )
            return False

    # ────────────────────────────────────────────────────────────
    # 观测
    # ────────────────────────────────────────────────────────────

    def metrics(self) -> SemaphoreMetrics:
        """导出当前指标快照 (零拷贝,按值返回)。"""
        # 按 sequence_id 排序,保证 holders 顺序与获取顺序一致
        ordered_holders = tuple(
            self._holders[sid].holder_name
            for sid in sorted(self._holders.keys())
        )
        return SemaphoreMetrics(
            capacity=self._capacity,
            held_count=self._held_count,
            waiting_count=self._waiting_count,
            acquired_total=self._acquired_total,
            released_total=self._released_total,
            timeout_total=self._timeout_total,
            cancelled_total=self._cancelled_total,
            last_acquire_wait_ms=self._last_acquire_wait_ms,
            holders=ordered_holders,
        )

    # ────────────────────────────────────────────────────────────
    # 内部: 持有者追踪
    # ────────────────────────────────────────────────────────────

    def _track_holder(self, holder_name: str) -> _SemaphoreToken:
        """登记一个持有者,返回内部 token。"""
        self._sequence_counter += 1
        token = _SemaphoreToken(
            holder_name=holder_name,
            acquired_at_monotonic=time.monotonic(),
            sequence_id=self._sequence_counter,
        )
        self._holders[token.sequence_id] = token
        self._held_count += 1
        # 有持有者了,清空"全部归还"信号
        self._all_released_event.clear()
        return token

    def _release_token(self, token: _SemaphoreToken) -> None:
        """释放一个 token,更新指标,触发 inner.release()。"""
        # 先从持有者表移除 (即使后续 release 异常也保持一致性)
        self._holders.pop(token.sequence_id, None)
        self._held_count = max(0, self._held_count - 1)
        self._released_total += 1

        held_duration_ms = (
            time.monotonic() - token.acquired_at_monotonic
        ) * 1000.0
        logger.debug(
            f"[LLMSemaphore] released · holder={token.holder_name!r} · "
            f"held_for={held_duration_ms:.1f}ms · "
            f"held={self._held_count}/{self._capacity}"
        )

        # 触发底层信号量释放
        # ⚠️ 即使下面这一行抛错,上面的指标也已更新,内核状态保持一致
        try:
            self._inner.release()
        except ValueError:
            # asyncio.Semaphore.release 在内部计数已达 cap 时会抛 ValueError
            # 这是绝对不应发生的双重释放,记录严重日志但不 re-raise (上下文管理器内 finally)
            logger.critical(
                f"[LLMSemaphore] 双重释放检测! holder={token.holder_name!r} · "
                f"这是严重的代码缺陷,请立即审查 guard 路径"
            )

        # 全部归还时唤醒 drain()
        if self._held_count == 0:
            self._all_released_event.set()


# ──────────────────────────────────────────────────────────────────────────────
# 自定义异常
# ──────────────────────────────────────────────────────────────────────────────


class SemaphoreError(Exception):
    """LLMSemaphore 异常基类。"""


class SemaphoreClosingError(SemaphoreError):
    """系统正在优雅关闭,拒绝新获取请求。"""


class SemaphoreTimeoutError(SemaphoreError):
    """try_guard 等待超时。"""


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例 · 双重检查锁定
# ──────────────────────────────────────────────────────────────────────────────


_singleton_instance: Optional[LLMSemaphore] = None
_singleton_init_lock: Optional[asyncio.Lock] = None


def _get_init_lock() -> asyncio.Lock:
    """
    懒加载 asyncio.Lock。

    asyncio.Lock 必须在事件循环就绪后创建; 模块导入期可能尚无 loop。
    """
    global _singleton_init_lock
    if _singleton_init_lock is None:
        _singleton_init_lock = asyncio.Lock()
    return _singleton_init_lock


async def get_llm_semaphore() -> LLMSemaphore:
    """
    获取全局 LLMSemaphore 单例 (异步版本,用于运行期获取)。

    使用模式:
        from backend.core.semaphore import get_llm_semaphore

        sem = await get_llm_semaphore()
        async with sem.guard("INTJ_logician"):
            await ollama_stream(...)
    """
    global _singleton_instance

    # 第一次检查 (无锁快速路径)
    if _singleton_instance is not None:
        return _singleton_instance

    # 加锁后第二次检查
    lock = _get_init_lock()
    async with lock:
        if _singleton_instance is None:
            settings = get_settings()
            _singleton_instance = LLMSemaphore(
                capacity=settings.GLOBAL_LLM_CONCURRENCY
            )
        return _singleton_instance


def get_llm_semaphore_sync() -> LLMSemaphore:
    """
    同步版本 · 仅供 lifespan 启动 / 关闭等已知无并发的场景。

    ⚠️ 严禁在请求处理路径中调用,会绕过初始化锁。
    """
    global _singleton_instance
    if _singleton_instance is None:
        settings = get_settings()
        _singleton_instance = LLMSemaphore(
            capacity=settings.GLOBAL_LLM_CONCURRENCY
        )
    return _singleton_instance


def reset_singleton_for_testing() -> None:
    """
    [仅供测试] 重置全局单例。

    ⚠️ 生产代码严禁调用 —— 会破坏所有持有者的引用。
    """
    global _singleton_instance, _singleton_init_lock
    _singleton_instance = None
    _singleton_init_lock = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "LLMSemaphore",
    "SemaphoreMetrics",
    "SemaphoreError",
    "SemaphoreClosingError",
    "SemaphoreTimeoutError",
    "get_llm_semaphore",
    "get_llm_semaphore_sync",
    "reset_singleton_for_testing",
]


# ──────────────────────────────────────────────────────────────────────────────
# 类型注解辅助 (不影响运行时)
# ──────────────────────────────────────────────────────────────────────────────


_ExcType = Optional[Type[BaseException]]
_ExcVal = Optional[BaseException]
_ExcTb = Optional[TracebackType]