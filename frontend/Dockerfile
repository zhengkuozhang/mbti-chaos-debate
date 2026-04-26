# syntax=docker/dockerfile:1.7
# ==============================================================================
# MBTI Chaos Debate · Frontend Dockerfile
# ------------------------------------------------------------------------------
# 多阶段构建:
#   Stage 1 (builder)  : Node 20 + pnpm 构建 Vite 静态产物
#   Stage 2 (runtime)  : nginx:alpine 托管 SPA + 反代 FastAPI (HTTP & WebSocket)
# ------------------------------------------------------------------------------
# 工程要点:
#   1. WebSocket 反代必须显式 HTTP/1.1 + Upgrade/Connection 头
#   2. proxy_read_timeout 设为 24h,杜绝长辩论场景被默认 60s 切断
#   3. SPA 路由兜底 try_files $uri /index.html
#   4. 带 hash 资产强缓存 1 年; index.html 禁缓存
#   5. 仅拷贝 dist/,严禁 node_modules 渗入运行时
# ==============================================================================

# ──────────────────────────────────────────────────────────────────────────────
# Stage 1: Builder (Node 20 + pnpm)
# ──────────────────────────────────────────────────────────────────────────────
FROM node:20.18.1-alpine AS builder

ENV PNPM_HOME=/usr/local/share/pnpm \
    PATH=/usr/local/share/pnpm:$PATH \
    # 关闭 corepack 在 alpine 上偶发的 SSL 校验抖动
    COREPACK_ENABLE_STRICT=0 \
    # Vite 6 在容器内 host check 需放开
    HOST=0.0.0.0

# 启用 corepack 并锁定 pnpm 版本 (与 package.json engines 对齐)
RUN corepack enable \
    && corepack prepare pnpm@9.15.1 --activate

WORKDIR /build

# ---- 依赖安装层 (利用 docker layer cache) ----
# 先单独 COPY lockfile,避免源码变更触发依赖重装
COPY package.json pnpm-lock.yaml* ./

# 使用 BuildKit 缓存挂载加速 CI 重建
# --frozen-lockfile : lock 与 manifest 不一致直接失败,杜绝幽灵升级
# --prefer-offline  : 优先命中本地 pnpm store
RUN --mount=type=cache,id=pnpm-store,target=/root/.local/share/pnpm/store \
    if [ -f pnpm-lock.yaml ]; then \
        pnpm install --frozen-lockfile --prefer-offline; \
    else \
        echo "⚠️  pnpm-lock.yaml 不存在,执行首次安装" \
        && pnpm install --prefer-offline; \
    fi

# ---- 源码构建层 ----
COPY . .

# vue-tsc --noEmit 类型校验 → vite build 产出 dist/
# 失败立刻熔断,杜绝带类型错误的镜像进入生产
RUN pnpm run build \
    && ls -lah dist/ \
    && echo "✅ Frontend build artifact ready"


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2: Runtime (nginx:alpine)
# ──────────────────────────────────────────────────────────────────────────────
FROM nginx:1.27.3-alpine AS runtime

LABEL org.opencontainers.image.title="mbti-chaos-debate-frontend" \
      org.opencontainers.image.description="赛博极简 HUD 监控大屏 · Vue 3 + Tailwind CSS" \
      org.opencontainers.image.licenses="MIT"

# 安装 curl 用于 healthcheck (alpine 默认无)
RUN apk add --no-cache curl tini \
    && rm -rf /var/cache/apk/*

# 清空 nginx 默认站点配置
RUN rm -rf /usr/share/nginx/html/* \
    && rm -f /etc/nginx/conf.d/default.conf

# ---- 内联 nginx 配置 (避免外部文件依赖) ----
# 通过 heredoc 写入,保持 Dockerfile 自包含
RUN cat > /etc/nginx/conf.d/mbti-hud.conf <<'NGINX_CONF'
# ==============================================================================
# MBTI Chaos Debate · nginx site config
# ------------------------------------------------------------------------------
# 关键:
#   1. WebSocket /ws/* 反代必须 HTTP/1.1 + Upgrade 头
#   2. proxy_read_timeout 24h,匹配长辩论场景
#   3. SPA 路由兜底 -> /index.html
#   4. /assets/ 带 hash 强缓存; /index.html 禁缓存
# ==============================================================================

# WebSocket 升级映射 (Upgrade 头存在则透传 connection: upgrade)
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# 上游: FastAPI 网关 (与 docker-compose 服务名一致)
upstream mbti_backend {
    server backend:8000;
    keepalive 32;
}

server {
    listen      80 default_server;
    listen      [::]:80 default_server;
    server_name _;

    root        /usr/share/nginx/html;
    index       index.html;

    # 安全 / 性能基础
    server_tokens   off;
    client_max_body_size 2m;

    # gzip 压缩 (字体文件较大,显著降低 LCP)
    gzip                on;
    gzip_vary           on;
    gzip_min_length     1024;
    gzip_proxied        any;
    gzip_comp_level     6;
    gzip_types
        text/plain
        text/css
        text/javascript
        application/javascript
        application/json
        application/xml
        application/wasm
        font/woff2
        font/ttf
        image/svg+xml;

    # ---- HTTP API 反代 ----
    location /api/ {
        proxy_pass              http://mbti_backend;
        proxy_http_version      1.1;
        proxy_set_header        Host                $host;
        proxy_set_header        X-Real-IP           $remote_addr;
        proxy_set_header        X-Forwarded-For     $proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto   $scheme;
        proxy_set_header        Connection          "";
        proxy_buffering         off;
        proxy_request_buffering off;
        proxy_read_timeout      120s;
        proxy_send_timeout      120s;
        proxy_connect_timeout   10s;
    }

    # ---- WebSocket 反代 (核心: HTTP/1.1 + Upgrade 头) ----
    location /ws/ {
        proxy_pass              http://mbti_backend;
        proxy_http_version      1.1;

        # WebSocket 必备升级头
        proxy_set_header        Upgrade             $http_upgrade;
        proxy_set_header        Connection          $connection_upgrade;

        proxy_set_header        Host                $host;
        proxy_set_header        X-Real-IP           $remote_addr;
        proxy_set_header        X-Forwarded-For     $proxy_add_x_forwarded_for;
        proxy_set_header        X-Forwarded-Proto   $scheme;

        # 关闭缓冲 → 流式 Token 实时推送
        proxy_buffering         off;
        proxy_request_buffering off;

        # ⚠️ 长连接关键: 默认 60s 会粗暴切断 WS,改为 24h
        proxy_read_timeout      86400s;
        proxy_send_timeout      86400s;
        proxy_connect_timeout   10s;
    }

    # ---- 静态资源 (带 hash,强缓存 1 年) ----
    location /assets/ {
        access_log              off;
        expires                 1y;
        add_header              Cache-Control       "public, immutable";
        try_files               $uri =404;
    }

    # ---- 字体 (跨域 + 强缓存) ----
    location ~* \.(woff2?|ttf|otf|eot)$ {
        access_log              off;
        expires                 1y;
        add_header              Cache-Control       "public, immutable";
        add_header              Access-Control-Allow-Origin "*";
        try_files               $uri =404;
    }

    # ---- SPA 路由兜底 ----
    # /index.html 禁缓存 → 每次刷新都校验最新 entry
    location = /index.html {
        add_header              Cache-Control       "no-cache, no-store, must-revalidate";
        add_header              Pragma              "no-cache";
        expires                 0;
    }

    location / {
        try_files               $uri $uri/ /index.html;
    }

    # ---- 健康检查端点 ----
    location = /healthz {
        access_log              off;
        return                  200 'ok';
        add_header              Content-Type        text/plain;
    }

    # ---- 安全响应头 ----
    add_header X-Frame-Options          "SAMEORIGIN"            always;
    add_header X-Content-Type-Options   "nosniff"               always;
    add_header Referrer-Policy          "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy       "geolocation=(), microphone=(), camera=()" always;
}
NGINX_CONF

# 拷贝构建产物
COPY --from=builder /build/dist /usr/share/nginx/html

# 提升 nginx 文件描述符上限 (长 WS 连接场景必需)
RUN echo "worker_rlimit_nofile 8192;" >> /etc/nginx/nginx.conf.tail \
    && sed -i '1i worker_rlimit_nofile 8192;' /etc/nginx/nginx.conf || true

EXPOSE 80

# 容器内健康检查 (与 compose healthcheck 双保险)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost/healthz || exit 1

# tini 作为 PID 1: 正确转发 SIGTERM 给 nginx,优雅关闭长连接
ENTRYPOINT ["/sbin/tini", "--"]

# nginx 前台运行 (daemon off) → 容器主进程不退出
CMD ["nginx", "-g", "daemon off;"]