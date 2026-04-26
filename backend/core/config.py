"""
==============================================================================
backend/core/config.py
------------------------------------------------------------------------------
全局配置中枢 · Single Source of Truth

所有模块必须通过 `get_settings()` 单例获取配置,严禁散落使用 os.getenv()。

设计原则:
    1. 启动期严格校验 (fail-fast): 任何物理量纲违规、范围越界、阈值矛盾,
       都在 Pydantic 校验器中熔断,杜绝运行时静默错误。
    2. 量纲铁律: 时间一律为「秒」, λ 单位为「1/秒」, 与公式中 Δt 配对。
    3. 类型安全: 所有字段强类型 + Literal 枚举, mypy 可静态推断。
    4. 单例缓存: @lru_cache(maxsize=1) 保证全应用仅一次实例化。

==============================================================================
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import List, Literal

from pydantic import (
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


# ──────────────────────────────────────────────────────────────────────────────
# 物理常量 (与 .env 解耦的不变量)
# ──────────────────────────────────────────────────────────────────────────────

#: Aggro 触发抢话申请的最低生命周期 (秒) —— 防止 Aggro 刚冒尖就被抢话的颤动
AGGRO_INTERRUPT_DEBOUNCE_SEC: float = 0.5

#: WebSocket 心跳间隔 (秒) —— 防止前后端空闲连接被中间代理断开
WEBSOCKET_HEARTBEAT_INTERVAL_SEC: float = 25.0

#: 状态包序列号回卷阈值 (32 位有符号整数上限)
PACKET_SEQUENCE_WRAP: int = 2_147_483_647


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic Settings 主体
# ──────────────────────────────────────────────────────────────────────────────


class Settings(BaseSettings):
    """
    全局配置对象。

    通过 BaseSettings 自动从以下源按优先级合并加载 (后者覆盖前者):
        1. 类字段默认值
        2. .env 文件
        3. 环境变量
        4. 显式传入构造参数 (仅测试用)
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        # 容忍未知变量 (Docker 会注入 PATH/HOME 等无关变量)
        extra="ignore",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 1] 应用运行环境
    # ──────────────────────────────────────────────────────────────────────
    APP_ENV: Literal["development", "staging", "production"] = Field(
        default="production",
        description="应用环境标记,影响日志详细程度与调试端点暴露",
    )

    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="loguru 日志级别",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 2] Ollama 宿主机直连
    # ──────────────────────────────────────────────────────────────────────
    OLLAMA_HOST: str = Field(
        default="http://host.docker.internal:11434",
        description="Ollama HTTP 端点 · 容器中必须使用 host.docker.internal",
    )

    OLLAMA_MODEL_PRIMARY: str = Field(
        default="qwen2.5:14b-instruct-q4_K_M",
        description="主推理模型 tag (8 个 Agent 公用)",
    )

    OLLAMA_MODEL_SUMMARIZER: str = Field(
        default="qwen2.5:3b-instruct-q4_K_M",
        description="后台 Summarizer 使用的轻量模型",
    )

    OLLAMA_REQUEST_TIMEOUT_SEC: float = Field(
        default=45.0,
        ge=5.0,
        le=300.0,
        description="单次 Ollama 请求超时 (秒)",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 3] 全局算力控流
    # ──────────────────────────────────────────────────────────────────────
    GLOBAL_LLM_CONCURRENCY: int = Field(
        default=2,
        ge=1,
        le=3,
        description="同一时刻允许的 Ollama 并发数 · 严禁超过 3",
    )

    LLM_REQUEST_TIMEOUT_SEC: float = Field(
        default=45.0,
        ge=5.0,
        le=300.0,
        description="全局看门狗超时 · 触发后强制 aclose() + 影子上下文",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 4] ChromaDB 向量记忆层
    # ──────────────────────────────────────────────────────────────────────
    CHROMA_HOST: str = Field(default="chromadb")
    CHROMA_PORT: int = Field(default=8000, ge=1, le=65535)
    CHROMA_COLLECTION_NAME: str = Field(default="mbti_debate_memory")
    CHROMA_TENANT: str = Field(default="default_tenant")
    CHROMA_DATABASE: str = Field(default="default_database")

    CHROMA_WRITE_QUEUE_MAXSIZE: int = Field(
        default=512,
        ge=16,
        le=8192,
        description="单线程写入 Worker 的环形缓冲容量",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 5] WebSocket 防风暴节流
    # ──────────────────────────────────────────────────────────────────────
    WS_STATE_PUSH_HZ: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="状态包(Aggro/FSM)推送频率 · 文本流不受此限",
    )

    WS_MAX_MESSAGE_BYTES: int = Field(
        default=65536,
        ge=1024,
        le=1_048_576,
        description="单连接单消息最大字节数 · 防恶意构造",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 6] Aggro 引擎物理参数 (量纲: 时间 = 秒, λ = 1/秒)
    # ──────────────────────────────────────────────────────────────────────
    AGGRO_LAMBDA_DEFAULT: float = Field(
        default=0.05,
        gt=0.0,
        le=1.0,
        description="衰减系数默认值 (单位: 1/秒) · e^(-λ·Δt) 中 Δt 必须为秒",
    )

    AGGRO_MIN: float = Field(
        default=0.0,
        ge=0.0,
        description="Aggro 绝对下界",
    )

    AGGRO_MAX: float = Field(
        default=100.0,
        gt=0.0,
        le=1000.0,
        description="Aggro 绝对上界",
    )

    AGGRO_INTERRUPT_THRESHOLD: float = Field(
        default=85.0,
        gt=0.0,
        description="抢话申请阈值 · 必须落在 (AGGRO_MIN, AGGRO_MAX) 区间",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 7] 上下文与短记忆窗口
    # ──────────────────────────────────────────────────────────────────────
    SHORT_MEMORY_WINDOW_TURNS: int = Field(
        default=8,
        ge=2,
        le=64,
        description="短记忆保留轮数 · 超出由 Summarizer 压缩归档",
    )

    STREAM_PREFIX_BUFFER_MAX_CHARS: int = Field(
        default=20,
        ge=8,
        le=128,
        description="滑动缓冲区前缀拦截字符上限",
    )

    # ──────────────────────────────────────────────────────────────────────
    # [SECTION 8] 安全 / CORS
    # ──────────────────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: List[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        description="CORS 允许来源 · .env 中以逗号分隔字符串提供",
    )

    # 可选: 未来若引入鉴权,使用 SecretStr 防止日志泄漏
    INTERNAL_API_TOKEN: SecretStr | None = Field(
        default=None,
        description="(预留) 内部 API 鉴权令牌",
    )

    # ══════════════════════════════════════════════════════════════════════
    # 单字段校验器
    # ══════════════════════════════════════════════════════════════════════

    @field_validator("OLLAMA_HOST")
    @classmethod
    def _validate_ollama_host(cls, v: str) -> str:
        """OLLAMA_HOST 必须包含 schema (http:// 或 https://)。"""
        if not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                f"OLLAMA_HOST 必须以 http:// 或 https:// 开头,当前值: {v!r}"
            )
        # 不允许尾随斜杠,统一规范化
        return v.rstrip("/")

    @field_validator("OLLAMA_MODEL_PRIMARY", "OLLAMA_MODEL_SUMMARIZER")
    @classmethod
    def _validate_model_tag(cls, v: str) -> str:
        """模型 tag 必须含冒号 (name:tag 形式),规避 Ollama 默认 latest 陷阱。"""
        if ":" not in v:
            raise ValueError(
                f"模型 tag 必须显式包含冒号 (例: qwen2.5:14b-instruct-q4_K_M),"
                f"当前值: {v!r} —— 杜绝 latest 浮动陷阱"
            )
        return v

    @field_validator("CORS_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, v: object) -> List[str]:
        """允许 .env 中以逗号分隔字符串提供 CORS 列表。"""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v if str(origin).strip()]
        raise TypeError(
            f"CORS_ALLOWED_ORIGINS 必须为 str(逗号分隔)或 List[str],"
            f"当前类型: {type(v).__name__}"
        )

    @field_validator("CORS_ALLOWED_ORIGINS")
    @classmethod
    def _validate_cors_origins(cls, v: List[str]) -> List[str]:
        """每个 CORS 来源必须含 schema。"""
        for origin in v:
            if not (origin.startswith("http://") or origin.startswith("https://")):
                raise ValueError(
                    f"CORS 来源必须以 http:// 或 https:// 开头,违规值: {origin!r}"
                )
        return v

    # ══════════════════════════════════════════════════════════════════════
    # 跨字段一致性校验
    # ══════════════════════════════════════════════════════════════════════

    @model_validator(mode="after")
    def _validate_aggro_bounds(self) -> "Settings":
        """
        Aggro 三元组一致性校验:
            1. AGGRO_MIN < AGGRO_MAX
            2. AGGRO_INTERRUPT_THRESHOLD ∈ (AGGRO_MIN, AGGRO_MAX)
        """
        if self.AGGRO_MIN >= self.AGGRO_MAX:
            raise ValueError(
                f"AGGRO_MIN ({self.AGGRO_MIN}) 必须严格小于 "
                f"AGGRO_MAX ({self.AGGRO_MAX})"
            )

        if not (self.AGGRO_MIN < self.AGGRO_INTERRUPT_THRESHOLD < self.AGGRO_MAX):
            raise ValueError(
                f"AGGRO_INTERRUPT_THRESHOLD ({self.AGGRO_INTERRUPT_THRESHOLD}) "
                f"必须严格落在 ({self.AGGRO_MIN}, {self.AGGRO_MAX}) 开区间内"
            )

        return self

    @model_validator(mode="after")
    def _validate_timeout_alignment(self) -> "Settings":
        """
        看门狗超时必须 ≥ Ollama 单次请求超时 + 缓冲。
        否则 Ollama 还在正常生成,看门狗就误熔断了。
        """
        # 看门狗保留至少 5s 缓冲用于影子上下文写入
        min_watchdog = self.OLLAMA_REQUEST_TIMEOUT_SEC + 5.0
        if self.LLM_REQUEST_TIMEOUT_SEC < min_watchdog:
            raise ValueError(
                f"LLM_REQUEST_TIMEOUT_SEC ({self.LLM_REQUEST_TIMEOUT_SEC}s) "
                f"必须 ≥ OLLAMA_REQUEST_TIMEOUT_SEC + 5s "
                f"(当前应 ≥ {min_watchdog}s) —— 避免误熔断"
            )
        return self

    @model_validator(mode="after")
    def _validate_chroma_endpoint(self) -> "Settings":
        """ChromaDB 主机名不允许为空字符串。"""
        if not self.CHROMA_HOST.strip():
            raise ValueError("CHROMA_HOST 不允许为空")
        return self

    @model_validator(mode="after")
    def _validate_model_distinction(self) -> "Settings":
        """
        Summarizer 模型与主推理模型可以相同,但若相同则记录警告意图
        (此处仅做存在性校验,实际告警由启动 banner 输出)。
        """
        # 保留位 —— 若未来需要强制区分模型,可在此处抛错
        return self

    # ══════════════════════════════════════════════════════════════════════
    # 派生属性 (业务层禁止重复推导)
    # ══════════════════════════════════════════════════════════════════════

    @property
    def aggro_decay_half_life_sec(self) -> float:
        """
        Aggro 衰减半衰期 (秒) = ln(2) / λ
        例: λ=0.05 → 半衰期 ≈ 13.86s
        """
        return math.log(2.0) / self.AGGRO_LAMBDA_DEFAULT

    @property
    def aggro_range(self) -> float:
        """Aggro 取值跨度 = max - min,供归一化使用。"""
        return self.AGGRO_MAX - self.AGGRO_MIN

    @property
    def ws_state_push_interval_sec(self) -> float:
        """状态包推送间隔 (秒) = 1 / Hz"""
        return 1.0 / self.WS_STATE_PUSH_HZ

    @property
    def chroma_url(self) -> str:
        """ChromaDB HTTP 端点 (供 chromadb-client 直连)。"""
        return f"http://{self.CHROMA_HOST}:{self.CHROMA_PORT}"

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    # ══════════════════════════════════════════════════════════════════════
    # 启动 banner
    # ══════════════════════════════════════════════════════════════════════

    def render_startup_banner(self) -> str:
        """
        生成启动横幅 · 集中暴露所有关键参数,供运维一眼诊断配置漂移。

        ⚠️ 严禁打印 SecretStr 字段的明文。
        """
        token_status = (
            "[CONFIGURED]" if self.INTERNAL_API_TOKEN is not None else "[NOT SET]"
        )
        lines = [
            "═" * 76,
            " MBTI Chaos Debate · Backend Bootstrap",
            "═" * 76,
            f" APP_ENV               : {self.APP_ENV}",
            f" LOG_LEVEL             : {self.LOG_LEVEL}",
            "─" * 76,
            f" OLLAMA_HOST           : {self.OLLAMA_HOST}",
            f" MODEL_PRIMARY         : {self.OLLAMA_MODEL_PRIMARY}",
            f" MODEL_SUMMARIZER      : {self.OLLAMA_MODEL_SUMMARIZER}",
            f" OLLAMA_TIMEOUT_SEC    : {self.OLLAMA_REQUEST_TIMEOUT_SEC}",
            "─" * 76,
            f" GLOBAL_LLM_CONCURRENCY: {self.GLOBAL_LLM_CONCURRENCY}",
            f" WATCHDOG_TIMEOUT_SEC  : {self.LLM_REQUEST_TIMEOUT_SEC}",
            "─" * 76,
            f" CHROMA_URL            : {self.chroma_url}",
            f" CHROMA_COLLECTION     : {self.CHROMA_COLLECTION_NAME}",
            f" CHROMA_WRITE_QUEUE    : {self.CHROMA_WRITE_QUEUE_MAXSIZE}",
            "─" * 76,
            f" WS_STATE_PUSH_HZ      : {self.WS_STATE_PUSH_HZ} Hz "
            f"(每 {self.ws_state_push_interval_sec * 1000:.1f} ms)",
            f" WS_MAX_MESSAGE_BYTES  : {self.WS_MAX_MESSAGE_BYTES}",
            "─" * 76,
            f" AGGRO_LAMBDA_DEFAULT  : {self.AGGRO_LAMBDA_DEFAULT} (1/s)",
            f" AGGRO_HALF_LIFE       : ≈ {self.aggro_decay_half_life_sec:.2f} s",
            f" AGGRO_RANGE           : [{self.AGGRO_MIN}, {self.AGGRO_MAX}]",
            f" AGGRO_INTERRUPT_TH    : {self.AGGRO_INTERRUPT_THRESHOLD}",
            "─" * 76,
            f" SHORT_MEMORY_WINDOW   : {self.SHORT_MEMORY_WINDOW_TURNS} turns",
            f" STREAM_PREFIX_BUFFER  : {self.STREAM_PREFIX_BUFFER_MAX_CHARS} chars",
            "─" * 76,
            f" CORS_ORIGINS          : {self.CORS_ALLOWED_ORIGINS}",
            f" INTERNAL_API_TOKEN    : {token_status}",
            "═" * 76,
        ]
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 全局单例工厂
# ──────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    获取全局 Settings 单例。

    使用模式:
        from backend.core.config import get_settings
        settings = get_settings()

    在 FastAPI 中作为依赖注入:
        from fastapi import Depends
        @app.get("/foo")
        async def foo(s: Settings = Depends(get_settings)):
            ...

    ⚠️ 严禁在业务代码中直接 `Settings()`,绕过 lru_cache 会导致:
        1. .env 重复 IO 解析
        2. 校验器重复执行
        3. 多实例配置漂移
    """
    return Settings()


def reload_settings_for_testing() -> Settings:
    """
    [仅供测试] 清除单例缓存并重新构造 Settings。

    ⚠️ 生产代码严禁调用 —— 这会破坏跨模块的配置一致性假设。
    """
    get_settings.cache_clear()
    return get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────

__all__ = [
    "Settings",
    "get_settings",
    "reload_settings_for_testing",
    "AGGRO_INTERRUPT_DEBOUNCE_SEC",
    "WEBSOCKET_HEARTBEAT_INTERVAL_SEC",
    "PACKET_SEQUENCE_WRAP",
]