"""
==============================================================================
backend/core/stream_parser.py
------------------------------------------------------------------------------
双轨制流式解析 · 滑动缓冲区 · 前缀拦截 · Chunk 切分陷阱终结者

工程死局背景:
    LLM 流式输出会把 "[TARGET: INTJ_logician]" 切成任意 chunk,例如:
        chunk_1: "["
        chunk_2: "TARGE"
        chunk_3: "T: INT"
        chunk_4: "J_log"
        chunk_5: "ician]"
    任何在单 chunk 上跑的正则必然失败 → 必须用滑动缓冲累积。

解析机三态:
    WAITING_PREFIX  → 缓冲累积,试图匹配 [TARGET: ...]
    PREFIX_FOUND    → 已匹配,emit 一次 PREFIX,立即转 TEXT_STREAMING
    TEXT_STREAMING  → 纯文本透传,无任何额外解析 (零拷贝)

降级策略:
    1. 缓冲累积 ≥ STREAM_PREFIX_BUFFER_MAX_CHARS 仍未匹配 →
       发出 ERROR (kind=NO_PREFIX),把缓冲全部作为 TEXT 放行,
       prefix_target 写空字符串,任由 ContextBus 兜底扫描别名
    2. 首字符不是 '[' →
       立即降级为 TEXT_STREAMING,首 chunk 即放行 (高频路径优化)
    3. 流提前结束 (feed_eos) 但仍在 WAITING_PREFIX →
       视同上限熔断,缓冲全部作为 TEXT 放行

⚠️ 工程铁律:
    - 严禁单次正则跨 chunk 匹配 (用缓冲累积)
    - 严禁缓冲无限增长 (硬上限熔断)
    - 严禁解析异常中断流 (一律降级为 TEXT)
    - 严禁进入 TEXT_STREAMING 后还跑正则 (短路放行)

==============================================================================
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Final, Iterable, Iterator, Optional

from loguru import logger

from backend.core.config import get_settings


# ──────────────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────────────

#: 前缀语法 · 锚定字符串开头,严格匹配
#: 示例:
#:   [TARGET: INTJ_logician]
#:   [TARGET: INTJ_logician, ENTP_debater]
#:   [TARGET:  ALL ]      ← 允许特殊保留 ID "ALL" 表示全员
_PREFIX_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^\[TARGET:\s*([A-Za-z0-9_,\s]+?)\]\s*",
    flags=re.DOTALL,
)

#: 检查"前缀有可能正在形成"的轻量判定 · 用于早期降级决策
_PREFIX_LEADING_CANDIDATES: Final[frozenset[str]] = frozenset({"[", "[T", "[TA"})

#: 保留目标 ID
TARGET_ALL: Final[str] = "ALL"


# ──────────────────────────────────────────────────────────────────────────────
# 解析器状态
# ──────────────────────────────────────────────────────────────────────────────


class ParserState(str, enum.Enum):
    """解析器三态 · 严格单调推进。"""

    WAITING_PREFIX = "WAITING_PREFIX"
    """初始态: 累积 chunk 试图匹配 [TARGET: ...]"""

    PREFIX_FOUND = "PREFIX_FOUND"
    """已匹配前缀, 已 emit PREFIX · 此态仅一瞬,立即转 TEXT_STREAMING"""

    TEXT_STREAMING = "TEXT_STREAMING"
    """前缀阶段结束 · 后续 chunk 零开销直接放行"""


# ──────────────────────────────────────────────────────────────────────────────
# Emit 输出协议
# ──────────────────────────────────────────────────────────────────────────────


class EmitKind(str, enum.Enum):
    """解析器输出类型。"""

    PREFIX = "PREFIX"
    """前缀解析成功 · 携带 targets 列表"""

    TEXT = "TEXT"
    """纯文本 · 携带 chunk 字符串供 WS 推送"""

    ERROR = "ERROR"
    """解析异常 · 携带 reason · 业务层应记录但不应中断流"""


class ParseErrorReason(str, enum.Enum):
    """ERROR 子类型,精细化诊断。"""

    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"
    """缓冲累积达上限仍未匹配前缀 → 已自动降级为 TEXT_STREAMING"""

    INVALID_PREFIX_SYNTAX = "INVALID_PREFIX_SYNTAX"
    """匹配到 [TARGET: ...] 但内部解析失败 (例: 全空白)"""

    UNEXPECTED_FEED_AFTER_EOS = "UNEXPECTED_FEED_AFTER_EOS"
    """feed_eos 后仍调用 feed (业务侧 bug)"""


@dataclass(frozen=True, slots=True)
class ParserEmit:
    """
    解析器单次输出。

    业务层使用模式:
        for emit in parser.feed(chunk):
            if emit.kind is EmitKind.PREFIX:
                ongoing.set_metadata("prefix_target", ",".join(emit.targets))
            elif emit.kind is EmitKind.TEXT:
                ongoing.append_chunk(emit.text)
                await ws.send_text_packet(emit.text)
            elif emit.kind is EmitKind.ERROR:
                logger.warning(f"parser error: {emit.error_reason}")
    """

    kind: EmitKind

    # PREFIX 时填充
    targets: tuple[str, ...] = ()

    # TEXT 时填充
    text: str = ""

    # ERROR 时填充
    error_reason: Optional[ParseErrorReason] = None
    error_detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "targets": list(self.targets),
            "text": self.text,
            "error_reason": (
                self.error_reason.value if self.error_reason else None
            ),
            "error_detail": self.error_detail,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 异常 (仅用于编程错误,非流解析失败)
# ──────────────────────────────────────────────────────────────────────────────


class StreamParserError(Exception):
    """流解析器编程错误基类 (流内容错误请用 ParserEmit ERROR)。"""


class ParserAlreadyClosedError(StreamParserError):
    """feed_eos 后再调用 feed/feed_eos。"""


# ──────────────────────────────────────────────────────────────────────────────
# 主体: PrefixStreamParser
# ──────────────────────────────────────────────────────────────────────────────


class PrefixStreamParser:
    """
    单流前缀解析器 (有状态 · 实例不复用)。

    每个 LLM 推理流必须新建一个 PrefixStreamParser 实例,
    解析完成后即可丢弃。

    线程模型:
        - 假定单事件循环串行调用 feed (符合 FastAPI/asyncio 模型)
        - 不持锁,内部状态字段非线程安全
    """

    __slots__ = (
        "_buffer",
        "_state",
        "_buffer_max_chars",
        "_targets",
        "_emit_count",
        "_text_emitted_chars",
        "_closed",
        "_total_chunks_fed",
    )

    def __init__(
        self,
        buffer_max_chars: Optional[int] = None,
    ) -> None:
        """
        Args:
            buffer_max_chars: 缓冲上限 · None 则取 settings 默认
        """
        if buffer_max_chars is None:
            buffer_max_chars = get_settings().STREAM_PREFIX_BUFFER_MAX_CHARS

        if buffer_max_chars < 8:
            raise ValueError(
                f"buffer_max_chars 必须 ≥ 8,当前: {buffer_max_chars}"
            )

        self._buffer: str = ""
        self._state: ParserState = ParserState.WAITING_PREFIX
        self._buffer_max_chars: int = buffer_max_chars
        self._targets: tuple[str, ...] = ()
        self._emit_count: int = 0
        self._text_emitted_chars: int = 0
        self._closed: bool = False
        self._total_chunks_fed: int = 0

    # ────────────────────────────────────────────────────────────
    # 公开属性
    # ────────────────────────────────────────────────────────────

    @property
    def state(self) -> ParserState:
        return self._state

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def targets(self) -> tuple[str, ...]:
        """已解析出的目标列表 · WAITING_PREFIX 期为空元组。"""
        return self._targets

    @property
    def total_chunks_fed(self) -> int:
        return self._total_chunks_fed

    @property
    def text_emitted_chars(self) -> int:
        return self._text_emitted_chars

    # ────────────────────────────────────────────────────────────
    # 核心: feed
    # ────────────────────────────────────────────────────────────

    def feed(self, chunk: str) -> list[ParserEmit]:
        """
        喂入一个 chunk,返回本次产生的所有 emit (按顺序)。

        Args:
            chunk: LLM 流式 token chunk · 允许空字符串 (静默忽略)

        Returns:
            list[ParserEmit] · 可能为空 (例如缓冲累积中尚未达成判定)

        Raises:
            ParserAlreadyClosedError: 流已 EOS 后再次 feed
            TypeError: chunk 类型非 str
        """
        if self._closed:
            raise ParserAlreadyClosedError(
                "PrefixStreamParser 已关闭 (feed_eos),严禁再次 feed"
            )
        if not isinstance(chunk, str):
            raise TypeError(
                f"chunk 必须为 str,当前类型: {type(chunk).__name__}"
            )
        if not chunk:
            return []  # 空 chunk 静默忽略

        self._total_chunks_fed += 1

        # 高频路径短路: TEXT_STREAMING 态 → 零拷贝放行
        if self._state is ParserState.TEXT_STREAMING:
            self._text_emitted_chars += len(chunk)
            self._emit_count += 1
            return [ParserEmit(kind=EmitKind.TEXT, text=chunk)]

        # WAITING_PREFIX 路径
        # PREFIX_FOUND 仅作为瞬时态使用,不会持久停留
        return self._feed_waiting_prefix(chunk)

    def _feed_waiting_prefix(self, chunk: str) -> list[ParserEmit]:
        """处于 WAITING_PREFIX 时的喂入逻辑。"""
        emits: list[ParserEmit] = []

        # ── 早期降级: 缓冲为空且首字符不是 '['  → 一定不是前缀 ──
        # 这是高频优化:大多数 LLM 输出不带前缀时第一个 token 就是普通文字
        if not self._buffer and not chunk.startswith("["):
            return self._fallback_to_text_streaming(
                pending_chunk=chunk,
                emits=emits,
                error_reason=None,  # 非错误,只是没前缀
            )

        # ── 累积到缓冲 ──
        # 注意: 缓冲存的是"前缀候选区",一旦匹配前缀,后续部分作为 TEXT 放行
        self._buffer += chunk

        # ── 尝试匹配前缀 ──
        match = _PREFIX_REGEX.match(self._buffer)
        if match:
            return self._handle_prefix_match(match, emits)

        # ── 检查缓冲是否仍可能形成前缀 ──
        # 如果当前缓冲与 [TARGET 前缀的任何前缀都不匹配,直接降级
        # 例如缓冲是 "你好" 这种,显然不可能再变成 [TARGET: ...]
        if not self._buffer_could_still_match():
            return self._fallback_to_text_streaming(
                pending_chunk="",  # 缓冲已经全在 _buffer 里,_fallback 会处理
                emits=emits,
                error_reason=None,
            )

        # ── 缓冲是否超限? ──
        if len(self._buffer) >= self._buffer_max_chars:
            logger.warning(
                f"[PrefixStreamParser] 缓冲达上限 {self._buffer_max_chars} 字符 "
                f"未匹配前缀 → 降级为纯文本 · buffer={self._buffer!r}"
            )
            return self._fallback_to_text_streaming(
                pending_chunk="",
                emits=emits,
                error_reason=ParseErrorReason.BUFFER_OVERFLOW,
            )

        # 仍在累积,本次 feed 无产出
        return emits

    def _handle_prefix_match(
        self,
        match: re.Match[str],
        emits: list[ParserEmit],
    ) -> list[ParserEmit]:
        """前缀正则命中时的处理路径。"""
        raw_targets = match.group(1)
        targets = self._parse_targets(raw_targets)

        if not targets:
            # 语法上匹配了 [TARGET: ...] 但 targets 全空
            logger.warning(
                f"[PrefixStreamParser] 匹配到 TARGET 前缀但 targets 为空 · "
                f"raw={raw_targets!r} → 降级为纯文本"
            )
            emits.append(
                ParserEmit(
                    kind=EmitKind.ERROR,
                    error_reason=ParseErrorReason.INVALID_PREFIX_SYNTAX,
                    error_detail=f"empty targets in {raw_targets!r}",
                )
            )
            return self._fallback_to_text_streaming(
                pending_chunk="",
                emits=emits,
                error_reason=None,  # ERROR 已 emit,不重复
            )

        # ── 成功路径 ──
        self._targets = targets
        self._state = ParserState.PREFIX_FOUND
        self._emit_count += 1
        emits.append(
            ParserEmit(
                kind=EmitKind.PREFIX,
                targets=targets,
            )
        )
        logger.debug(
            f"[PrefixStreamParser] 前缀匹配成功 · targets={targets}"
        )

        # 立刻处理"前缀之后的剩余文本" (它已经在 _buffer 里)
        rest = self._buffer[match.end():]
        # 转 TEXT_STREAMING
        self._state = ParserState.TEXT_STREAMING
        self._buffer = ""

        if rest:
            self._text_emitted_chars += len(rest)
            self._emit_count += 1
            emits.append(ParserEmit(kind=EmitKind.TEXT, text=rest))

        return emits

    def _fallback_to_text_streaming(
        self,
        pending_chunk: str,
        emits: list[ParserEmit],
        error_reason: Optional[ParseErrorReason],
    ) -> list[ParserEmit]:
        """
        降级为 TEXT_STREAMING:
            1. 若有 error_reason,先 emit 一个 ERROR
            2. 缓冲 + pending_chunk 全部作为 TEXT 放行
            3. 状态推进至 TEXT_STREAMING

        ⚠️ 注意 emits 是引用,本函数会就地追加。
        """
        if error_reason is not None:
            emits.append(
                ParserEmit(
                    kind=EmitKind.ERROR,
                    error_reason=error_reason,
                    error_detail=(
                        f"buffer_len={len(self._buffer)} "
                        f"max={self._buffer_max_chars}"
                    ),
                )
            )

        full_text = self._buffer + pending_chunk
        self._buffer = ""
        self._state = ParserState.TEXT_STREAMING

        # prefix_target 留空 (无前缀场景)
        if not self._targets:
            self._targets = ()

        if full_text:
            self._text_emitted_chars += len(full_text)
            self._emit_count += 1
            emits.append(ParserEmit(kind=EmitKind.TEXT, text=full_text))

        return emits

    # ────────────────────────────────────────────────────────────
    # 流结束
    # ────────────────────────────────────────────────────────────

    def feed_eos(self) -> list[ParserEmit]:
        """
        通知流已结束 (LLM 生成完毕 / 主动断流)。

        若仍处于 WAITING_PREFIX 且缓冲非空 → 把缓冲作为 TEXT 放行。

        Raises:
            ParserAlreadyClosedError: 重复调用
        """
        if self._closed:
            raise ParserAlreadyClosedError(
                "PrefixStreamParser 已关闭,严禁重复 feed_eos"
            )

        emits: list[ParserEmit] = []

        if self._state is ParserState.WAITING_PREFIX and self._buffer:
            logger.debug(
                f"[PrefixStreamParser] EOS 时仍在 WAITING_PREFIX · "
                f"buffer={self._buffer!r} → 作为 TEXT 放行"
            )
            return_emits = self._fallback_to_text_streaming(
                pending_chunk="",
                emits=emits,
                error_reason=None,
            )
            self._closed = True
            return return_emits

        # 其他态: 无残留,直接关闭
        self._closed = True
        return emits

    # ────────────────────────────────────────────────────────────
    # 内部: 工具
    # ────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_targets(raw: str) -> tuple[str, ...]:
        """
        解析 "INTJ_logician, ENTP_debater" → ("INTJ_logician", "ENTP_debater")

        - 去重 (保持首次出现顺序)
        - 去空白
        - 大小写保持原样 (与 Agent 注册 ID 一致)
        """
        seen: set[str] = set()
        out: list[str] = []
        for piece in raw.split(","):
            t = piece.strip()
            if not t:
                continue
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
        return tuple(out)

    def _buffer_could_still_match(self) -> bool:
        """
        判断当前缓冲是否还有可能形成 [TARGET: ...] 前缀。

        策略: 检查缓冲是否是 "[TARGET:" 开头序列的某个前缀,
        或者缓冲已包含 "[TARGET:" 完整开头但 ']' 还没出现。
        """
        prefix_seed = "[TARGET:"
        buf = self._buffer

        # 缓冲短于 seed 时,seed 必须以 buf 开头
        if len(buf) <= len(prefix_seed):
            return prefix_seed.startswith(buf)

        # 缓冲长于 seed: 必须以 [TARGET: 开头,且尚未出现 ]
        return buf.startswith(prefix_seed) and "]" not in buf

    # ────────────────────────────────────────────────────────────
    # 诊断
    # ────────────────────────────────────────────────────────────

    def diagnostic_snapshot(self) -> dict[str, object]:
        """诊断快照 · 用于故障复盘 / 单元测试断言。"""
        return {
            "state": self._state.value,
            "is_closed": self._closed,
            "targets": list(self._targets),
            "buffer_len": len(self._buffer),
            "buffer_max_chars": self._buffer_max_chars,
            "buffer_preview": self._buffer[:32],
            "total_chunks_fed": self._total_chunks_fed,
            "emit_count": self._emit_count,
            "text_emitted_chars": self._text_emitted_chars,
        }


# ──────────────────────────────────────────────────────────────────────────────
# 流式辅助: 便利迭代器 (可选语法糖)
# ──────────────────────────────────────────────────────────────────────────────


def parse_chunks(
    chunks: Iterable[str],
    buffer_max_chars: Optional[int] = None,
) -> Iterator[ParserEmit]:
    """
    一次性消费 chunk 序列,逐个 yield ParserEmit。

    典型用于离线测试 / 重放,生产路径请直接持有 PrefixStreamParser 实例。
    """
    parser = PrefixStreamParser(buffer_max_chars=buffer_max_chars)
    try:
        for chunk in chunks:
            yield from parser.feed(chunk)
    finally:
        # 收尾清理,确保残留缓冲被 yield 出来
        if not parser.is_closed:
            for emit in parser.feed_eos():
                yield emit


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    # 状态枚举
    "ParserState",
    "EmitKind",
    "ParseErrorReason",
    # 数据类
    "ParserEmit",
    # 主体
    "PrefixStreamParser",
    # 异常
    "StreamParserError",
    "ParserAlreadyClosedError",
    # 便利函数
    "parse_chunks",
    # 常量
    "TARGET_ALL",
]