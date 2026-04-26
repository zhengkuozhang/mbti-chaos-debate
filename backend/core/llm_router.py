"""
==============================================================================
backend/core/llm_router.py
------------------------------------------------------------------------------
LLM 推理路由器 · Ollama 流式客户端 · 抢话/熔断/僵尸返回值终结者

整合职责 (整个后端最复杂的核心模块):
    1. httpx.AsyncClient 流式调用 Ollama /api/chat (NDJSON 协议)
    2. response.aclose() 主动断流 → 杜绝僵尸返回值污染下一回合
    3. FSM + 信号量 + 仲裁器三重守门 (acquire 前断言)
    4. 强心剂 Prompt 注入 (尾部行为锚点,防人格漂移)
    5. 三路并发监听:
         - Ollama chunk 流
         - 抢话事件 (interrupt_event)
         - 看门狗超时事件 (watchdog_event)
       任一就绪立即响应,asyncio.wait FIRST_COMPLETED
    6. tenacity 重试: 仅对"未开始流"的连接抖动生效
       流已开启的中途异常严禁重试,避免重复消耗算力
    7. PrefixStreamParser 双轨制集成
    8. ContextBus OngoingTurn 生命周期管理

⚠️ 工程铁律:
    - acquire 信号量前必须 fsm.assert_can_invoke_llm()
    - 必须持有 MicrophoneToken,否则拒绝调用
    - aclose 必须在 finally 路径,异常/正常都触发
    - 流中途异常严禁重试 (tenacity stop_after_attempt=1 for streaming)
    - 看门狗超时 → seal_with_shadow_suffix(WATCHDOG_TIMEOUT)
    - 抢话信号 → seal_with_shadow_suffix(INTERRUPTED, interrupter)

==============================================================================
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass
from typing import Final, Optional

import httpx
import orjson
from loguru import logger
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from backend.core.arbitrator import (
    Arbitrator,
    MicrophoneToken,
)
from backend.core.config import Settings, get_settings
from backend.core.context_bus import (
    ContextBus,
    OngoingTurn,
    Turn,
    TruncationReason,
)
from backend.core.fsm import (
    AgentFSM,
    FSMState,
    LLMInvocationForbiddenError,
)
from backend.core.semaphore import LLMSemaphore
from backend.core.stream_parser import (
    EmitKind,
    ParserEmit,
    PrefixStreamParser,
)


# ──────────────────────────────────────────────────────────────────────────────
# Ollama 协议常量
# ──────────────────────────────────────────────────────────────────────────────

#: Ollama chat endpoint
_OLLAMA_CHAT_PATH: Final[str] = "/api/chat"

#: 单 chunk 最大字节 (防止恶意/损坏的 NDJSON 行)
_MAX_NDJSON_LINE_BYTES: Final[int] = 64 * 1024

#: 流开启前的连接抖动重试次数
_CONNECT_RETRY_ATTEMPTS: Final[int] = 3

#: 重试间隔 (指数退避)
_CONNECT_RETRY_MIN_WAIT_SEC: Final[float] = 0.3
_CONNECT_RETRY_MAX_WAIT_SEC: Final[float] = 2.0


# ──────────────────────────────────────────────────────────────────────────────
# 抢话路由协议: text chunk 推送回调签名
# ──────────────────────────────────────────────────────────────────────────────


from typing import Awaitable, Callable

TextChunkCallback = Callable[[str], Awaitable[None]]
"""
业务层注入的 text chunk 推送回调。
每解析出一段纯文本 → 立即推送到 WebSocket (实时帧率)。
回调内部异常会被捕获,不影响主流。
"""


# ──────────────────────────────────────────────────────────────────────────────
# 输出结果
# ──────────────────────────────────────────────────────────────────────────────


class TerminationCause(str, enum.Enum):
    """单次推理终止原因。"""

    NORMAL = "NORMAL"
    """Ollama 自然完成 (done=True)"""

    INTERRUPTED = "INTERRUPTED"
    """被仲裁器抢话信号中止"""

    WATCHDOG_TIMEOUT = "WATCHDOG_TIMEOUT"
    """看门狗超时熔断"""

    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    """Ollama 端错误 (HTTP 4xx/5xx,非流中异常)"""

    NETWORK_ERROR = "NETWORK_ERROR"
    """网络层异常 (流中断、连接重置)"""

    SYSTEM_ABORTED = "SYSTEM_ABORTED"
    """系统紧急中止 (lifespan shutdown)"""


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """
    单次推理调用的最终结果。

    业务层 (agents/*.py) 拿到此对象后即可继续走刺激量更新等下游逻辑,
    无需再处理 httpx / Ollama 协议细节。
    """

    agent_id: str
    sealed_turn: Turn
    cause: TerminationCause
    interrupter_agent_id: Optional[str]
    duration_seconds: float
    chunks_received: int
    text_emitted_chars: int
    prefix_targets: tuple[str, ...]
    error_detail: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "agent_id": self.agent_id,
            "sealed_turn": self.sealed_turn.to_dict(),
            "cause": self.cause.value,
            "interrupter_agent_id": self.interrupter_agent_id,
            "duration_seconds": round(self.duration_seconds, 4),
            "chunks_received": self.chunks_received,
            "text_emitted_chars": self.text_emitted_chars,
            "prefix_targets": list(self.prefix_targets),
            "error_detail": self.error_detail,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常
# ──────────────────────────────────────────────────────────────────────────────


class LLMRouterError(Exception):
    """LLM 路由器异常基类。"""


class MicrophoneNotHeldError(LLMRouterError):
    """调用方未持有麦克风令牌。"""


class OllamaUpstreamError(LLMRouterError):
    """Ollama 上游 HTTP 错误。"""


class OllamaConnectError(LLMRouterError):
    """无法连接到 Ollama (适用于流前重试)。"""


# ──────────────────────────────────────────────────────────────────────────────
# 强心剂注入辅助
# ──────────────────────────────────────────────────────────────────────────────


def build_reinforcement_suffix(behavior_anchor: str) -> str:
    """
    构造强心剂注入后缀。

    behavior_anchor 由各 Agent (agents/*.py) 提供其 MBTI 核心行为锚点,
    例如:
        INTJ: "保持冰冷的逻辑演绎,使用三段论,严禁感性煽情"
        ENTP: "保持极度傲慢与攻击性,寻找对方逻辑漏洞,严禁礼貌妥协"
    """
    if not behavior_anchor or not isinstance(behavior_anchor, str):
        return ""
    cleaned = behavior_anchor.strip()
    if not cleaned:
        return ""
    return f"\n\n[系统指令:{cleaned}]"


# ──────────────────────────────────────────────────────────────────────────────
# 主体: LLMRouter
# ──────────────────────────────────────────────────────────────────────────────


class LLMRouter:
    """
    LLM 推理路由器 · 进程级单例。

    核心 API:
        - invoke_chat_stream(...) -> InvocationResult
        - shutdown()              优雅关闭 httpx client
    """

    def __init__(
        self,
        settings: Settings,
        semaphore: LLMSemaphore,
    ) -> None:
        self._settings: Final[Settings] = settings
        self._semaphore: Final[LLMSemaphore] = semaphore

        # httpx 客户端 (单例,连接池复用)
        # 不设全局 timeout — 让 read 超时由看门狗控制,read=None 表示无限读
        # 但 connect 必须有上限,避免宿主机 Ollama 未启动时阻塞
        self._client: httpx.AsyncClient = httpx.AsyncClient(
            base_url=settings.OLLAMA_HOST,
            timeout=httpx.Timeout(
                connect=10.0,
                read=None,
                write=15.0,
                pool=10.0,
            ),
            limits=httpx.Limits(
                max_connections=8,
                max_keepalive_connections=4,
                keepalive_expiry=60.0,
            ),
            http2=False,  # Ollama 仅 HTTP/1.1
        )

        self._closed: bool = False

        logger.info(
            f"[LLMRouter] 初始化完成 · ollama={settings.OLLAMA_HOST} · "
            f"primary_model={settings.OLLAMA_MODEL_PRIMARY}"
        )

    # ────────────────────────────────────────────────────────────
    # 关闭
    # ────────────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """关闭底层 httpx 客户端 (lifespan shutdown 调用)。"""
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.aclose()
            logger.info("[LLMRouter] httpx client 已关闭")
        except Exception as exc:  # 关闭路径必须不抛
            logger.warning(f"[LLMRouter] httpx aclose 异常: {exc!r}")

    # ════════════════════════════════════════════════════════════
    # 核心: 流式推理
    # ════════════════════════════════════════════════════════════

    async def invoke_chat_stream(
        self,
        *,
        agent_id: str,
        fsm: AgentFSM,
        token: MicrophoneToken,
        arbitrator: Arbitrator,
        context_bus: ContextBus,
        messages: list[dict[str, str]],
        behavior_anchor: str,
        on_text_chunk: Optional[TextChunkCallback] = None,
        model_override: Optional[str] = None,
        temperature: float = 0.85,
        top_p: float = 0.9,
        max_tokens: Optional[int] = None,
        watchdog_event: Optional[asyncio.Event] = None,
    ) -> InvocationResult:
        """
        发起一次完整的流式推理。

        Args:
            agent_id: 调用方 Agent ID
            fsm: 该 Agent 的 FSM (用于状态切换 + LLM 调用守门)
            token: 当前持有的麦克风令牌 (epoch 守门)
            arbitrator: 全局仲裁器 (用于在结束时释放令牌)
            context_bus: 共享上下文总线 (用于 OngoingTurn 生命周期)
            messages: Ollama chat 消息列表 [{role, content}, ...]
                      最后一条 assistant 准备开口 / 或 user/system 任意上下文
            behavior_anchor: MBTI 行为锚点字符串
            on_text_chunk: 每解析出一段纯文本 chunk 时的异步回调 (WS 推送)
            model_override: 覆盖默认模型 (例: ESTP 可指定 summarizer 模型)
            temperature / top_p / max_tokens: Ollama 采样参数
            watchdog_event: 外部看门狗事件 (None 则使用内部超时)

        Returns:
            InvocationResult · 含已封存的 Turn

        Raises:
            MicrophoneNotHeldError: token epoch 不匹配当前持有者
            LLMInvocationForbiddenError: FSM 处于禁止状态 (LISTENING / IDLE / UPDATING)
            OllamaConnectError: 流前连接失败 (重试已耗尽)
        """
        if self._closed:
            raise LLMRouterError("LLMRouter 已关闭,严禁调用")

        # ── 守门 1: token 必须匹配当前持有者 ──
        current = arbitrator.current_holder()
        if current is None or current.epoch != token.epoch:
            raise MicrophoneNotHeldError(
                f"Agent {agent_id!r} 持有的 token (epoch={token.epoch}) "
                f"已失效或被抢占"
            )

        # ── 守门 2: FSM 状态允许调 LLM ──
        # 注意此时 FSM 应处于 COMPUTING 或更靠后的状态
        # (调用方应在拿到 token 后立即 transition 到 COMPUTING)
        fsm.assert_can_invoke_llm()

        # ── 注入强心剂 ──
        injected_messages = self._inject_reinforcement(messages, behavior_anchor)

        # ── 准备 PrefixStreamParser + OngoingTurn ──
        parser = PrefixStreamParser()
        ongoing: Optional[OngoingTurn] = None

        # 内部看门狗事件 (与外部传入的合并监听)
        internal_watchdog = asyncio.Event()
        external_watchdog = (
            watchdog_event if watchdog_event is not None else asyncio.Event()
        )

        chunks_received = 0
        text_emitted_chars = 0
        prefix_targets: tuple[str, ...] = ()
        cause = TerminationCause.NORMAL
        interrupter_id: Optional[str] = None
        error_detail: Optional[str] = None

        started_mono = time.monotonic()

        # 开启 OngoingTurn (在 ContextBus 中登记)
        try:
            ongoing = await context_bus.start_turn(agent_id)
        except Exception as exc:
            raise LLMRouterError(
                f"start_turn 失败 · agent={agent_id!r} · {exc!r}"
            ) from exc

        # ── 包入信号量 + 看门狗任务 ──
        watchdog_task: Optional[asyncio.Task[None]] = None
        try:
            # 启动看门狗 (内部超时定时器)
            watchdog_task = asyncio.create_task(
                self._watchdog_timer(
                    timeout_sec=self._settings.LLM_REQUEST_TIMEOUT_SEC,
                    fire_event=internal_watchdog,
                ),
                name=f"watchdog_{agent_id}",
            )

            async with self._semaphore.guard(agent_id):
                # 推流前再次确认 token 仍有效 (acquire 期间可能被抢)
                current = arbitrator.current_holder()
                if current is None or current.epoch != token.epoch:
                    raise MicrophoneNotHeldError(
                        f"信号量 acquire 期间 token 失效 (epoch={token.epoch})"
                    )

                # 转 SPEAKING (在拿到信号量、即将首 token 时)
                if fsm.state == FSMState.COMPUTING:
                    await fsm.transition(
                        FSMState.SPEAKING,
                        reason="first_token_imminent",
                    )

                (
                    cause,
                    interrupter_id,
                    chunks_received,
                    text_emitted_chars,
                    prefix_targets,
                    error_detail,
                ) = await self._run_stream(
                    agent_id=agent_id,
                    ongoing=ongoing,
                    parser=parser,
                    token=token,
                    arbitrator=arbitrator,
                    messages=injected_messages,
                    on_text_chunk=on_text_chunk,
                    model=model_override or self._settings.OLLAMA_MODEL_PRIMARY,
                    temperature=temperature,
                    top_p=top_p,
                    max_tokens=max_tokens,
                    internal_watchdog=internal_watchdog,
                    external_watchdog=external_watchdog,
                )

        except LLMInvocationForbiddenError:
            cause = TerminationCause.SYSTEM_ABORTED
            error_detail = "FSM forbids LLM invocation"
            raise
        except MicrophoneNotHeldError as exc:
            cause = TerminationCause.SYSTEM_ABORTED
            error_detail = str(exc)
            raise
        except asyncio.CancelledError:
            # 上游 cancel: 视为系统中止
            cause = TerminationCause.SYSTEM_ABORTED
            error_detail = "task cancelled"
            raise
        finally:
            # 看门狗收尾
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except (asyncio.CancelledError, Exception):
                    pass

            # 封存 Turn
            sealed_turn = await self._finalize_turn(
                context_bus=context_bus,
                ongoing=ongoing,
                agent_id=agent_id,
                cause=cause,
                interrupter_id=interrupter_id,
                prefix_targets=prefix_targets,
            )

            # 释放令牌 (无论何种结局)
            try:
                await arbitrator.release_token(token)
            except Exception as exc:
                logger.warning(
                    f"[LLMRouter] release_token 异常 (忽略) · "
                    f"agent={agent_id!r} · {exc!r}"
                )

            # FSM 转回 LISTENING (除非已经被外部转走)
            try:
                if fsm.state in (FSMState.SPEAKING, FSMState.COMPUTING):
                    await fsm.transition(
                        FSMState.LISTENING,
                        reason=f"turn_done:{cause.value}",
                    )
            except Exception as exc:
                logger.warning(
                    f"[LLMRouter] FSM 收尾 transition 失败 · "
                    f"agent={agent_id!r} · {exc!r}"
                )

        duration = time.monotonic() - started_mono
        result = InvocationResult(
            agent_id=agent_id,
            sealed_turn=sealed_turn,  # type: ignore[arg-type]
            cause=cause,
            interrupter_agent_id=interrupter_id,
            duration_seconds=duration,
            chunks_received=chunks_received,
            text_emitted_chars=text_emitted_chars,
            prefix_targets=prefix_targets,
            error_detail=error_detail,
        )

        logger.info(
            f"[LLMRouter] DONE · agent={agent_id!r} · "
            f"cause={cause.value} · duration={duration:.2f}s · "
            f"chunks={chunks_received} · chars={text_emitted_chars}"
            + (f" · interrupter={interrupter_id!r}" if interrupter_id else "")
        )
        return result

    # ════════════════════════════════════════════════════════════
    # 内部: 主流程
    # ════════════════════════════════════════════════════════════

    async def _run_stream(
        self,
        *,
        agent_id: str,
        ongoing: OngoingTurn,
        parser: PrefixStreamParser,
        token: MicrophoneToken,
        arbitrator: Arbitrator,
        messages: list[dict[str, str]],
        on_text_chunk: Optional[TextChunkCallback],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: Optional[int],
        internal_watchdog: asyncio.Event,
        external_watchdog: asyncio.Event,
    ) -> tuple[
        TerminationCause,
        Optional[str],
        int,  # chunks_received
        int,  # text_emitted_chars
        tuple[str, ...],  # prefix_targets
        Optional[str],  # error_detail
    ]:
        """
        执行实际的 Ollama 流式调用 + 三路监听 + Parser 喂入。

        返回各类指标供上层汇总。
        """
        payload = self._build_payload(
            messages=messages,
            model=model,
            temperature=temperature,
            top_p=top_p,
            max_tokens=max_tokens,
        )

        chunks_received = 0
        text_emitted_chars = 0
        prefix_targets: tuple[str, ...] = ()
        cause = TerminationCause.NORMAL
        interrupter_id: Optional[str] = None
        error_detail: Optional[str] = None

        # ── 流前重试 ──
        try:
            response = await self._open_stream_with_retry(payload)
        except OllamaConnectError as exc:
            return (
                TerminationCause.NETWORK_ERROR,
                None,
                0,
                0,
                (),
                f"connect failed: {exc!s}",
            )
        except OllamaUpstreamError as exc:
            return (
                TerminationCause.UPSTREAM_ERROR,
                None,
                0,
                0,
                (),
                str(exc),
            )

        # ── 三路监听 + 流消费 ──
        # 注意: response 是 async context manager,但我们在多路监听中
        # 不能直接 async with,因为需要在抢话/超时时主动 aclose 它
        try:
            interrupt_event = token.interrupt_event

            line_iter = self._iter_ndjson_lines(response)

            while True:
                next_chunk_task = asyncio.ensure_future(
                    self._safe_next(line_iter)
                )
                interrupt_task = asyncio.ensure_future(interrupt_event.wait())
                int_wd_task = asyncio.ensure_future(internal_watchdog.wait())
                ext_wd_task = asyncio.ensure_future(external_watchdog.wait())

                done, pending = await asyncio.wait(
                    {
                        next_chunk_task,
                        interrupt_task,
                        int_wd_task,
                        ext_wd_task,
                    },
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # 立即取消所有未完成的等待 task,防泄漏
                for p in pending:
                    p.cancel()
                # 等取消完成,吞掉 CancelledError
                for p in pending:
                    try:
                        await p
                    except (asyncio.CancelledError, Exception):
                        pass

                # ── 抢话 ──
                if interrupt_task in done:
                    holder = arbitrator.current_holder()
                    interrupter_id = (
                        holder.holder_agent_id if holder else None
                    )
                    cause = TerminationCause.INTERRUPTED
                    logger.info(
                        f"[LLMRouter] 抢话信号 · agent={agent_id!r} · "
                        f"interrupter={interrupter_id!r}"
                    )
                    break

                # ── 看门狗 ──
                if int_wd_task in done or ext_wd_task in done:
                    cause = TerminationCause.WATCHDOG_TIMEOUT
                    error_detail = (
                        "internal_watchdog"
                        if int_wd_task in done
                        else "external_watchdog"
                    )
                    logger.warning(
                        f"[LLMRouter] 看门狗熔断 · agent={agent_id!r} · "
                        f"src={error_detail}"
                    )
                    break

                # ── 流数据 ──
                # next_chunk_task done
                try:
                    next_value = next_chunk_task.result()
                except Exception as exc:
                    cause = TerminationCause.NETWORK_ERROR
                    error_detail = f"stream read error: {exc!r}"
                    logger.error(
                        f"[LLMRouter] 流读取异常 · agent={agent_id!r} · "
                        f"err={exc!r}"
                    )
                    break

                if next_value is None:
                    # 流自然结束
                    break

                # 解析 NDJSON 行
                parsed = self._parse_ollama_line(next_value)
                if parsed is None:
                    # 损坏行,静默跳过
                    continue

                chunks_received += 1
                content_delta = parsed.get("content_delta", "")
                done_flag = bool(parsed.get("done", False))

                if content_delta:
                    # 喂入 PrefixStreamParser
                    emits = parser.feed(content_delta)
                    new_chars, targets_now = await self._dispatch_emits(
                        emits=emits,
                        ongoing=ongoing,
                        on_text_chunk=on_text_chunk,
                    )
                    text_emitted_chars += new_chars
                    if targets_now and not prefix_targets:
                        prefix_targets = targets_now

                if done_flag:
                    cause = TerminationCause.NORMAL
                    break

            # 收尾 parser (EOS)
            try:
                tail_emits = parser.feed_eos()
                new_chars, targets_now = await self._dispatch_emits(
                    emits=tail_emits,
                    ongoing=ongoing,
                    on_text_chunk=on_text_chunk,
                )
                text_emitted_chars += new_chars
                if targets_now and not prefix_targets:
                    prefix_targets = targets_now
            except Exception as exc:
                logger.warning(
                    f"[LLMRouter] feed_eos 异常 (忽略) · {exc!r}"
                )

        finally:
            # ⚠️ 关键: 主动 aclose 流,触发 Ollama 客户端断开检测
            # 这是 "杜绝僵尸返回值" 的核心步骤
            try:
                await response.aclose()
            except Exception as exc:
                logger.debug(
                    f"[LLMRouter] response.aclose 异常 (忽略): {exc!r}"
                )

        return (
            cause,
            interrupter_id,
            chunks_received,
            text_emitted_chars,
            prefix_targets,
            error_detail,
        )

    # ════════════════════════════════════════════════════════════
    # 内部: 流前连接 + 重试
    # ════════════════════════════════════════════════════════════

    async def _open_stream_with_retry(
        self,
        payload: dict[str, object],
    ) -> httpx.Response:
        """
        打开流式响应,仅对"连接抖动"重试,流开启后绝不重试。
        """
        last_exc: Optional[BaseException] = None
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(_CONNECT_RETRY_ATTEMPTS),
                wait=wait_exponential(
                    multiplier=_CONNECT_RETRY_MIN_WAIT_SEC,
                    max=_CONNECT_RETRY_MAX_WAIT_SEC,
                ),
                retry=retry_if_exception_type(
                    (httpx.ConnectError, httpx.ConnectTimeout)
                ),
                reraise=True,
            ):
                with attempt:
                    return await self._open_stream_once(payload)
        except RetryError as re:
            last_exc = re.last_attempt.exception() if re.last_attempt else re
            raise OllamaConnectError(
                f"Ollama 连接失败 (重试 {_CONNECT_RETRY_ATTEMPTS} 次): {last_exc!r}"
            ) from last_exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise OllamaConnectError(
                f"Ollama 连接失败: {exc!r}"
            ) from exc

        # 不应到达 (AsyncRetrying 会通过 with 块返回)
        raise OllamaConnectError("Ollama 连接失败 (未知路径)")

    async def _open_stream_once(
        self,
        payload: dict[str, object],
    ) -> httpx.Response:
        """单次尝试打开流。HTTP 错误抛 OllamaUpstreamError。"""
        try:
            request = self._client.build_request(
                "POST",
                _OLLAMA_CHAT_PATH,
                content=orjson.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
            # send with stream=True 等价于 client.stream
            response = await self._client.send(request, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise
        except httpx.HTTPError as exc:
            raise OllamaUpstreamError(
                f"Ollama HTTP 错误: {exc!r}"
            ) from exc

        # 状态码检查 (允许 200 之外的失败)
        if response.status_code != 200:
            try:
                body_preview = (await response.aread())[:512].decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                body_preview = "<unreadable>"
            try:
                await response.aclose()
            except Exception:
                pass
            raise OllamaUpstreamError(
                f"Ollama HTTP {response.status_code}: {body_preview}"
            )

        return response

    # ════════════════════════════════════════════════════════════
    # 内部: NDJSON 流迭代
    # ════════════════════════════════════════════════════════════

    @staticmethod
    async def _iter_ndjson_lines(response: httpx.Response):
        """
        迭代 Ollama 的 NDJSON 流 · 每行一个 JSON 对象 (字节序列)。

        使用 aiter_lines 而非 aiter_bytes,因为 httpx 已自动按 \n 切分。
        """
        async for line in response.aiter_lines():
            if not line:
                continue
            line_bytes = line.encode("utf-8") if isinstance(line, str) else line
            if len(line_bytes) > _MAX_NDJSON_LINE_BYTES:
                logger.warning(
                    f"[LLMRouter] NDJSON 行过长 ({len(line_bytes)} bytes),跳过"
                )
                continue
            yield line_bytes

    @staticmethod
    async def _safe_next(aiter) -> Optional[bytes]:
        """
        await 异步迭代器的下一个元素;迭代结束返回 None。
        """
        try:
            return await aiter.__anext__()
        except StopAsyncIteration:
            return None

    @staticmethod
    def _parse_ollama_line(line: bytes) -> Optional[dict[str, object]]:
        """
        解析单行 Ollama NDJSON,返回标准化字段:
            { content_delta: str, done: bool }

        损坏行返回 None。
        """
        try:
            obj = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            logger.warning(
                f"[LLMRouter] NDJSON 解析失败 · {exc} · line={line[:120]!r}"
            )
            return None

        if not isinstance(obj, dict):
            return None

        # Ollama /api/chat 协议:
        #   { "message": {"role": "assistant", "content": "..."}, "done": false }
        # 末尾:
        #   { "done": true, "total_duration": ..., ... }
        msg = obj.get("message")
        content_delta = ""
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str):
                content_delta = c

        done = bool(obj.get("done", False))

        return {"content_delta": content_delta, "done": done}

    # ════════════════════════════════════════════════════════════
    # 内部: Emit 分发 (Parser → Ongoing/WS)
    # ════════════════════════════════════════════════════════════

    async def _dispatch_emits(
        self,
        *,
        emits: list[ParserEmit],
        ongoing: OngoingTurn,
        on_text_chunk: Optional[TextChunkCallback],
    ) -> tuple[int, tuple[str, ...]]:
        """
        将 parser 的 emit 分发到 ongoing turn 与 WS 推送。

        返回: (本批次新增 text 字符数, 解析出的 prefix targets)
        """
        added = 0
        targets: tuple[str, ...] = ()
        for emit in emits:
            if emit.kind is EmitKind.PREFIX:
                targets = emit.targets
                ongoing.set_metadata(
                    "prefix_target", ",".join(emit.targets)
                )
                logger.debug(
                    f"[LLMRouter] PREFIX detected · targets={emit.targets}"
                )
            elif emit.kind is EmitKind.TEXT:
                ongoing.append_chunk(emit.text)
                added += len(emit.text)
                if on_text_chunk is not None:
                    try:
                        await on_text_chunk(emit.text)
                    except Exception as exc:
                        # WS 推送失败不影响主流
                        logger.warning(
                            f"[LLMRouter] on_text_chunk 异常 (忽略): {exc!r}"
                        )
            elif emit.kind is EmitKind.ERROR:
                logger.debug(
                    f"[LLMRouter] parser ERROR (已降级,继续) · "
                    f"reason={emit.error_reason!s} · detail={emit.error_detail}"
                )

        return added, targets

    # ════════════════════════════════════════════════════════════
    # 内部: 看门狗 + 收尾
    # ════════════════════════════════════════════════════════════

    @staticmethod
    async def _watchdog_timer(
        timeout_sec: float,
        fire_event: asyncio.Event,
    ) -> None:
        """
        简单计时器,到点 set 事件。被 cancel 则静默退出。
        """
        try:
            await asyncio.sleep(timeout_sec)
            fire_event.set()
        except asyncio.CancelledError:
            return

    @staticmethod
    async def _finalize_turn(
        *,
        context_bus: ContextBus,
        ongoing: Optional[OngoingTurn],
        agent_id: str,
        cause: TerminationCause,
        interrupter_id: Optional[str],
        prefix_targets: tuple[str, ...],
    ) -> Optional[Turn]:
        """
        统一封存 Turn (NORMAL / 影子后缀)。

        失败不抛 (lifespan / except 路径已经在收尾,不应再抛)。
        """
        if ongoing is None:
            return None

        # 兜底再写一次 prefix_target (防止 emit 阶段未触达 set_metadata)
        if prefix_targets:
            try:
                ongoing.set_metadata(
                    "prefix_target", ",".join(prefix_targets)
                )
            except RuntimeError:
                # ongoing 已封存,忽略
                pass

        truncation = TruncationReason.NORMAL
        interrupter = None
        if cause == TerminationCause.INTERRUPTED:
            truncation = TruncationReason.INTERRUPTED
            interrupter = interrupter_id or "UNKNOWN_INTERRUPTER"
        elif cause == TerminationCause.WATCHDOG_TIMEOUT:
            truncation = TruncationReason.WATCHDOG_TIMEOUT
        elif cause in (
            TerminationCause.UPSTREAM_ERROR,
            TerminationCause.NETWORK_ERROR,
            TerminationCause.SYSTEM_ABORTED,
        ):
            truncation = TruncationReason.SYSTEM_ABORTED

        try:
            return await context_bus.seal_turn(
                agent_id=agent_id,
                truncation=truncation,
                interrupter_agent_id=interrupter,
            )
        except Exception as exc:
            logger.error(
                f"[LLMRouter] seal_turn 异常 (尝试 emergency) · "
                f"agent={agent_id!r} · {exc!r}"
            )
            try:
                sealed = await context_bus.emergency_seal_all(
                    truncation=TruncationReason.SYSTEM_ABORTED
                )
                for s in sealed:
                    if s.agent_id == agent_id:
                        return s
            except Exception as exc2:
                logger.error(
                    f"[LLMRouter] emergency_seal_all 也失败: {exc2!r}"
                )
            return None

    # ════════════════════════════════════════════════════════════
    # 内部: Prompt / Payload 构造
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def _inject_reinforcement(
        messages: list[dict[str, str]],
        behavior_anchor: str,
    ) -> list[dict[str, str]]:
        """
        在 messages 列表的末尾追加 system 类型的强心剂消息。

        - 若末尾已存在 system 锚点,合并而非重复追加
        - 不修改入参 (返回新列表)
        """
        suffix = build_reinforcement_suffix(behavior_anchor)
        if not suffix:
            return list(messages)

        new_msgs = list(messages)
        new_msgs.append(
            {
                "role": "system",
                "content": suffix.lstrip("\n"),
            }
        )
        return new_msgs

    @staticmethod
    def _build_payload(
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        top_p: float,
        max_tokens: Optional[int],
    ) -> dict[str, object]:
        """构造 Ollama /api/chat 请求体。"""
        options: dict[str, object] = {
            "temperature": float(temperature),
            "top_p": float(top_p),
        }
        if max_tokens is not None and max_tokens > 0:
            options["num_predict"] = int(max_tokens)

        return {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options,
            # keep_alive 设大一些,减少冷启动 (5 分钟)
            "keep_alive": "5m",
        }


# ──────────────────────────────────────────────────────────────────────────────
# 进程级单例
# ──────────────────────────────────────────────────────────────────────────────


_router_singleton: Optional[LLMRouter] = None
_router_init_lock: Optional[asyncio.Lock] = None


def _get_router_init_lock() -> asyncio.Lock:
    global _router_init_lock
    if _router_init_lock is None:
        _router_init_lock = asyncio.Lock()
    return _router_init_lock


async def get_llm_router() -> LLMRouter:
    """
    获取全局 LLMRouter 单例。

    使用模式:
        from backend.core.llm_router import get_llm_router
        router = await get_llm_router()
        result = await router.invoke_chat_stream(...)
    """
    global _router_singleton
    if _router_singleton is not None:
        return _router_singleton

    lock = _get_router_init_lock()
    async with lock:
        if _router_singleton is None:
            from backend.core.semaphore import get_llm_semaphore
            settings = get_settings()
            semaphore = await get_llm_semaphore()
            _router_singleton = LLMRouter(
                settings=settings,
                semaphore=semaphore,
            )
        return _router_singleton


async def shutdown_llm_router() -> None:
    """关闭 LLMRouter 单例 (lifespan shutdown 调用)。"""
    global _router_singleton
    if _router_singleton is not None:
        await _router_singleton.shutdown()
        _router_singleton = None


def reset_router_for_testing() -> None:
    """[仅供测试] 重置全局单例 (不调 shutdown)。"""
    global _router_singleton, _router_init_lock
    _router_singleton = None
    _router_init_lock = None


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 主体
    "LLMRouter",
    # 数据类
    "InvocationResult",
    "TerminationCause",
    # 异常
    "LLMRouterError",
    "MicrophoneNotHeldError",
    "OllamaUpstreamError",
    "OllamaConnectError",
    # 回调签名
    "TextChunkCallback",
    # 工具
    "build_reinforcement_suffix",
    # 单例
    "get_llm_router",
    "shutdown_llm_router",
    "reset_router_for_testing",
]