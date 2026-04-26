"""
==============================================================================
backend/memory/chroma_gateway.py
------------------------------------------------------------------------------
ChromaDB 网关 · 单线程写入队列 · 防 SQLite 锁死

工程死局背景:
    ChromaDB 内部基于 SQLite,多线程并发写入会立即触发:
        sqlite3.OperationalError: database is locked
    在 FastAPI 事件循环中,若 ContextBus pub-sub 直接调用 chromadb client.add(),
    8 个 Agent 高频封存 Turn 时必定撞上锁竞争 → 整个 backend 卡死。

解决方案:
    1. Write-Ahead Queue:
       所有写入请求入队 (入队即返回),后台单线程 Worker 串行消费
    2. asyncio.to_thread 读取:
       读操作不持锁,但仍要走线程池,绝不阻塞事件循环
    3. 批量合并:
       高频小写入自动合并为单 batch (上限 32 条 / 200ms 兜底 flush),
       显著降低 SQLite 事务开销
    4. 背压控制:
       队列满 → FIFO 丢弃旧条目 + 严重日志,绝不无界堆积
    5. 优雅降级:
       ChromaDB 短暂不可达时进入 backoff 重试,主流不阻塞;
       最终失败的批次标记为 dropped 而非抛错回 ContextBus

⚠️ 工程铁律:
    - ChromaDB 写入路径绝对单线程 (整个 backend 唯一 writer)
    - 所有读取必须走 asyncio.to_thread (event loop 安全)
    - 写入队列必须有界 + FIFO 丢弃
    - Worker 顶层 supervisor 自动重启,严禁静默死亡
    - lifespan stop 时必须 flush 全部待写,超时兜底强制结束

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from loguru import logger

from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 单批最大写入条数 (与 ChromaDB 推荐 batch size 对齐)
_BATCH_MAX_SIZE: Final[int] = 32

#: 攒批等待时长上限 (秒) · 即使未满 32 条,200ms 后也强制 flush
_BATCH_MAX_WAIT_SEC: Final[float] = 0.2

#: 失败重试 backoff 起始值 (秒)
_RETRY_INITIAL_BACKOFF_SEC: Final[float] = 0.5

#: 失败重试 backoff 上限 (秒)
_RETRY_MAX_BACKOFF_SEC: Final[float] = 5.0

#: 单批连续失败多少次后判定彻底失败 (drop 该批)
_BATCH_MAX_RETRY_ATTEMPTS: Final[int] = 4

#: Worker supervisor 异常重启 backoff
_WORKER_RESTART_BACKOFF_SEC: Final[float] = 1.0

#: 历史环容量
_HISTORY_RING_SIZE: Final[int] = 64


# ──────────────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────────────


class EntryStatus(str, enum.Enum):
    """单条 MemoryEntry 在网关内部的生命周期状态。"""

    QUEUED = "QUEUED"
    """已入队,等待 Worker 消费"""

    WRITTEN = "WRITTEN"
    """已成功写入 Chroma"""

    DROPPED_OVERFLOW = "DROPPED_OVERFLOW"
    """背压丢弃 (队列满)"""

    DROPPED_FAILED = "DROPPED_FAILED"
    """重试耗尽后丢弃"""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """
    单条记忆条目 · 不可变。

    与 core.context_bus.Turn 字段语义对齐,作为网关内部的统一载体。
    """

    entry_id: str
    """网关内部 ID (UUID4) · 与 turn_id 不必相同"""

    turn_id: str
    """关联的 Turn ID (供未来反查用)"""

    agent_id: str
    topic_id: str
    content: str
    """文本主体 (Turn.text 含影子后缀)"""

    metadata: dict[str, Any]
    """ChromaDB 标量元数据 (str/int/float/bool)"""

    created_at_monotonic: float
    created_at_wall_unix: float

    def to_chroma_kwargs(self) -> dict[str, Any]:
        """
        转换为 ChromaDB collection.add(...) 所需的形参字典。

        ChromaDB 0.5.x 协议:
            collection.add(
                ids=[...],
                documents=[...],
                metadatas=[...],
            )
        本方法返回 *单条* 的字典化形态,由批量合并器组装为列表参数。
        """
        # ChromaDB metadata 仅接受标量类型,过滤兜底
        safe_meta: dict[str, Any] = {}
        for k, v in self.metadata.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, (str, int, float, bool)):
                safe_meta[k] = v
            else:
                # 复杂值降级为字符串
                safe_meta[k] = str(v)

        # 必备元数据 (供后续过滤查询)
        safe_meta.setdefault("turn_id", self.turn_id)
        safe_meta.setdefault("agent_id", self.agent_id)
        safe_meta.setdefault("topic_id", self.topic_id)
        safe_meta.setdefault(
            "created_at_wall_unix", round(self.created_at_wall_unix, 6)
        )

        return {
            "id": self.entry_id,
            "document": self.content,
            "metadata": safe_meta,
        }


@dataclass(slots=True)
class GatewayMetrics:
    """网关运行指标 · 透出至 /api/sandbox/snapshot 与 HUD。"""

    is_running: bool
    queue_size: int
    queue_capacity: int
    enqueued_total: int
    written_total: int
    dropped_overflow_total: int
    dropped_failed_total: int
    last_batch_size: int
    last_write_latency_ms: float
    last_error: Optional[str]
    consecutive_write_failures: int

    def to_dict(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "queue_size": self.queue_size,
            "queue_capacity": self.queue_capacity,
            "enqueued_total": self.enqueued_total,
            "written_total": self.written_total,
            "dropped_overflow_total": self.dropped_overflow_total,
            "dropped_failed_total": self.dropped_failed_total,
            "last_batch_size": self.last_batch_size,
            "last_write_latency_ms": round(self.last_write_latency_ms, 3),
            "last_error": self.last_error,
            "consecutive_write_failures": self.consecutive_write_failures,
        }


@dataclass(frozen=True, slots=True)
class QueryHit:
    """查询命中条目 · 供 ISFJ 检索使用。"""

    entry_id: str
    content: str
    metadata: dict[str, Any]
    distance: Optional[float]
    """相似度距离 (越小越相似) · ChromaDB 未必返回"""

    def to_dict(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "content": self.content,
            "metadata": dict(self.metadata),
            "distance": (
                round(self.distance, 6) if self.distance is not None else None
            ),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class ChromaGatewayError(Exception):
    pass


class GatewayNotRunningError(ChromaGatewayError):
    """start 前调用业务方法。"""


class GatewayStoppedError(ChromaGatewayError):
    """stop 后调用业务方法。"""


# ──────────────────────────────────────────────────────────────────────────────
# 主体: ChromaGateway
# ──────────────────────────────────────────────────────────────────────────────


class ChromaGateway:
    """
    ChromaDB 单线程写入网关 (进程级单例)。

    生命周期:
        gateway = get_chroma_gateway()
        await gateway.start()                 # lifespan startup
        gateway.enqueue(...)                  # 业务路径 (即时返回)
        hits = await gateway.query(...)       # 业务路径 (异步线程池)
        await gateway.stop()                  # lifespan shutdown
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        collection_name: str,
        tenant: str,
        database: str,
        write_queue_maxsize: int,
    ) -> None:
        if write_queue_maxsize < 16:
            raise ValueError(
                f"write_queue_maxsize 必须 ≥ 16,当前: {write_queue_maxsize}"
            )

        self._host: Final[str] = host
        self._port: Final[int] = port
        self._collection_name: Final[str] = collection_name
        self._tenant: Final[str] = tenant
        self._database: Final[str] = database
        self._queue_capacity: Final[int] = write_queue_maxsize

        # 写入队列 (deque + 锁 · 比 asyncio.Queue 更易做 LWW/FIFO drop)
        self._queue: deque[MemoryEntry] = deque()
        self._queue_lock: asyncio.Lock = asyncio.Lock()
        self._queue_wakeup: asyncio.Event = asyncio.Event()

        # ChromaDB client / collection (lazy 创建)
        self._client: Optional[Any] = None
        self._collection: Optional[Any] = None

        # Worker
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stop_signal: asyncio.Event = asyncio.Event()
        self._stopped: bool = False
        self._started: bool = False

        # 累计指标
        self._enqueued_total: int = 0
        self._written_total: int = 0
        self._dropped_overflow_total: int = 0
        self._dropped_failed_total: int = 0
        self._last_batch_size: int = 0
        self._last_write_latency_ms: float = 0.0
        self._last_error: Optional[str] = None
        self._consecutive_write_failures: int = 0

        # 历史环 (诊断)
        self._batch_history: deque[dict[str, object]] = deque(
            maxlen=_HISTORY_RING_SIZE
        )

        logger.info(
            f"[ChromaGateway] 初始化 · {host}:{port} · "
            f"collection={collection_name!r} · "
            f"queue_cap={write_queue_maxsize}"
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        """
        启动网关 · 创建 / 获取 collection,启动 Worker。

        ChromaDB 不可达时不抛错,Worker 进入 backoff 重连。
        """
        if self._stopped:
            raise GatewayStoppedError(
                "Gateway 已 stop · 不可重启 (创建新实例)"
            )
        if self._started:
            return

        # 尝试初始化 client + collection (失败不阻塞 start,Worker 会重试)
        try:
            await self._ensure_collection()
            logger.info(
                f"[ChromaGateway] 已连接 · collection={self._collection_name!r}"
            )
        except Exception as exc:
            logger.warning(
                f"[ChromaGateway] 初始连接失败 · 将由 Worker 后台重试: {exc!r}"
            )
            self._last_error = f"initial_connect: {exc!r}"

        self._started = True
        self._stop_signal.clear()
        self._worker_task = asyncio.create_task(
            self._worker_supervisor(),
            name="chroma_write_worker",
        )

    async def stop(self, flush_timeout_sec: float = 10.0) -> None:
        """
        停止网关 · 主动 flush 全部待写 · 超时兜底强制结束。

        Args:
            flush_timeout_sec: 等待 Worker 自然消费完队列的最大秒数
        """
        if self._stopped:
            return
        self._stopped = True

        # 1) 给 Worker 一段时间自然消费完队列
        deadline = time.monotonic() + flush_timeout_sec
        while time.monotonic() < deadline:
            async with self._queue_lock:
                pending = len(self._queue)
            if pending == 0:
                break
            self._queue_wakeup.set()
            await asyncio.sleep(0.1)

        # 2) 通知 Worker 停止
        self._stop_signal.set()
        self._queue_wakeup.set()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        # 3) 报告残留
        async with self._queue_lock:
            remaining = len(self._queue)
            if remaining > 0:
                self._dropped_failed_total += remaining
                self._queue.clear()
                logger.warning(
                    f"[ChromaGateway] stop 时仍有 {remaining} 条未写入,已丢弃"
                )

        # 4) 关闭 client
        # ChromaDB 客户端通常无显式 close,丢弃即可,GC 处理
        self._client = None
        self._collection = None

        logger.info(
            f"[ChromaGateway] 已停止 · "
            f"written={self._written_total} · "
            f"dropped_overflow={self._dropped_overflow_total} · "
            f"dropped_failed={self._dropped_failed_total}"
        )

    @property
    def is_running(self) -> bool:
        return (
            self._started
            and not self._stopped
            and self._worker_task is not None
            and not self._worker_task.done()
        )

    # ════════════════════════════════════════════════════════════
    # 业务接口: 写入 (入队即返回)
    # ════════════════════════════════════════════════════════════

    def enqueue(
        self,
        *,
        turn_id: str,
        agent_id: str,
        topic_id: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> EntryStatus:
        """
        将一条记忆入队 (即时返回,不等待写入)。

        ⚠️ 返回值仅表示"是否成功入队",不代表已落库。

        队列满时 FIFO 丢弃最旧条目 (因为最新发言通常更重要)。

        Returns:
            EntryStatus.QUEUED            - 入队成功
            EntryStatus.DROPPED_OVERFLOW  - 队列满,新条目被丢弃
        """
        if not self._started:
            raise GatewayNotRunningError("Gateway 未 start")
        if self._stopped:
            return EntryStatus.DROPPED_OVERFLOW

        # 基础校验 (轻量,不阻塞)
        if not turn_id or not agent_id or not topic_id:
            logger.warning(
                f"[ChromaGateway] enqueue 缺失必填字段 · "
                f"turn={turn_id!r} agent={agent_id!r} topic={topic_id!r}"
            )
            return EntryStatus.DROPPED_OVERFLOW
        if not isinstance(content, str):
            return EntryStatus.DROPPED_OVERFLOW

        # 超长文本截断 (ChromaDB embedding 通常对 ≤ 8KB 表现最佳)
        if len(content) > 32_768:
            content = content[:32_768] + " [TRUNCATED_BY_GATEWAY]"

        entry = MemoryEntry(
            entry_id=str(uuid.uuid4()),
            turn_id=turn_id,
            agent_id=agent_id,
            topic_id=topic_id,
            content=content,
            metadata=dict(metadata) if metadata else {},
            created_at_monotonic=time.monotonic(),
            created_at_wall_unix=time.time(),
        )

        # 队列写入 (非阻塞,快路径)
        # 注意: 这里访问 _queue 不加 asyncio.Lock,因为我们运行在单事件循环中,
        # deque 的 append/popleft 本身在 CPython 是原子的;asyncio.Lock 仅在
        # async 路径中保护跨 await 的临界区。enqueue 是同步方法,不会让出循环。
        if len(self._queue) >= self._queue_capacity:
            # FIFO 丢弃 (最旧的)
            try:
                discarded = self._queue.popleft()
                self._dropped_overflow_total += 1
                logger.warning(
                    f"[ChromaGateway] 队列满 ({self._queue_capacity}) · "
                    f"FIFO 丢弃最旧条目 turn_id={discarded.turn_id!r}"
                )
            except IndexError:
                pass

        self._queue.append(entry)
        self._enqueued_total += 1
        # 唤醒 Worker (Event.set 是线程安全的快路径)
        self._queue_wakeup.set()

        return EntryStatus.QUEUED

    # ════════════════════════════════════════════════════════════
    # 业务接口: 读取 (异步线程池)
    # ════════════════════════════════════════════════════════════

    async def query(
        self,
        query_text: str,
        *,
        n_results: int = 5,
        topic_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> list[QueryHit]:
        """
        相似度检索 · 仅 ISFJ_archivist 调用。

        通过 asyncio.to_thread 走线程池,绝不阻塞事件循环。

        Args:
            query_text: 查询文本
            n_results: 返回 top-N
            topic_id / agent_id: 元数据过滤 (None = 不过滤)

        Returns:
            list[QueryHit] · 按相似度递增排序

        Raises:
            ChromaGatewayError: ChromaDB 不可达 / 集合未就绪
        """
        if not self.is_running:
            raise GatewayNotRunningError("Gateway 未运行")
        if not query_text or not isinstance(query_text, str):
            return []
        if n_results < 1:
            return []
        n_results = min(n_results, 50)  # 硬上限,防滥用

        # 构造 metadata 过滤 (ChromaDB where 子句)
        where: dict[str, Any] = {}
        if topic_id:
            where["topic_id"] = topic_id
        if agent_id:
            where["agent_id"] = agent_id

        try:
            return await asyncio.to_thread(
                self._sync_query,
                query_text,
                n_results,
                where if where else None,
            )
        except Exception as exc:
            self._last_error = f"query: {exc!r}"
            logger.warning(
                f"[ChromaGateway] query 失败 · {exc!r} (返回空列表降级)"
            )
            return []

    async def count_records(self) -> int:
        """统计当前 collection 总条目数 · 供 HUD 显示。"""
        if not self.is_running:
            return 0
        try:
            return await asyncio.to_thread(self._sync_count)
        except Exception as exc:
            logger.debug(f"[ChromaGateway] count_records 失败: {exc!r}")
            return 0

    # ════════════════════════════════════════════════════════════
    # 内部: 同步 ChromaDB 操作 (在线程池中执行)
    # ════════════════════════════════════════════════════════════

    def _sync_query(
        self,
        query_text: str,
        n_results: int,
        where: Optional[dict[str, Any]],
    ) -> list[QueryHit]:
        """⚠️ 同步方法 · 仅在 asyncio.to_thread 中调用。"""
        if self._collection is None:
            raise ChromaGatewayError("collection 尚未初始化")

        kwargs: dict[str, Any] = {
            "query_texts": [query_text],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        result = self._collection.query(**kwargs)

        # ChromaDB 返回结构: {ids: [[...]], documents: [[...]], ...}
        # 单查询时取 [0]
        hits: list[QueryHit] = []
        ids = (result.get("ids") or [[]])[0]
        docs = (result.get("documents") or [[]])[0]
        metas = (result.get("metadatas") or [[]])[0]
        dists = (result.get("distances") or [[]])[0]

        for i, eid in enumerate(ids):
            if not eid:
                continue
            content = docs[i] if i < len(docs) else ""
            meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
            dist = dists[i] if i < len(dists) else None
            hits.append(
                QueryHit(
                    entry_id=str(eid),
                    content=str(content),
                    metadata=dict(meta),
                    distance=(
                        float(dist)
                        if dist is not None and isinstance(dist, (int, float))
                        else None
                    ),
                )
            )
        return hits

    def _sync_count(self) -> int:
        """⚠️ 同步方法 · 仅在 asyncio.to_thread 中调用。"""
        if self._collection is None:
            return 0
        try:
            count = self._collection.count()
            return int(count) if count is not None else 0
        except Exception:
            return 0

    def _sync_batch_write(self, batch: list[MemoryEntry]) -> None:
        """⚠️ 同步方法 · 仅在 asyncio.to_thread 中调用,且仅 Worker 单线程调用。"""
        if not batch:
            return
        if self._collection is None:
            raise ChromaGatewayError("collection 未就绪")

        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for entry in batch:
            ck = entry.to_chroma_kwargs()
            ids.append(ck["id"])
            documents.append(ck["document"])
            metadatas.append(ck["metadata"])

        # ChromaDB add: 主键冲突时抛错,使用 upsert 更安全
        # 0.5.x 支持 collection.upsert
        if hasattr(self._collection, "upsert"):
            self._collection.upsert(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
        else:
            self._collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )

    # ════════════════════════════════════════════════════════════
    # 内部: ChromaDB client / collection 初始化
    # ════════════════════════════════════════════════════════════

    async def _ensure_collection(self) -> None:
        """
        确保 client + collection 就绪。

        在 to_thread 中执行,因为 chromadb_client 同步且可能慢启动。
        """
        if self._collection is not None:
            return
        await asyncio.to_thread(self._sync_ensure_collection)

    def _sync_ensure_collection(self) -> None:
        """⚠️ 同步路径 · 仅在 asyncio.to_thread 中调用。"""
        if self._collection is not None:
            return

        # 延迟导入 chromadb 客户端,避免模块加载时强依赖
        try:
            import chromadb  # type: ignore
        except ImportError as exc:
            raise ChromaGatewayError(
                "chromadb-client 未安装,请检查 requirements.txt"
            ) from exc

        # ChromaDB HttpClient (与容器化 Chroma server 通信)
        if self._client is None:
            self._client = chromadb.HttpClient(
                host=self._host,
                port=self._port,
                tenant=self._tenant,
                database=self._database,
            )

        # 兼容多版本: get_or_create_collection 是稳定 API
        self._collection = self._client.get_or_create_collection(
            name=self._collection_name,
            metadata={
                "hnsw:space": "cosine",
                "purpose": "mbti_chaos_debate_long_memory",
            },
        )

    # ════════════════════════════════════════════════════════════
    # 内部: 后台 Worker (单线程串行写入)
    # ════════════════════════════════════════════════════════════

    async def _worker_supervisor(self) -> None:
        """Worker 顶层守护 · 异常自动重启,严禁静默死亡。"""
        while not self._stop_signal.is_set():
            try:
                await self._worker_loop()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    f"[ChromaGateway] worker_loop 异常 · {exc!r} · "
                    f"{_WORKER_RESTART_BACKOFF_SEC}s 后重启"
                )
                self._last_error = f"worker_loop: {exc!r}"
                try:
                    await asyncio.sleep(_WORKER_RESTART_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return

    async def _worker_loop(self) -> None:
        """
        主 Worker 循环 · 攒批 + 写入。

        逻辑:
            1. 等待队列唤醒或 stop_signal
            2. 攒批 (上限 BATCH_MAX_SIZE 或等待 BATCH_MAX_WAIT_SEC)
            3. 调用 _sync_batch_write (在 to_thread 中)
            4. 失败重试 backoff,超过 MAX_RETRY_ATTEMPTS 则丢弃整批
        """
        logger.info("[ChromaGateway] worker_loop 进入主循环")

        while not self._stop_signal.is_set():
            # 1) 等待有数据 (或 stop)
            if not self._queue:
                self._queue_wakeup.clear()
                try:
                    await asyncio.wait_for(
                        self._queue_wakeup.wait(),
                        timeout=1.0,  # 兜底,允许检查 stop_signal
                    )
                except asyncio.TimeoutError:
                    continue
                if self._stop_signal.is_set():
                    return

            # 2) 攒批
            batch = self._collect_batch()
            if not batch:
                continue

            # 3) 确保 collection 就绪 (失败则丢弃整批,避免锁死)
            try:
                await self._ensure_collection()
            except Exception as exc:
                logger.error(
                    f"[ChromaGateway] collection 初始化失败 · "
                    f"丢弃批次 ({len(batch)} 条): {exc!r}"
                )
                self._last_error = f"ensure_collection: {exc!r}"
                self._dropped_failed_total += len(batch)
                # backoff 后继续 (避免热循环)
                await self._sleep_with_stop_check(_RETRY_INITIAL_BACKOFF_SEC)
                continue

            # 4) 写入 (重试)
            await self._write_batch_with_retry(batch)

    def _collect_batch(self) -> list[MemoryEntry]:
        """
        从队列取出最多 BATCH_MAX_SIZE 条。

        ⚠️ 同步方法 · 假定运行在事件循环主协程中,deque 操作原子。
        """
        batch: list[MemoryEntry] = []
        while self._queue and len(batch) < _BATCH_MAX_SIZE:
            try:
                batch.append(self._queue.popleft())
            except IndexError:
                break
        return batch

    async def _write_batch_with_retry(self, batch: list[MemoryEntry]) -> None:
        """对一批数据执行带退避的写入。"""
        attempts = 0
        backoff = _RETRY_INITIAL_BACKOFF_SEC

        while attempts < _BATCH_MAX_RETRY_ATTEMPTS:
            attempts += 1
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self._sync_batch_write, batch)
                # 成功
                latency_ms = (time.monotonic() - t0) * 1000.0
                self._written_total += len(batch)
                self._last_batch_size = len(batch)
                self._last_write_latency_ms = latency_ms
                self._consecutive_write_failures = 0
                self._batch_history.append(
                    {
                        "size": len(batch),
                        "latency_ms": round(latency_ms, 3),
                        "at_wall_unix": round(time.time(), 6),
                        "status": "ok",
                    }
                )
                logger.debug(
                    f"[ChromaGateway] batch 写入成功 · "
                    f"size={len(batch)} · latency={latency_ms:.1f}ms"
                )
                return

            except asyncio.CancelledError:
                # stop 路径,把整批退回队首 (尽可能不丢)
                # 注: 这里把 batch 反向 appendleft 以保持时间序
                for entry in reversed(batch):
                    self._queue.appendleft(entry)
                raise

            except Exception as exc:
                self._consecutive_write_failures += 1
                self._last_error = f"batch_write: {exc!r}"
                logger.warning(
                    f"[ChromaGateway] batch 写入失败 (attempt={attempts}/"
                    f"{_BATCH_MAX_RETRY_ATTEMPTS}) · "
                    f"size={len(batch)} · err={exc!r}"
                )

                if attempts >= _BATCH_MAX_RETRY_ATTEMPTS:
                    self._dropped_failed_total += len(batch)
                    self._batch_history.append(
                        {
                            "size": len(batch),
                            "latency_ms": 0.0,
                            "at_wall_unix": round(time.time(), 6),
                            "status": "dropped_failed",
                            "error": str(exc)[:128],
                        }
                    )
                    logger.error(
                        f"[ChromaGateway] batch 重试耗尽,丢弃 · "
                        f"size={len(batch)} · last_err={exc!r}"
                    )
                    return

                await self._sleep_with_stop_check(backoff)
                backoff = min(backoff * 2.0, _RETRY_MAX_BACKOFF_SEC)

    async def _sleep_with_stop_check(self, sec: float) -> None:
        """带 stop_signal 提早唤醒的 sleep。"""
        try:
            await asyncio.wait_for(self._stop_signal.wait(), timeout=sec)
            # stop_signal set, 提前返回
        except asyncio.TimeoutError:
            return

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> GatewayMetrics:
        return GatewayMetrics(
            is_running=self.is_running,
            queue_size=len(self._queue),
            queue_capacity=self._queue_capacity,
            enqueued_total=self._enqueued_total,
            written_total=self._written_total,
            dropped_overflow_total=self._dropped_overflow_total,
            dropped_failed_total=self._dropped_failed_total,
            last_batch_size=self._last_batch_size,
            last_write_latency_ms=self._last_write_latency_ms,
            last_error=self._last_error,
            consecutive_write_failures=self._consecutive_write_failures,
        )

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "recent_batches": list(self._batch_history)[-16:],
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_gateway_singleton: Optional[ChromaGateway] = None


def get_chroma_gateway() -> ChromaGateway:
    """
    获取全局 ChromaGateway 单例。

    使用模式 (lifespan):
        @asynccontextmanager
        async def lifespan(app):
            gateway = get_chroma_gateway()
            await gateway.start()
            try:
                yield
            finally:
                await gateway.stop()
    """
    global _gateway_singleton
    if _gateway_singleton is None:
        s = get_settings()
        _gateway_singleton = ChromaGateway(
            host=s.CHROMA_HOST,
            port=s.CHROMA_PORT,
            collection_name=s.CHROMA_COLLECTION_NAME,
            tenant=s.CHROMA_TENANT,
            database=s.CHROMA_DATABASE,
            write_queue_maxsize=s.CHROMA_WRITE_QUEUE_MAXSIZE,
        )
    return _gateway_singleton


def reset_gateway_for_testing() -> None:
    """[仅供测试] 重置全局单例 (不调 stop)。"""
    global _gateway_singleton
    _gateway_singleton = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 主体
    "ChromaGateway",
    # 数据类
    "MemoryEntry",
    "QueryHit",
    "GatewayMetrics",
    "EntryStatus",
    # 异常
    "ChromaGatewayError",
    "GatewayNotRunningError",
    "GatewayStoppedError",
    # 单例
    "get_chroma_gateway",
    "reset_gateway_for_testing",
]