# syntax=docker/dockerfile:1.7
# ==============================================================================
# MBTI Chaos Debate · Backend Dockerfile
# ------------------------------------------------------------------------------
# 多阶段构建策略:
#   Stage 1 (builder) : 编译并安装 Python wheels (保留 build-essential)
#   Stage 2 (runtime) : 极简运行时镜像 (仅 libc + tini + python),非 root 用户
# ------------------------------------------------------------------------------
# 关键工程考量:
#   - tini 作为 PID 1: 正确转发 SIGTERM/SIGINT,触发 FastAPI lifespan shutdown
#     → 信号量 release / Ollama response.aclose() / Chroma write queue flush
#   - PYTHONUNBUFFERED=1 : 日志实时刷出,与 docker logs 流式同步
#   - PYTHONDONTWRITEBYTECODE=1 : 不生成 .pyc,降低镜像层体积
#   - 非 root 运行 : 容器逃逸纵深防御
# ==============================================================================

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Builder
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120

WORKDIR /build

# 仅安装构建期必需的系统依赖 (numpy 等需 gcc)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 利用 BuildKit 缓存挂载加速重建 (CI 命中率显著提升)
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip==24.3.1 setuptools==75.6.0 wheel==0.45.1 \
    && pip install --user --no-warn-script-location -r requirements.txt

# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.11.11-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="mbti-chaos-debate-backend" \
      org.opencontainers.image.description="FastAPI 推理网关 · 8 MBTI Agent 多智能体辩论沙盒" \
      org.opencontainers.image.source="https://github.com/local/mbti-chaos-debate" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/home/appuser/.local/bin:${PATH}" \
    # 防止 httpx 在容器内对未配置的 ipv6 地址做无谓尝试
    HTTPX_DEFAULT_HOST_FAMILY=ipv4 \
    # 让 uvicorn 优雅 shutdown 时的等待时长 = 看门狗超时上限
    UVICORN_GRACEFUL_TIMEOUT=50

# 仅安装运行时必需的系统组件:
#   - tini       : 信号转发的标准 PID 1
#   - curl       : 健康检查与诊断
#   - libgomp1   : numpy / 向量化计算依赖 (运行时)
#   - ca-certificates : HTTPS 证书 (httpx 调用任何外部地址前置依赖)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini \
        curl \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# 创建非 root 用户 (UID 1000 与 macOS 默认用户对齐,降低 volume 权限摩擦)
RUN groupadd --system --gid 1000 appgroup \
    && useradd --system --uid 1000 --gid appgroup \
        --create-home --home-dir /home/appuser \
        --shell /usr/sbin/nologin appuser

# 从 builder 阶段拷贝 Python 用户级依赖
COPY --from=builder --chown=appuser:appgroup /root/.local /home/appuser/.local

WORKDIR /app

# 拷贝源码 (.dockerignore 排除 __pycache__ / .pytest_cache / tests)
COPY --chown=appuser:appgroup . /app

USER appuser

EXPOSE 8000

# 容器内健康检查 (与 compose healthcheck 双保险)
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/sandbox/snapshot \
        || exit 1

# tini 作为 PID 1 → 正确转发 SIGTERM 至 uvicorn → FastAPI lifespan 优雅关闭:
#   ↳ 1. 关闭 WS 连接 (向前端推送 BYE 包)
#   ↳ 2. 释放全局信号量 (async with 自动)
#   ↳ 3. response.aclose() 主动断开 Ollama 流
#   ↳ 4. Chroma 写入队列 flush
ENTRYPOINT ["/usr/bin/tini", "--"]

# uvicorn 参数解析:
#   --host 0.0.0.0           容器内监听全部接口 (compose 端口已限制 127.0.0.1 暴露)
#   --port 8000              与 compose 映射对齐
#   --workers 1              全局单例信号量 / Arbitrator 必须单进程,严禁多 worker
#   --loop uvloop            高性能事件循环 (linux 适用)
#   --http httptools         高性能 http parser
#   --timeout-graceful-shutdown 50  与 LLM_REQUEST_TIMEOUT_SEC 对齐,等待看门狗清理
#   --no-access-log          access log 改由 loguru 接管,避免重复
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--timeout-graceful-shutdown", "50", \
     "--no-access-log"]