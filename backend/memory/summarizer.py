"""
==============================================================================
backend/memory/summarizer.py
------------------------------------------------------------------------------
后台低优先级 Summarizer · 算力空闲压缩 · 数据完整性双轨制

定位:
    将 ContextBus 的溢出 Turn 周期性地压缩为单条摘要 MemoryEntry,
    降低 ChromaDB 条目膨胀、提升语义检索质量。

设计原则 (核心铁律):
    1. 严禁与主推理抢算力 -
       仅当 LLMSemaphore.held_count == 0 且 Arbitrator 空闲时才尝试压缩
       即使尝试也走 try_guard(timeout=0.3),主推理一来立即放弃
    2. 数据完整性双轨制 -
       原始溢出 Turn 通过 sliding_window.bridge_overflow_to_chroma
       *无条件* 优先入库 (即使摘要失败) → Summarizer 死亡不丢数据
       摘要作为 *额外* 条目,带 metadata.kind="summary" 标识
    3. 完全可选 -
       Ollama 不可达 / 模型未加载 / 任意路径失败,均不影响主流;
       仅记录 ollama_failures 指标,继续轮询
    4. 算力守门双闸 -
       (a) 启动条件: held_count==0 && arbitrator idle
       (b) 拿锁条件: try_guard(timeout=0.3),抢不到立刻让位
    5. Worker 顶层 supervisor -
       任何路径异常自动重启,严禁静默死亡

⚠️ 工程铁律:
    - 摘要 metadata 必须 kind="summary",否则 ISFJ 检索时混合污染
    - 失败时严禁删除原始 Turn (双轨制原则)
    - 死循环防护: overflow 空时进入长 sleep,严禁热循环

==============================================================================
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Final, Optional

import httpx
import orjson
from loguru import logger

from backend.core.arbitrator import Arbitrator
from backend.core.config import Settings, get_settings
from backend.core.context_bus import (
    ContextBus,
    Turn,
    get_context_bus,
)
from backend.core.semaphore import (
    LLMSemaphore,
    SemaphoreClosingError,
    SemaphoreTimeoutError,
)
from backend.memory.chroma_gateway import (
    ChromaGateway,
    EntryStatus,
    get_chroma_gateway,
)
from backend.memory.sliding_window import (
    BridgeReport,
    bridge_overflow_to_chroma,
)


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 触发摘要压缩的最少累积 Turn 数 (低于此值仅做原始桥接)
_SUMMARY_TRIGGER_MIN_TURNS: Final[int] = 6

#: 单次摘要批次的最大 Turn 数 (与 sliding_window.bridge max_items 对齐)
_SUMMARY_BATCH_MAX_TURNS: Final[int] = 16

#: 算力守门: try_guard 拿信号量的超时 (秒) · 必须极短,主推理一来立刻让位
_SEMAPHORE_TRY_TIMEOUT_SEC: Final[float] = 0.3

#: 主循环常规 tick 间隔 (秒) · overflow 空闲时使用
_IDLE_TICK_INTERVAL_SEC: Final[float] = 5.0

#: overflow 有数据但算力忙时的退避 tick 间隔 (秒)
_BUSY_BACKOFF_TICK_SEC: Final[float] = 2.0

#: Worker supervisor 异常重启 backoff
_WORKER_RESTART_BACKOFF_SEC: Final[float] = 1.5

#: Ollama 单次摘要请求超时 (秒) · 严格控制,避免拖慢主推理空隙
_SUMMARY_OLLAMA_TIMEOUT_SEC: Final[float] = 30.0

#: 摘要 Prompt 输入文本字符上限 (硬上限,超出强制截断)
_SUMMARY_INPUT_CHAR_HARD_LIMIT: Final[int] = 12_000

#: 摘要输出最大 token 数
_SUMMARY_NUM_PREDICT: Final[int] = 320

#: 历史环容量 (诊断用)
_HISTORY_RING_SIZE: Final[int] = 32

#: 摘要 metadata 中标识条目类型的 kind 值
SUMMARY_METADATA_KIND: Final[str] = "summary"

#: 单 Turn 在原始数据中的 metadata.kind (供 ISFJ 检索过滤)
RAW_METADATA_KIND: Final[str] = "raw_turn"

#: Summarizer 发给 Ollama 的 system prompt 模板
_SUMMARY_SYSTEM_PROMPT: Final[str] = (
    "你是一名严谨的辩论会议记录员。"
    "请基于下方按时间顺序排列的发言片段,生成一段不超过 200 字的中文摘要,"
    "客观提炼核心观点、分歧点与情绪走向,禁止虚构未出现的内容,"
    "禁止评判任何一方,禁止使用第一人称。"
    "若发言被强制截断,你应在摘要中保留其残留语义但不要描述截断本身。"
)


# ──────────────────────────────────────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SummarizerMetrics:
    """Summarizer 运行指标 · 透出 /api/sandbox/snapshot 与 HUD。"""

    is_running: bool
    cycles_total: int
    """主循环 tick 累计次数"""

    summaries_made_total: int
    """成功生成的摘要条目数"""

    summary_attempts_total: int
    """已尝试摘要的批次数 (含失败)"""

    busy_skips_total: int
    """因算力忙跳过摘要 (但已桥接原始) 的次数"""

    ollama_failures_total: int
    """Ollama 调用失败次数 (不含 try_guard 让位)"""

    last_bridge: Optional[dict[str, int]]
    """最近一次 bridge_overflow_to_chroma 报告"""

    last_summary_preview: Optional[str]
    """最近一次摘要内容前 80 字预览 (诊断用)"""

    last_error: Optional[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "is_running": self.is_running,
            "cycles_total": self.cycles_total,
            "summaries_made_total": self.summaries_made_total,
            "summary_attempts_total": self.summary_attempts_total,
            "busy_skips_total": self.busy_skips_total,
            "ollama_failures_total": self.ollama_failures_total,
            "last_bridge": dict(self.last_bridge) if self.last_bridge else None,
            "last_summary_preview": self.last_summary_preview,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class _SummaryRecord:
    """单条摘要的诊断记录 (历史环成员)。"""

    summary_id: str
    source_turn_count: int
    source_topic_ids: tuple[str, ...]
    char_count: int
    at_wall_unix: float

    def to_dict(self) -> dict[str, object]:
        return {
            "summary_id": self.summary_id,
            "source_turn_count": self.source_turn_count,
            "source_topic_ids": list(self.source_topic_ids),
            "char_count": self.char_count,
            "at_wall_unix": round(self.at_wall_unix, 6),
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class SummarizerError(Exception):
    pass


class SummarizerStoppedError(SummarizerError):
    pass


# ──────────────────────────────────────────────────────────────────────────────
# 主体: BackgroundSummarizer
# ──────────────────────────────────────────────────────────────────────────────


class BackgroundSummarizer:
    """
    后台低优先级 Summarizer Worker (进程级单例)。

    生命周期:
        s = get_summarizer()
        await s.start()            # lifespan startup
        ...
        await s.stop()             # lifespan shutdown
    """

    def __init__(
        self,
        *,
        settings: Settings,
        context_bus: ContextBus,
        chroma_gateway: ChromaGateway,
        semaphore: LLMSemaphore,
        arbitrator: Arbitrator,
    ) -> None:
        self._settings: Final[Settings] = settings
        self._bus: Final[ContextBus] = context_bus
        self._gateway: Final[ChromaGateway] = chroma_gateway
        self._semaphore: Final[LLMSemaphore] = semaphore
        self._arbitrator: Final[Arbitrator] = arbitrator

        self._stop_signal: asyncio.Event = asyncio.Event()
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._stopped: bool = False
        self._started: bool = False

        # 累计指标
        self._cycles_total: int = 0
        self._summaries_made_total: int = 0
        self._summary_attempts_total: int = 0
        self._busy_skips_total: int = 0
        self._ollama_failures_total: int = 0
        self._last_bridge: Optional[BridgeReport] = None
        self._last_summary_preview: Optional[str] = None
        self._last_error: Optional[str] = None

        # 历史环
        self._history: deque[_SummaryRecord] = deque(
            maxlen=_HISTORY_RING_SIZE
        )

        # 内置 httpx 客户端 (与 LLMRouter 解耦,Summarizer 失败不影响主推理)
        self._http_client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=settings.OLLAMA_HOST,
            timeout=httpx.Timeout(
                connect=10.0,
                read=_SUMMARY_OLLAMA_TIMEOUT_SEC,
                write=15.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_connections=2,
                max_keepalive_connections=1,
                keepalive_expiry=60.0,
            ),
            http2=False,
        )

        logger.info(
            f"[Summarizer] 初始化 · model={settings.OLLAMA_MODEL_SUMMARIZER!r} · "
            f"trigger_min_turns={_SUMMARY_TRIGGER_MIN_TURNS} · "
            f"batch_max_turns={_SUMMARY_BATCH_MAX_TURNS}"
        )

    # ════════════════════════════════════════════════════════════
    # 生命周期
    # ════════════════════════════════════════════════════════════

    async def start(self) -> None:
        if self._stopped:
            raise SummarizerStoppedError(
                "Summarizer 已 stop · 不可重启 (创建新实例)"
            )
        if self._started:
            return
        self._started = True
        self._stop_signal.clear()
        self._worker_task = asyncio.create_task(
            self._supervisor(),
            name="summarizer_worker",
        )
        logger.info("[Summarizer] 后台 Worker 已启动")

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_signal.set()

        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
            self._worker_task = None

        # 关闭 http client (失败路径不抛)
        try:
            await self._http_client.aclose()
        except Exception as exc:
            logger.debug(f"[Summarizer] http client aclose 异常: {exc!r}")

        logger.info(
            f"[Summarizer] 已停止 · "
            f"cycles={self._cycles_total} · "
            f"summaries={self._summaries_made_total} · "
            f"busy_skips={self._busy_skips_total} · "
            f"ollama_failures={self._ollama_failures_total}"
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
    # Supervisor + 主循环
    # ════════════════════════════════════════════════════════════

    async def _supervisor(self) -> None:
        """异常自动重启的顶层守护。"""
        while not self._stop_signal.is_set():
            try:
                await self._main_loop()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error(
                    f"[Summarizer] main_loop 异常 · {exc!r} · "
                    f"{_WORKER_RESTART_BACKOFF_SEC}s 后重启"
                )
                self._last_error = f"main_loop: {exc!r}"
                try:
                    await asyncio.sleep(_WORKER_RESTART_BACKOFF_SEC)
                except asyncio.CancelledError:
                    return

    async def _main_loop(self) -> None:
        """
        主循环:
            1. 等下一 tick (idle 5s / busy 2s)
            2. 检查 ContextBus 是否有溢出
            3. 无溢出 → 长 sleep
            4. 有溢出 → 桥接原始 Turn 到 Chroma (无条件)
            5. 桥接条目 ≥ trigger_min_turns 且算力空闲 → 摘要尝试
            6. 算力忙 → 计入 busy_skips,继续轮询
        """
        logger.info("[Summarizer] main_loop 进入主循环")

        while not self._stop_signal.is_set():
            self._cycles_total += 1

            try:
                await self._tick_once()
            except Exception as exc:
                # tick_once 内已捕获大部分异常;此处兜底
                logger.error(
                    f"[Summarizer] tick_once 异常: {exc!r}"
                )
                self._last_error = f"tick_once: {exc!r}"

            # 决定下一 tick 间隔
            sleep_sec = self._compute_next_sleep()
            try:
                await asyncio.wait_for(
                    self._stop_signal.wait(),
                    timeout=sleep_sec,
                )
                return  # stop_signal 触发,退出
            except asyncio.TimeoutError:
                continue

    def _compute_next_sleep(self) -> float:
        """根据当前队列状态决定下次 tick 间隔。"""
        overflow_size = self._bus.overflow_queue_size()
        if overflow_size <= 0:
            return _IDLE_TICK_INTERVAL_SEC
        # 有溢出但算力可能忙 → 较短退避
        if self._semaphore.held_count > 0:
            return _BUSY_BACKOFF_TICK_SEC
        return max(0.5, _BUSY_BACKOFF_TICK_SEC / 2)

    # ════════════════════════════════════════════════════════════
    # 单 Tick: 桥接 + (可选) 摘要
    # ════════════════════════════════════════════════════════════

    async def _tick_once(self) -> None:
        # 0) 无溢出快速返回
        if not self._bus.has_overflow():
            return

        # 1) 优先桥接原始 Turn 到 Chroma (双轨制铁律)
        # 注: ContextBus.pop_overflow 会从队列移除条目,因此后续摘要必须基于
        # 我们手中已 pop 出的 turns,而不是再次 pop。
        # 为了"摘要+原始"两套都入库,我们这里先 pop 一批 turns,然后手工分发:
        #   (a) 原始: 通过 gateway.enqueue 直接入 Chroma (kind=raw_turn)
        #   (b) 摘要: 仅在算力空闲 + 数量足够时,基于同一批 turns 调摘要 LLM
        turns = self._pop_overflow_batch()
        if not turns:
            return

        # 1a) 原始批量入库 (无论摘要是否成功)
        bridge_report = self._enqueue_raw_turns(turns)
        self._last_bridge = bridge_report

        # 2) 评估是否进入摘要路径
        if len(turns) < _SUMMARY_TRIGGER_MIN_TURNS:
            logger.debug(
                f"[Summarizer] 批次 {len(turns)} < trigger "
                f"{_SUMMARY_TRIGGER_MIN_TURNS},跳过摘要"
            )
            return

        # 3) 算力守门 (启动条件)
        if not self._is_compute_idle():
            self._busy_skips_total += 1
            logger.debug(
                f"[Summarizer] 算力忙,跳过摘要 · "
                f"held={self._semaphore.held_count} · "
                f"holder={self._arbitrator.current_holder() is not None}"
            )
            return

        # 4) 尝试摘要 (内部还会做第二闸 try_guard)
        self._summary_attempts_total += 1
        try:
            await self._maybe_summarize(turns)
        except Exception as exc:
            self._ollama_failures_total += 1
            self._last_error = f"summarize: {exc!r}"
            logger.warning(
                f"[Summarizer] 摘要异常 (原始已入库,无数据丢失) · {exc!r}"
            )

    def _pop_overflow_batch(self) -> list[Turn]:
        """从 ContextBus 一次取出最多 N 条溢出 Turn。"""
        try:
            return self._bus.pop_overflow(max_items=_SUMMARY_BATCH_MAX_TURNS)
        except Exception as exc:
            logger.error(
                f"[Summarizer] pop_overflow 异常 (跳过本 tick): {exc!r}"
            )
            return []

    def _enqueue_raw_turns(self, turns: list[Turn]) -> BridgeReport:
        """
        把原始 Turn 直接入 ChromaGateway 队列。

        注意: 不调用 sliding_window.bridge_overflow_to_chroma,因为那个函数会
        再次 pop_overflow,而我们已经手工 pop 出了 turns。
        """
        inspected = len(turns)
        enqueued = 0
        skipped_empty = 0
        dropped_overflow = 0

        for turn in turns:
            text = (turn.text or "").strip()
            if not text:
                skipped_empty += 1
                continue
            metadata: dict[str, object] = {
                "kind": RAW_METADATA_KIND,
                "turn_id": turn.turn_id,
                "agent_id": turn.agent_id,
                "topic_id": turn.topic_id,
                "truncation": turn.truncation.value,
                "is_truncated": turn.is_truncated(),
                "duration_seconds": round(turn.duration_seconds(), 4),
                "sealed_at_wall_unix": round(turn.sealed_at_wall_unix, 6),
            }
            if turn.interrupter_agent_id:
                metadata["interrupter_agent_id"] = turn.interrupter_agent_id
            for k in ("prefix_target", "token_count"):
                v = turn.metadata.get(k)
                if isinstance(v, str) and v:
                    metadata[k] = v

            try:
                status = self._gateway.enqueue(
                    turn_id=turn.turn_id,
                    agent_id=turn.agent_id,
                    topic_id=turn.topic_id,
                    content=turn.text,
                    metadata=metadata,
                )
            except Exception as exc:
                # gateway 未运行 → 数据丢失,但不影响 Summarizer 主流
                logger.warning(
                    f"[Summarizer] 原始入库异常 (该条丢失) · "
                    f"turn={turn.turn_id!r} · {exc!r}"
                )
                dropped_overflow += 1
                continue

            if status == EntryStatus.QUEUED:
                enqueued += 1
            else:
                dropped_overflow += 1

        if inspected > 0:
            logger.debug(
                f"[Summarizer] raw bridge · "
                f"inspected={inspected} · enqueued={enqueued} · "
                f"skipped_empty={skipped_empty} · dropped={dropped_overflow}"
            )

        return BridgeReport(
            inspected=inspected,
            enqueued=enqueued,
            skipped_empty=skipped_empty,
            dropped_overflow=dropped_overflow,
        )

    def _is_compute_idle(self) -> bool:
        """
        算力空闲 = 信号量无持有者 + 仲裁器无持麦 Agent。
        """
        if self._semaphore.held_count > 0:
            return False
        if self._semaphore.is_closing:
            return False
        if self._arbitrator.current_holder() is not None:
            return False
        return True

    # ════════════════════════════════════════════════════════════
    # 摘要核心
    # ════════════════════════════════════════════════════════════

    async def _maybe_summarize(self, turns: list[Turn]) -> None:
        """
        在算力空闲且第二道 try_guard 抢到信号量时,执行一次摘要 LLM 调用。

        失败路径 (任意 step): 不抛上层,仅记录 ollama_failures。
        """
        # 第二闸: try_guard 拿信号量 (300ms 超时,主推理一来立即放弃)
        try:
            async with self._semaphore.try_guard(
                holder_name="summarizer_worker",
                timeout_sec=_SEMAPHORE_TRY_TIMEOUT_SEC,
            ):
                # 二次检查 (acquire 期间 Arbitrator 状态可能改变)
                if self._arbitrator.current_holder() is not None:
                    logger.debug(
                        "[Summarizer] 拿到信号量但仲裁器已被占用 · 让位"
                    )
                    self._busy_skips_total += 1
                    return

                summary_text = await self._call_ollama_summarize(turns)
                if not summary_text:
                    return  # 已记录失败指标

                self._enqueue_summary(turns=turns, summary_text=summary_text)

        except SemaphoreTimeoutError:
            self._busy_skips_total += 1
            logger.debug(
                "[Summarizer] try_guard 超时 (主推理在用),让位"
            )
            return
        except SemaphoreClosingError:
            logger.debug("[Summarizer] 信号量正在关闭,放弃摘要")
            return

    async def _call_ollama_summarize(
        self,
        turns: list[Turn],
    ) -> Optional[str]:
        """
        调用 Ollama 生成摘要 (非流式,一次性返回)。

        Returns:
            摘要文本 / None (失败)
        """
        if not turns:
            return None

        user_text = self._build_summary_input(turns)
        if not user_text:
            return None

        payload = {
            "model": self._settings.OLLAMA_MODEL_SUMMARIZER,
            "messages": [
                {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            "options": {
                "temperature": 0.3,
                "top_p": 0.9,
                "num_predict": _SUMMARY_NUM_PREDICT,
            },
            "keep_alive": "5m",
        }

        try:
            response = await self._http_client.post(
                "/api/chat",
                content=orjson.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            self._ollama_failures_total += 1
            self._last_error = f"http: {exc!r}"
            logger.warning(f"[Summarizer] Ollama HTTP 错误: {exc!r}")
            return None

        if response.status_code != 200:
            self._ollama_failures_total += 1
            preview = response.text[:256] if response.text else ""
            self._last_error = (
                f"http_status_{response.status_code}: {preview}"
            )
            logger.warning(
                f"[Summarizer] Ollama 状态码 {response.status_code} · "
                f"body={preview!r}"
            )
            return None

        try:
            obj = response.json()
        except (orjson.JSONDecodeError, ValueError) as exc:
            self._ollama_failures_total += 1
            self._last_error = f"json: {exc!r}"
            logger.warning(f"[Summarizer] 响应 JSON 解析失败: {exc!r}")
            return None

        # Ollama /api/chat 非流式响应:
        # { "message": {"role":"assistant","content":"..."}, "done": true, ... }
        if not isinstance(obj, dict):
            self._ollama_failures_total += 1
            return None
        msg = obj.get("message")
        if not isinstance(msg, dict):
            self._ollama_failures_total += 1
            return None
        content = msg.get("content")
        if not isinstance(content, str):
            self._ollama_failures_total += 1
            return None

        cleaned = content.strip()
        if not cleaned:
            self._ollama_failures_total += 1
            return None
        return cleaned

    def _build_summary_input(self, turns: list[Turn]) -> str:
        """
        把 Turn 列表渲染为单段输入文本 (供 user role 喂入)。

        硬上限 _SUMMARY_INPUT_CHAR_HARD_LIMIT,超出从最旧那条开始裁。
        """
        lines: list[str] = []
        total = 0
        # 按时间顺序 (旧→新),保留语义连贯
        for turn in turns:
            text = (turn.text or "").strip()
            if not text:
                continue
            line = f"[{turn.agent_id}] {text}"
            if total + len(line) > _SUMMARY_INPUT_CHAR_HARD_LIMIT:
                # 中断累加,后续条目丢弃 (摘要至少有最旧的部分)
                # 这里宁可少一些数据,也不让 prompt 撑爆
                logger.debug(
                    f"[Summarizer] 输入达到硬上限 "
                    f"{_SUMMARY_INPUT_CHAR_HARD_LIMIT} 字符,"
                    f"丢弃后续 {len(turns) - len(lines)} 条"
                )
                break
            lines.append(line)
            total += len(line)

        if not lines:
            return ""

        return "下面是辩论会的若干轮发言记录,按时间顺序排列:\n\n" + "\n\n".join(
            lines
        )

    # ════════════════════════════════════════════════════════════
    # 摘要入库
    # ════════════════════════════════════════════════════════════

    def _enqueue_summary(
        self,
        *,
        turns: list[Turn],
        summary_text: str,
    ) -> None:
        """把摘要作为 *额外* 条目入 ChromaGateway。"""
        if not summary_text:
            return

        summary_id = f"sum_{uuid.uuid4().hex[:16]}"
        # 摘要的"代表性 turn_id"使用本身,但 metadata 含 source_turn_ids
        source_turn_ids = ",".join(t.turn_id for t in turns)
        source_topic_ids = sorted({t.topic_id for t in turns})
        agents_covered = sorted({t.agent_id for t in turns})
        primary_topic_id = (
            turns[-1].topic_id if turns else "unknown"  # 最新议题
        )

        metadata: dict[str, object] = {
            "kind": SUMMARY_METADATA_KIND,
            "source_turn_ids": source_turn_ids[:1024],  # 防止 metadata 超长
            "source_turn_count": len(turns),
            "source_topic_ids": ",".join(source_topic_ids)[:512],
            "agents_covered": ",".join(agents_covered)[:512],
            "primary_topic_id": primary_topic_id,
            "summarizer_model": self._settings.OLLAMA_MODEL_SUMMARIZER,
            "created_at_wall_unix": round(time.time(), 6),
        }

        try:
            status = self._gateway.enqueue(
                turn_id=summary_id,
                agent_id="__summarizer__",
                topic_id=primary_topic_id,
                content=summary_text,
                metadata=metadata,
            )
        except Exception as exc:
            self._last_error = f"enqueue_summary: {exc!r}"
            logger.warning(
                f"[Summarizer] 摘要入库异常 (原始已入库,语义未损): {exc!r}"
            )
            return

        if status == EntryStatus.QUEUED:
            self._summaries_made_total += 1
            self._last_summary_preview = summary_text[:80]
            self._history.append(
                _SummaryRecord(
                    summary_id=summary_id,
                    source_turn_count=len(turns),
                    source_topic_ids=tuple(source_topic_ids),
                    char_count=len(summary_text),
                    at_wall_unix=time.time(),
                )
            )
            logger.info(
                f"[Summarizer] ✅ 摘要入库 · id={summary_id} · "
                f"sources={len(turns)} · chars={len(summary_text)} · "
                f"preview={summary_text[:60]!r}"
            )
        else:
            logger.warning(
                f"[Summarizer] 摘要被 Chroma 队列拒收 · status={status.value}"
            )

    # ════════════════════════════════════════════════════════════
    # 观测
    # ════════════════════════════════════════════════════════════

    def metrics(self) -> SummarizerMetrics:
        return SummarizerMetrics(
            is_running=self.is_running,
            cycles_total=self._cycles_total,
            summaries_made_total=self._summaries_made_total,
            summary_attempts_total=self._summary_attempts_total,
            busy_skips_total=self._busy_skips_total,
            ollama_failures_total=self._ollama_failures_total,
            last_bridge=(
                self._last_bridge.to_dict() if self._last_bridge else None
            ),
            last_summary_preview=self._last_summary_preview,
            last_error=self._last_error,
        )

    def snapshot_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics().to_dict(),
            "recent_summaries": [r.to_dict() for r in list(self._history)[-8:]],
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_summarizer_singleton: Optional[BackgroundSummarizer] = None
_summarizer_init_lock: Optional[asyncio.Lock] = None


def _get_init_lock() -> asyncio.Lock:
    global _summarizer_init_lock
    if _summarizer_init_lock is None:
        _summarizer_init_lock = asyncio.Lock()
    return _summarizer_init_lock


async def get_summarizer() -> BackgroundSummarizer:
    """
    获取全局 BackgroundSummarizer 单例 (异步,需要事件循环已就绪)。
    """
    global _summarizer_singleton
    if _summarizer_singleton is not None:
        return _summarizer_singleton

    lock = _get_init_lock()
    async with lock:
        if _summarizer_singleton is None:
            from backend.core.arbitrator import get_arbitrator
            from backend.core.semaphore import get_llm_semaphore

            settings = get_settings()
            sem = await get_llm_semaphore()
            _summarizer_singleton = BackgroundSummarizer(
                settings=settings,
                context_bus=get_context_bus(),
                chroma_gateway=get_chroma_gateway(),
                semaphore=sem,
                arbitrator=get_arbitrator(),
            )
        return _summarizer_singleton


def reset_summarizer_for_testing() -> None:
    """[仅供测试] 重置全局单例 (不调 stop)。"""
    global _summarizer_singleton, _summarizer_init_lock
    _summarizer_singleton = None
    _summarizer_init_lock = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 主体
    "BackgroundSummarizer",
    # 数据类
    "SummarizerMetrics",
    # 异常
    "SummarizerError",
    "SummarizerStoppedError",
    # 常量
    "SUMMARY_METADATA_KIND",
    "RAW_METADATA_KIND",
    # 单例
    "get_summarizer",
    "reset_summarizer_for_testing",
]