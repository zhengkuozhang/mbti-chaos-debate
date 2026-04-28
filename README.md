# MBTI Chaos Debate · 多智能体混沌辩论模拟器

**100% 离线运行 · 零成本本地部署 · 高并发沙盒系统**

一个由 8 个差异化 MBTI 人格代理在显式有限状态机管控下开展实时博弈辩论的混沌沙盒平台，配套赛博极简主义风格可视化 HUD 实时监控大屏，全流程本地算力闭环，无需联网、无额外费用。


[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)]()
[![Vue](https://img.shields.io/badge/Vue-3.5-4FC08D?logo=vue.js&logoColor=white)]()
[![Ollama](https://img.shields.io/badge/Ollama-Metal_Native-000000)]()
[![License](https://img.shields.io/badge/License-MIT-green)]()
    

---

## 一、系统架构总览

### 1.1 物理隔离拓扑架构

采用**宿主机原生AI推理 + Docker容器业务编排**物理隔离架构，兼顾Metal硬件加速性能与服务运维便捷性，杜绝容器虚拟化损耗。

```text
┌──────────────────────────────────────────────────────────────────────┐
│                  macOS 宿主机 (Apple Silicon · Metal API)             │
│                                                                       │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  🦙  Ollama Daemon (裸跑 · 独占 GPU/Metal 硬件加速)         │    │
│   │     端口: 11434                                             │    │
│   │     模型: qwen2.5:14b-instruct-q4_K_M                       │    │
│   │     ⚠️ 严禁容器化 —— Docker 会切断 Metal API 硬件直通        │    │
│   └─────────────────────▲──────────────────────────────────────┘    │
│                         │ host.docker.internal:11434                 │
│                         │ 容器跨宿主机通信互通                        │
│   ┌─────────────────────┴──────────────────────────────────────┐    │
│   │  🐳 Docker Compose Network (hud_net · bridge 桥接网络)      │    │
│   │                                                              │    │
│   │  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │    │
│   │  │   Frontend   │    │   Backend    │    │   ChromaDB   │  │    │
│   │  │  (nginx:80)  │───▶│  FastAPI     │───▶│  (向量数据库) │  │    │
│   │  │   Vue 3 HUD  │    │  (8000)      │    │   (8001)     │  │    │
│   │  │              │◀──WS│ 实时通信通道 │    │ 长期记忆存储 │  │    │
│   │  └──────────────┘    └──────────────┘    └──────────────┘  │    │
│   │       127.0.0.1:5173    127.0.0.1:8000   127.0.0.1:8001     │    │
│   └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.2 控制流核心运行机制

内置八大核心工程化机制，彻底解决多智能体并发死锁、算力溢出、人格漂移、任务僵尸化等核心痛点，保障混沌辩论稳定有序运行。

|核心机制名称|代码实现位置|核心解决工程问题|
|---|---|---|
|全局算力信号量|`core/semaphore.py`|限制LLM最大并发数，防止内存溢出OOM，硬件算力负载均衡|
|显式有限状态机 (FSM)|`core/fsm.py`|监听态禁止无效推理运算，精准节约硬件算力资源|
|Aggro 情绪压力值引擎|`core/aggro_engine.py`|纯规则化计算人格情绪刺激，数值钳位防数据溢出异常|
|全局唯一仲裁器|`core/arbitrator.py`|单抢话中断令牌管控，杜绝多智能体同时抢话造成死锁阻塞|
|滑动数据缓冲区|`core/stream_parser.py`|双轨制前缀拦截解析，完美解决LLM流式输出chunk切分解析失败陷阱|
|全局超时看门狗|`core/watchdog.py`|自动熔断僵尸卡死任务，主动断开无效推理流，释放占用资源|
|影子上下文管理|`core/context_bus.py`|辩论打断后纯规则文本截断，无需调用LLM二次总结，节省算力|
|人格强心剂注入|`agents/base_agent.py`|Prompt尾部行为锚点固定，长期辩论过程中防止MBTI人格同化偏移|
---

## 二、快速部署上手教程

### 2.1 系统硬件软件前置要求

|依赖组件|最低适配版本|推荐生产版本|关键补充说明|
|---|---|---|---|
|macOS 系统|13 Ventura|14 Sonoma+|仅限 Apple Silicon M1/M2/M3 系列芯片|
|设备内存|16 GB|32 GB+|M3 Pro 18GB 仅支持运行7B轻量化模型|
|Docker Desktop|4.30|4.36+|必须启用 VirtioFS 磁盘加速功能|
|Homebrew 包管理器|4.0|最新稳定版|用于宿主机原生安装 Ollama 推理服务|
|Ollama 推理引擎|0.5.0|0.5.4+|**必须宿主机裸跑，禁止任何容器化部署**|
### 2.2 第一步：宿主机安装并初始化Ollama推理服务

Ollama必须原生运行在macOS宿主机，依托Metal API硬件加速，容器化会直接切断硬件直通通道，导致推理性能暴跌、运行报错。

```bash
# 1. 通过Homebrew官方安装Ollama推理引擎
brew install ollama

# 2. 守护进程后台常驻启动，系统开机自动重启接管
brew services start ollama

# 3. 验证Ollama服务正常启动连通
curl http://localhost:11434/api/tags
# 正常期望响应: {"models":[]}

# 4. 预拉取主辩论推理模型 (~9GB，核心辩论算力支撑)
ollama pull qwen2.5:14b-instruct-q4_K_M

# 5. 预拉取对话摘要轻量化模型 (~2GB，后台记忆汇总专用)
ollama pull qwen2.5:3b-instruct-q4_K_M

# 6. 核验所有模型下载就绪，准备后续运行
ollama list
```

**关键强制备注**：禁止使用 `ollama serve `& 临时启动服务，终端关闭后服务会被系统SIGHUP信号强制杀死。必须通过 `brew services` 交由系统launchd托管，保障重启自动恢复、后台稳定常驻。

### 2.3 第二步：项目环境变量个性化配置

```bash
# 克隆项目仓库后，终端进入项目根目录执行
cp .env.example .env

# 打开.env配置文件，根据自身设备硬件性能调整两大核心参数
# GLOBAL_LLM_CONCURRENCY：全局LLM推理并发数，按机型严格匹配
# OLLAMA_MODEL_PRIMARY：填写已拉取的主推理模型标签名称
```

### 2.4 第三步：Docker容器编排一键启动

```bash
# 一键构建镜像并后台启动所有容器服务（首次启动3-5分钟，后续秒级启动）
docker compose up -d --build

# 查看所有容器运行健康状态
docker compose ps

# 正常期望输出示例：
#   NAME             STATUS                  PORTS
#   mbti_backend     Up (healthy)            127.0.0.1:8000->8000/tcp
#   mbti_chroma      Up (healthy)            127.0.0.1:8001->8000/tcp
#   mbti_frontend    Up (healthy)            127.0.0.1:5173->80/tcp
```

### 2.5 第四步：访问赛博HUD可视化监控大屏

浏览器直接访问本地地址，即可进入多智能体混沌辩论实时监控操作台：

**http://localhost:5173**

适配说明：推荐1920×1080及以上分辨率显示器，4K/5K高分屏自动适配三栏黄金视觉比例，沉浸式观看MBTI人格实时辩论博弈。

---

## 三、核心算力与情绪参数详解

### 3.1 算力并发控流核心配置（防OOM关键）

根据Apple Silicon不同机型精准匹配并发数，严控推理负载，杜绝Metal内存分配抖动与系统崩溃。

|设备机型配置|推荐LLM全局并发数|实测稳定推理速度(token/s)|
|---|---|---|
|M3 Pro 18GB 内存|1|~25|
|M2 Max 32GB 内存|2|~45 双流并发|
|M3 Max 64GB 内存|3|~70 三流并发|
|M3 Ultra 128GB 内存|3|~90（受Metal系统调度上限约束）|
**强制约束**：全局并发数严禁超过3，Ollama内部无精细化任务队列管理，过高并发会直接触发metal内存分配器抖动，最终导致推理进程崩溃、设备OOM卡死。

### 3.2 Aggro情绪压力值核心计算公式

所有情绪量纲统一为秒级，纯数学规则计算，不依赖LLM推理，算力零消耗：

 $A_t = \max\left(0, \min\left(A_{max}, A_{max} - (A_{max} - A_{t-1}) \cdot e^{-\lambda \cdot \Delta t} + \sum \Delta A_{stimulus}\right)\right)$ 

- **AGGRO_LAMBDA_DEFAULT=0.05**：情绪衰减系数，约14秒情绪自然衰减50%

- **AGGRO_INTERRUPT_THRESHOLD=85.0**：情绪阈值，达到数值自动触发智能体抢话申请

- **AGGRO_MAX=100.0**：情绪数值上限，触顶不溢出、触底不归零，数值稳定可控

每个MBTI智能体专属衰减系数λ，在`agents/*.py`人格配置文件中差异化自定义，贴合不同人格情绪反应特性（INTJ衰减极慢、ENTP衰减极快）。

---

## 四、8类MBTI辩论智能体核心人设速览

八大典型人格分四大阵营差异化定位，专属情绪系数、核心职能与辩论风格，形成真实混沌对抗辩论效果。

|智能体人格|人格阵营|核心辩论特征|情绪系数λ|专属核心职能能力|
|---|---|---|---|---|
|INTJ 逻辑硬核|NT 理性蓝营|三段论严谨论证，冰冷客观纯说理，无情绪干扰|极低 0.02|核心逻辑立论，奠定辩论基础框架|
|ENTP 犀利攻防|NT 理性蓝营|专攻逻辑漏洞，高频打断反驳，擅长辩论攻防|极高 0.15|低阈值主动抢话，打乱对方论证节奏|
|INFP 共情煽动|NF 感性紫营|情绪价值驱动，侧重语义共情，打动主观认知|中等 0.06|情感共鸣造势，引导辩论情绪走向|
|ENFP 幽默解构|NF 感性紫营|轻松破冰插科打诨，解构严肃对立辩论氛围|中等 0.07|全局情绪过高时自动申请破冰缓和气氛|
|ISTJ 稳守框架|SJ 守序琥珀营|严格坚守辩论议题，杜绝偏题跑题，规则至上|偏低 0.04|辩论主题守门员，强行拉回偏离议题|
|ISFJ 资料考据|SJ 守序琥珀营|实证证据驱动，凡事必检索考据，务实严谨|偏低 0.04|**全局唯一ChromaDB向量检索专属权限**|
|ESTP 全局控场|SP 灵活绿营|把控辩论节奏，填补发言真空期，统筹全局走向|中等 0.08|后台对话自动摘要总结，梳理辩论脉络|
|ISFP 灵活应变|SP 灵活绿营|动态平衡阵营战力，灵活补位适配辩论局势|偏高 0.10|随全局情绪值动态调整阵营立场与发言风格|
---

## 五、常见故障诊断运维 Runbook

### 5.1 故障症状：HUD大屏空白 / WebSocket连接失败

```bash
# Step 1：核验后端服务接口健康状态
curl http://localhost:8000/api/sandbox/snapshot

# Step 2：查看后端容器实时运行日志，排查报错
docker compose logs -f backend --tail 100

# Step 3：测试后端容器能否正常连通宿主机Ollama服务
docker compose exec backend curl http://host.docker.internal:11434/api/tags
# 排查方案：若访问失败，检查docker-compose.yml内extra_hosts配置是否完整保留
```

### 5.2 故障症状：智能体长期卡在[COMPUTING]状态，算力信号量不释放

```bash
# 1. 进入后端容器，查看asyncio异步任务阻塞堆栈
docker compose exec backend python -c "
import asyncio, sys
for t in asyncio.all_tasks():
    print(t)
" || true

# 2. 紧急快速修复：重启后端服务，自动触发生命周期关闭，强制释放算力信号量
docker compose restart backend
```

### 5.3 故障症状：ChromaDB向量数据库写入阻塞、数据库锁定报错

ChromaDB底层基于SQLite开发，不支持多线程并发写入，项目已内置单线程写入队列防护，异常时按以下步骤修复：

```bash
# 1. 查看数据库写入队列堆积日志，确认阻塞情况
docker compose logs backend | grep "chroma_write_queue"

# 2. 重建Chroma持久化存储卷（⚠️ 不可逆操作，会清空历史辩论记忆数据）
docker compose down
docker volume rm mbti-chaos-debate_chroma_data
docker compose up -d
```

### 5.4 故障症状：推理运行时设备风扇狂转、系统严重卡顿

```bash
# 核心解决：算力过载降配
# 编辑.env配置文件，将GLOBAL_LLM_CONCURRENCY调低至1
# 仅重启后端服务即可生效，无需重构镜像
docker compose up -d backend
```

---

## 六、已规避十大工程致命地雷（维护者必读）

|序号|原生技术陷阱|项目专属规避解决方案|
|---|---|---|
|1|Ollama容器化部署导致Metal硬件加速失效|物理架构隔离，Ollama强制宿主机裸跑，永不容器化|
|2|智能体抢话打断后，Ollama后台持续生成僵尸无效返回值|推理流主动aclose断开 + 全局看门狗双重熔断机制|
|3|Nginx默认60s强制切断WebSocket长连接|配置proxy_read_timeout 86400s，永久保活连接|
|4|LLM流式输出chunk切分，导致JSON正则解析失败|滑动缓冲区前缀拦截 + 双轨制纯文本解析兜底|
|5|多智能体同时抢话，引发任务死锁阻塞|全局仲裁器单令牌互斥机制，同一时间仅一人抢话|
|6|长周期辩论过程中MBTI人格逐渐同化漂移|Prompt尾部人格强心剂固定注入，锁定人设不变|
|7|ChromaDB SQLite多线程并发写入锁冲突|单线程专属写入队列 + 异步线程隔离读取操作|
|8|WebSocket状态高频推送引发消息风暴卡顿|10Hz频率节流阀，限制状态推送频次|
|9|辩论打断后调用LLM总结残文，二次占用算力|纯规则后缀文本截断，影子上下文无需LLM处理|
|10|Uvicorn多进程部署导致算力信号量全局失效|--workers 1单进程强制铁律，保障信号量全局唯一|
---

## 七、项目完整目录结构概览

```text
mbti-chaos-debate/
├── docker-compose.yml      # 容器编排核心配置（仅业务容器，Ollama宿主机外置）
├── .env.example            # 环境变量配置模板（部署需复制为.env自定义修改）
├── backend/                # FastAPI后端推理网关核心服务
│   ├── core/               # 底层核心控制层（信号量/FSM状态机/情绪引擎/仲裁器/看门狗）
│   ├── agents/             # 8个差异化MBTI辩论智能体人格逻辑实现
│   ├── memory/             # ChromaDB向量数据库网关 + 对话摘要汇总模块
│   ├── api/                # REST业务接口 + WebSocket实时通信路由
│   └── schemas/            # Pydantic数据模型契约与参数校验
└── frontend/               # 赛博极简风格HUD可视化监控大屏
    └── src/
        ├── components/     # 大屏核心组件：HUD外壳/智能体卡片/辩论流展示
        ├── composables/    # Vue组合式工具：WebSocket封装/抖动缓冲区处理
        └── stores/         # Pinia全局状态管理，实时同步辩论数据
```

完整精细化目录树详见项目内置**[File Ledger]** 文件。

---

## 八、项目停止与环境清理操作

```bash
# 1. 优雅停止所有容器服务（自动触发生命周期关闭，释放信号量与WebSocket连接）
docker compose down

# 2. 彻底停止容器并清空向量记忆持久卷（⚠️ 数据不可逆，谨慎操作）
docker compose down -v

# 3. 停止宿主机常驻Ollama推理服务
brew services stop ollama
```

---

## 九、开源许可与技术致谢

- **开源许可协议**：MIT License

- **本地推理引擎**：[Ollama](https://ollama.com/) · 阿里通义千问 [Qwen2.5](https://github.com/QwenLM/Qwen2.5)

- **向量记忆数据库**：[ChromaDB](https://www.trychroma.com/)

- **前端可视化技术栈**：[Vue 3](https://vuejs.org/) · [Tailwind CSS](https://tailwindcss.com/) · [VueUse](https://vueuse.org/)

- **界面专用字体**：[JetBrains Mono](https://www.jetbrains.com/lp/mono/) · [Inter](https://rsms.me/inter/)
