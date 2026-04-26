# 🌀 MBTI Chaos Debate · 多智能体混沌辩论模拟器

> **100% 离线运行 · 零成本本地部署 · 高并发沙盒系统**
> 一个由 8 个差异化 MBTI 人格代理在显式有限状态机下进行实时辩论的混沌沙盒,
> 配备赛博极简主义风格的 HUD 监控大屏。

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)]()
[![Vue](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vue.js&logoColor=white)]()
[![Ollama](https://img.shields.io/badge/Ollama-Metal_Native-000000)]()
[![License](https://img.shields.io/badge/License-MIT-green)]()

---

## 📐 一、系统架构总览

### 1.1 物理隔离拓扑

┌──────────────────────────────────────────────────────────────────────┐
│                  macOS 宿主机 (Apple Silicon · Metal API)             │
│                                                                       │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  🦙  Ollama Daemon (裸跑 · 独占 GPU/Metal)                  │    │
│   │     端口: 11434                                             │    │
│   │     模型: qwen2.5:14b-instruct-q4_K_M                       │    │
│   │     ⚠️ 严禁容器化 —— Docker 会切断 Metal API 直通          │    │
│   └─────────────────────▲──────────────────────────────────────┘    │
│                         │ host.docker.internal:11434                 │
│                         │                                            │
│   ┌─────────────────────┴──────────────────────────────────────┐    │
│   │  🐳 Docker Compose Network (hud_net · bridge)               │    │
│   │                                                              │    │
│   │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │    │
│   │  │   Frontend   │    │   Backend    │    │   ChromaDB   │  │    │
│   │  │  (nginx:80)  │───▶│  FastAPI     │───▶│  (vector DB) │  │    │
│   │  │   Vue 3 HUD  │    │  (8000)      │    │   (8000)     │  │    │
│   │  │              │◀──WS│              │    │              │  │    │
│   │  └──────────────┘    └──────────────┘    └──────────────┘  │    │
│   │       127.0.0.1:5173    127.0.0.1:8000   127.0.0.1:8001     │    │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘

### 1.2 控制流核心机制

| 机制 | 实现位置 | 解决的工程问题 |
|------|---------|---------------|
| 全局算力信号量 | `core/semaphore.py` | 防 OOM · 强制 LLM 并发 ≤ N |
| 显式有限状态机 (FSM) | `core/fsm.py` | Listening 态严禁推理 → 算力节约 |
| Aggro 压力值引擎 | `core/aggro_engine.py` | 纯规则计算被动刺激 · 防溢出 clamp |
| 全局仲裁器 | `core/arbitrator.py` | 唯一中断令牌 · 杜绝多 Agent 抢话死锁 |
| 滑动缓冲区 | `core/stream_parser.py` | 双轨制前缀拦截 · 解决 chunk 切分陷阱 |
| 全局超时看门狗 | `core/watchdog.py` | 熔断僵尸任务 · 主动 `aclose()` 断流 |
| 影子上下文 | `core/context_bus.py` | 打断后纯规则截断 · 不调用 LLM 总结 |
| 强心剂注入 | `agents/base_agent.py` | Prompt 尾部行为锚点 · 防人格同化 |

---

## 🚀 二、快速开始

### 2.1 系统前置要求

| 组件 | 最低版本 | 推荐版本 | 说明 |
|------|---------|---------|------|
| macOS | 13 Ventura | 14 Sonoma+ | Apple Silicon (M1/M2/M3) |
| 内存 | 16 GB | 32 GB+ | M3 Pro 18GB 仅可运行 7B 模型 |
| Docker Desktop | 4.30 | 4.36+ | 启用 VirtioFS 加速 |
| Homebrew | 4.0 | latest | 用于安装 Ollama |
| Ollama | 0.5.0 | 0.5.4+ | 必须裸跑于宿主机 |

### 2.2 第一步: 宿主机安装并启动 Ollama (⚠️ 严禁容器化)

```bash
# 1. 安装 Ollama (推荐 Homebrew)
brew install ollama

# 2. 以 Daemon 方式启动 (后台常驻 · 自动接管 Metal API)
brew services start ollama

# 3. 验证服务
curl http://localhost:11434/api/tags
# 期望响应: {"models":[]}

# 4. 预拉取主推理模型 (~ 9 GB)
ollama pull qwen2.5:14b-instruct-q4_K_M

# 5. 预拉取摘要模型 (~ 2 GB,后台 Summarizer 使用)
ollama pull qwen2.5:3b-instruct-q4_K_M

# 6. 验证模型已就绪
ollama list
```

**⚠️ 重要**: 切勿用 `ollama serve &` 临时启动 —— 终端关闭后服务会被 SIGHUP 杀死。
**必须使用 `brew services` 让 launchd 接管**,以保证 macOS 重启后 Ollama 自动恢复。

### 2.3 第二步: 配置环境变量

```bash
# 克隆项目后,在项目根目录执行:
cp .env.example .env

# 编辑 .env,根据本机硬件调整以下关键参数:
#   - GLOBAL_LLM_CONCURRENCY  : M3 Pro 18GB → 1 ; M2 Max 32GB → 2 ; M3 Max 64GB → 3
#   - OLLAMA_MODEL_PRIMARY    : 已下载的模型 tag
```

### 2.4 第三步: 启动容器编排

```bash
# 一键构建并启动 (首次约 3 ~ 5 分钟,后续秒级)
docker compose up -d --build

# 查看启动状态
docker compose ps

# 期望输出:
#   NAME             STATUS                  PORTS
#   mbti_backend     Up (healthy)            127.0.0.1:8000->8000/tcp
#   mbti_chroma      Up (healthy)            127.0.0.1:8001->8000/tcp
#   mbti_frontend    Up (healthy)            127.0.0.1:5173->80/tcp
```

### 2.5 第四步: 打开 HUD 大屏

浏览器访问: **http://localhost:5173**

> 推荐使用 1920×1080 及以上分辨率以获得最佳视觉效果。
> 4K / 5K 显示器下 HUD 自动适配三栏黄金比例。

---

## ⚙️ 三、关键配置项详解

### 3.1 算力控流 (`GLOBAL_LLM_CONCURRENCY`)

| 机型 | 推荐并发数 | 实测 token/s |
|------|-----------|-------------|
| M3 Pro 18GB | 1 | ~ 25 |
| M2 Max 32GB | 2 | ~ 45 (双流) |
| M3 Max 64GB | 3 | ~ 70 (三流) |
| M3 Ultra 128GB | 3 | ~ 90 (受 Metal 调度上限制约) |

**严禁超过 3** —— Ollama 内部不做精细队列,过高并发会触发 metal allocator 抖动并最终 OOM。

### 3.2 Aggro 物理参数

公式 (量纲一律为「秒」):

$$A_t = \max\left(0, \min\left(A_{max}, A_{max} - (A_{max} - A_{t-1}) \cdot e^{-\lambda \cdot \Delta t} + \sum \Delta A_{stimulus}\right)\right)$$

- `AGGRO_LAMBDA_DEFAULT=0.05` ⇒ 约 14 秒衰减 50%
- `AGGRO_INTERRUPT_THRESHOLD=85.0` ⇒ 触发抢话申请
- `AGGRO_MAX=100.0` ⇒ 触顶不爆炸,触底不归零

各 MBTI 代理的 λ 在 `agents/*.py` 中按人格差异化覆盖 (例: INTJ λ 极低,ENTP λ 极高)。

---

## 🎭 四、8 个 MBTI 代理速览

| Agent | 阵营 | 核心特征 | λ (1/s) | 关键能力 |
|-------|------|---------|---------|---------|
| **INTJ** 逻辑硬核 | NT (青) | 三段论 · 冰冷说理 | 极低 0.02 | — |
| **ENTP** 犀利攻防 | NT (青) | 寻找漏洞 · 高频打断 | 极高 0.15 | 低阈值抢话 |
| **INFP** 共情煽动 | NF (紫) | 情绪驱动 · 纯语义 | 中 0.06 | — |
| **ENFP** 幽默解构 | NF (紫) | 破冰插科打诨 | 中 0.07 | 全局 Aggro>85 时申请破冰 |
| **ISTJ** 稳守框架 | SJ (琥) | 议题拉回 · 偏题零容忍 | 低 0.04 | 主题守门员 |
| **ISFJ** 资料考据 | SJ (琥) | 证据驱动 · 必检索 | 低 0.04 | **唯一 ChromaDB 检索权** |
| **ESTP** 全局控场 | SP (绿) | 节奏推动 · 真空期补位 | 中 0.08 | **后台 Summarizer** |
| **ISFP** 灵活应变 | SP (绿) | 阵营动态补位 | 高 0.10 | 受全局 Aggro 影响阵营 |

---

## 🛠️ 五、故障诊断 Runbook

### 5.1 症状: HUD 大屏空白 / WS 连接失败

```bash
# Step 1: 检查后端是否健康
curl http://localhost:8000/api/sandbox/snapshot

# Step 2: 检查容器日志
docker compose logs -f backend --tail 100

# Step 3: 检查 Ollama 是否被容器访问到
docker compose exec backend curl http://host.docker.internal:11434/api/tags
# ⚠️ 若失败: 检查 docker-compose.yml 中 extra_hosts 是否保留
```

### 5.2 症状: Agent 长时间卡在 [COMPUTING] · 信号量未释放

```bash
# 1. 进入容器查看 asyncio task 栈
docker compose exec backend python -c "
import asyncio, sys
for t in asyncio.all_tasks():
    print(t)
" || true

# 2. 紧急释放: 重启 backend (会触发 lifespan shutdown 释放信号量)
docker compose restart backend
```

### 5.3 症状: ChromaDB 写入阻塞 · 数据库锁定

```bash
# Chroma 内部使用 SQLite,严禁多线程并发写入。
# 本项目通过单线程 write queue 已规避,若仍出现:

# 1. 检查写入队列堆积
docker compose logs backend | grep "chroma_write_queue"

# 2. 重建 Chroma 持久卷 (⚠️ 会丢失长期记忆)
docker compose down
docker volume rm mbti-chaos-debate_chroma_data
docker compose up -d
```

### 5.4 症状: 推理时宿主机风扇狂转 · 系统卡顿

```bash
# 算力过载,需降低并发:
# 编辑 .env → GLOBAL_LLM_CONCURRENCY=1
docker compose up -d backend  # 仅重启 backend 即可生效
```

---

## 🔥 六、已规避的工程地雷 (维护者必读)

| # | 陷阱 | 本项目对策 |
|---|------|-----------|
| 1 | Ollama 容器化导致 Metal 不可用 | 物理隔离,Ollama 永远裸跑宿主机 |
| 2 | 抢话打断时 Ollama 仍在生成 (僵尸返回值) | `response.aclose()` 主动断流 + 看门狗熔断 |
| 3 | nginx 默认 60s 切断 WebSocket | `proxy_read_timeout 86400s` |
| 4 | LLM 输出 JSON 因 chunk 切分而正则失败 | 滑动缓冲区前缀拦截 + 双轨制纯文本 |
| 5 | 多 Agent 同时抢话 → 死锁 | 全局仲裁器单令牌互斥 |
| 6 | 长对话人格漂移 | Prompt 尾部强心剂注入 |
| 7 | ChromaDB SQLite 多线程锁 | 单线程写入队列 + `asyncio.to_thread` 读 |
| 8 | WebSocket 状态包风暴 | 10 Hz 节流阀 |
| 9 | 打断时调用 LLM 总结残文 → 二次抢算力 | 纯规则后缀截断 → 影子上下文 |
| 10 | uvicorn 多 worker 导致信号量失效 | `--workers 1` 单进程铁律 |

---

## 📂 七、目录结构概览

mbti-chaos-debate/
├── docker-compose.yml      # 编排 · 仅 3 个容器,Ollama 在外
├── .env.example            # 环境变量模板
├── backend/                # FastAPI 推理网关
│   ├── core/               # 核心控制层 (信号量/FSM/Aggro/仲裁/看门狗)
│   ├── agents/             # 8 个 MBTI 代理实现
│   ├── memory/             # ChromaDB 网关 + Summarizer
│   ├── api/                # REST + WebSocket 路由
│   └── schemas/            # Pydantic 数据契约
└── frontend/               # 赛博极简 HUD
└── src/
├── components/     # HudShell / AgentCard / DebateStream
├── composables/    # useWebSocket / useJitterBuffer
└── stores/         # Pinia 状态管理

详细目录树见 `[File Ledger]`。

---

## 🛑 八、停止与清理

```bash
# 优雅停止 (会触发 lifespan shutdown · 信号量释放 · WS BYE 包推送)
docker compose down

# 同时清理向量记忆持久卷 (⚠️ 不可逆)
docker compose down -v

# 停止宿主机 Ollama
brew services stop ollama
```

---

## 📜 九、许可与致谢

- License: **MIT**
- 推理引擎: [Ollama](https://ollama.com/) · [Qwen2.5](https://github.com/QwenLM/Qwen2.5)
- 向量库: [ChromaDB](https://www.trychroma.com/)
- 前端: [Vue 3](https://vuejs.org/) · [Tailwind CSS](https://tailwindcss.com/) · [VueUse](https://vueuse.org/)
- 字体: [JetBrains Mono](https://www.jetbrains.com/lp/mono/) · [Inter](https://rsms.me/inter/)

> 本项目以「工程现实」为最高优先级,所有设计决策均经过死锁与 OOM 推演。
> 任何看似多余的复杂度,都是某个深夜踩过的坑的墓志铭。