# agent-platform

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![Node](https://img.shields.io/badge/node-%E2%89%A518-green.svg)](https://nodejs.org/)
[![React](https://img.shields.io/badge/react-19-61dafb.svg)](https://react.dev/)

[English](README.md) · **简体中文**

企业级 AI Agent 运行平台：多租户会话、在隔离沙箱（Docker 或 microVM）中执行的 Agent 任务、
可复用的**应用（App）**、技能包、租户级 MCP 服务、知识库管道，以及一套 28 个模块的管理后台。

agent-platform 是**业务层**，构建在
[**saas-foundation**](https://github.com/chenzefeng09/saas-foundation) 之上 ——
后者是一个独立可复用的企业级 SaaS 底座，负责身份、授权、组织、计费、审计、合规、LLM 网关、
存储、密码学、DLP 与特性开关。详见[与 saas-foundation 的关系](#与-saas-foundation-的关系)。

> **项目状态**：处于活跃开发中，已用于内部试点，但尚未 1.0。服务之间的接口仍在演进。
> 开发栈基于 docker-compose；`deploy/helm/` 下有 Helm chart，但打磨程度不及前者。

---

## 目录

- [能力概览](#能力概览)
- [与 saas-foundation 的关系](#与-saas-foundation-的关系)
- [架构](#架构)
- [仓库结构](#仓库结构)
- [快速开始](#快速开始)
- [配置](#配置)
- [开发](#开发)
- [沙箱镜像](#沙箱镜像)
- [安全模型](#安全模型)
- [文档](#文档)
- [参与贡献](#参与贡献)
- [许可证](#许可证)

---

## 能力概览

**对话与执行**

- **流式会话** —— 基于 SSE 的真 token 级流式输出，支持思考块、工具调用步骤分组折叠、多轮历史。
- **沙箱化 Agent 运行** —— 每次运行分配全新隔离工作区。两种运行时：Docker（开发）与
  CubeSandbox microVM（生产，内核级隔离，见
  [ADR 0001](docs/adr/0001-sandbox-runtime-cube-default-kata-fallback.md)）。
- **两套可互换的 Agent Worker** —— 基于
  [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) 的 TypeScript
  worker，以及基于 [AgentScope 2.x](https://github.com/agentscope-ai/agentscope) 的 Python
  worker（见 [ADR 0002](docs/adr/0002-agent-kernel-agentscope-over-openhands.md)）。
- **人在回路（HITL）** —— Agent 可在运行中途暂停提问或请求确认；卡片在页面刷新后仍能恢复，
  并原地续跑。

**可复用的 Agent 资产**

- **应用（App）** —— 把固定的 Agent harness（提示词、工具、输入、模型）封装为带版本、
  可审核、可发布的应用。支持手动运行、定时（cron）触发、以及 Webhook 入站触发（可带文件），
  并提供按应用维度的完整执行历史。
- **技能（Skill）** —— 纯指令包（`SKILL.md` + 可选脚本），渐进式披露。以内容寻址的宿主机缓存
  只读挂载进沙箱，单次运行零下载开销。内置一批平台级默认技能。
- **MCP** —— 租户级 MCP 服务注册表，支持自定义 header、鉴权（含 OAuth）、超时与工具黑白名单。
  经 broker 转发，沙箱内不持有租户凭据。
- **知识库** —— 文档 → 切块 → 向量化 → pgvector 的入库管道，运行期可作为工具检索。

**文档与工作区**

- **工作区与文件** —— 会话绑定的工作区、`@` 文件补全，以及 PDF / Word / Excel / PowerPoint /
  压缩包的内联预览。
- **画布（Canvas）** —— 模型可在对话过程中创建与修订的文档面，支持可运行 HTML 在沙箱 iframe 中预览。
- **产物（Artifacts）** —— 运行产出以可下载卡片的形式呈现在对话流中。

**运维**

- **管理后台** —— 28 个模块，覆盖租户、用户、RBAC、SSO、配额、模型管理、审计、会话回放、DLP、
  合规、数据保护、PAM、安全运营、生命周期、特性开关、存储、沙箱池监控与应用审核。
  其中大多数是对 saas-foundation 服务的薄代理。
- **全链路多租户** —— Postgres RLS、租户隔离存储、租户级配额校验，以及用于成本归集的
  per-run LLM 虚拟密钥。

---

## 与 saas-foundation 的关系

两个仓库沿一条线切分：**saas-foundation 是通用企业底座，agent-platform 是所有与 Agent 相关的部分。**

| | [saas-foundation](https://github.com/chenzefeng09/saas-foundation) | agent-platform（本仓库） |
|---|---|---|
| **定位** | 可复用 SaaS 底座，独立交付 | 构建在底座之上的业务层 |
| **负责** | identity、authz、org、billing、audit、compliance、notification、search、api-platform、llm-gateway、storage、crypto、dlp、data-protection、feature-flag、lifecycle、pam、security-ops | 会话、消息、运行、沙箱、Agent worker、技能、MCP、知识库、应用、画布、产物 |
| **依赖方向** | 不感知上层 | 通过 HTTP + SDK 消费底座 |

沿用底座的设计原则：**业务服务不直接触碰底层开源组件。** 本仓库没有任何服务直连 Keycloak、
OPA、LiteLLM、MinIO、Vault 或 Unleash —— 一律调用对应的 foundation 服务。这样底层组件可以整体
替换，而不必改动 Agent 侧代码。

### 具体怎么打通

1. **`foundation_sdk`（Python）** —— 各服务从中获取 Cookie 签名（`CookieSigner`）、
   请求上下文透传（`install_request_context`）、RFC 9457 problem+json 错误处理
   （`register_problem_details`）以及租户/用户提取（`get_tenant_id`、`get_user_id`）。
2. **共享 Docker 网络** —— agent compose 栈接入 foundation 栈的外部网络，由
   `FOUNDATION_NETWORK` 指定（默认 `saas-foundation-dev_foundation`）。
3. **服务间 HTTP** —— BFF 把管理后台流量代理到 foundation 服务；orchestrator 与 worker 携带
   per-run 虚拟密钥访问 LLM 网关。

SDK 有两条获取路径，详见
[docs/foundation-sdk-consumption.md](docs/foundation-sdk-consumption.md)：

- **内部 PyPI**（CI / 生产）—— 从私有源拉取 `foundation-sdk = ">=0.1,<0.2"`。
- **构建上下文**（本地开发，本仓库默认）—— docker-compose 通过 `additional_contexts` 注入兄弟
  目录里的 `sdks/python`，改 SDK 立即生效，不必先发版。

### ⚠️ 必须有 saas-foundation 才能跑起来

`foundation-sdk` 尚未发布到公网 PyPI。请把两个仓库克隆为兄弟目录：

```
Development/
├── saas-foundation/     # 必须先启动
└── agent-platform/      # 本仓库
```

若目录结构不同，请覆盖 `FOUNDATION_DIR`（Makefile）与 `FOUNDATION_SDK_PATH`
（`deploy/docker-compose/.env`）。

---

## 架构

```
                    浏览器 — React 19 + Vite  (:5173)
                              │  签名 Cookie 会话 · SSE
                              ▼
              ┌───────────────────────────────────┐        ┌──────────────────────────┐
              │  bff  :8005                       │───────▶│     saas-foundation      │
              │  鉴权代理 · API 聚合              │        │  identity · authz · org  │
              │  SSE 中继 · 管理后台代理          │◀───────│  audit · billing · dlp   │
              └───┬───────────────────────────────┘        │  llm-gateway · storage   │
                  │                                        │  crypto · feature-flag … │
                  │                                        └──────────────────────────┘
   ┌──────────────┼────────────────┬──────────────┬───────────────┬──────────────┐
   ▼              ▼                ▼              ▼               ▼              ▼
session-      app-registry    skill-store    mcp-registry    knowledge-     cron-runner
orchestrator     :8014           :8011          :8010          ingest          :8015
   :8006       应用：带版本      技能包         租户 MCP        :8012         定时任务 +
 会话 · 消息    harness +       （tarball）     服务         文档 → pgvector    Webhook
 运行 · 画布    审核/发布
 工作区
   │
   ▼
agent-scheduler :8007 ───────────────▶ sandbox-pool :8008
每次运行拉起一个沙箱                    预热的 CubeSandbox microVM 池
   │
   ├── runtime: docker  （开发）
   └── runtime: cube    （microVM，内核级隔离）
              │
              ▼
        ┌───────────────────────────────────────────────┐
        │  沙箱内的 Agent Worker                        │
        │  • worker-claude-sdk   (TS, Claude Agent SDK) │
        │  • agentscope-ext      (Py, AgentScope 2.x)   │
        └───────┬───────────────────────────┬───────────┘
                │                           │
                ▼                           ▼
      tool-broker :8024           web-fetch-gateway :8023
      访问 foundation 工具        受控出网：白名单 + 默认拒绝
      的策略门面                  + SSRF 防护
```

**一轮 Agent 对话的链路**：浏览器 → BFF（鉴权、解析租户）→ session-orchestrator（落库消息、
开启 run）→ agent-scheduler（申请沙箱、签发短期 token 与 per-run LLM 虚拟密钥）→ worker
（驱动 Agent 循环、经 broker 调用工具）→ 事件经 Redis 回流 → SSE → 浏览器。

### 服务清单

| 服务 | 宿主端口 | 职责 |
|---|---|---|
| `web` | 5173 | React 19 单页应用 —— 对话、应用、资源库、工作区、管理后台 |
| `bff` | 8005 | 鉴权代理、API 聚合、SSE 中继、管理后台分发 |
| `session-orchestrator` | 8006 | 会话、消息、运行、画布、工作区、会话内工具循环 |
| `agent-scheduler` | 8007 | 拉起沙箱容器/microVM，注入凭据 |
| `sandbox-pool` | 8008 | 预热的 CubeSandbox microVM 池 |
| `mcp-registry` | 8010 | 租户 MCP 服务注册表（密钥加密、工具过滤） |
| `skill-store` | 8011 | 带版本的技能包 |
| `knowledge-ingest` | 8012 | 文档 → 向量 → pgvector 管道 |
| `app-registry` | 8014 | 应用：版本化 harness、审核、发布 |
| `cron-runner` | 8015 | 定时与 Webhook 触发的应用执行 |
| `web-fetch-gateway` | 8023 | Agent 工具的受控出网 HTTP（SSRF 防护） |
| `tool-broker` | 8024 | 沙箱访问 foundation 的策略门面 |

---

## 仓库结构

```
agent-platform/
├── apps/web/              React 19 单页应用（Vite、Zustand、Tailwind）
│   └── src/admin/         28 个模块的管理后台
├── services/              FastAPI 服务（Python 3.12、Poetry）—— 见上方清单
├── worker-claude-sdk/     TypeScript Agent worker —— Claude Agent SDK
├── agentscope-ext/        Python Agent worker + AgentScope 扩展
│                          （画布/知识库/联网工具、网关强制器、
│                            MCP 租户注入器、技能加载器、审计 sink）
├── sandbox-images/        沙箱 Dockerfile —— base / office / MATLAB 分层
├── libs/                  共享客户端库（特性开关、配额守卫）
├── contracts/             OpenAPI 3.1 契约 + worker 线协议
├── deploy/
│   ├── docker-compose/    开发栈
│   ├── helm/              Kubernetes chart
│   ├── cube/              CubeSandbox controller 配置
│   └── scripts/           证书生成、运维脚本
├── docs/
│   ├── adr/               架构决策记录
│   ├── design/            滚动细化的设计文档
│   └── runbook/           备份恢复、压测、Cube 启用
└── tests/conformance/     跨服务一致性测试套件
```

---

## 快速开始

**前置条件**：Docker（含 Compose v2）、Python 3.12 + Poetry、Node 18+，以及作为兄弟目录存在的
[saas-foundation](https://github.com/chenzefeng09/saas-foundation) 检出。

```bash
git clone https://github.com/chenzefeng09/saas-foundation.git
git clone https://github.com/chenzefeng09/agent-platform.git
cd agent-platform
```

```bash
make env
```

这会从示例生成 `deploy/docker-compose/.env`。编辑它，至少要设置 `BFF_COOKIE_SECRET`
（必须 ≥32 字符，弱值会导致启动直接失败）：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

然后拉起两个栈并构建沙箱镜像：

```bash
make up
```

```bash
make build-claude-sdk-image
```

浏览器打开 <http://localhost:5173>。开发用的种子账号来自 foundation 栈，见其
`deploy/docker-compose/README.md`。

常用命令：

```bash
make ps
```

```bash
make logs SVC=bff
```

```bash
make down
```

若想在 Docker 之外跑前端、连容器化的后端：

```bash
make web-install && make web-dev
```

---

## 配置

配置集中在 `deploy/docker-compose/.env`（从 `.env.example` 复制）。最关键的几项：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `FOUNDATION_NETWORK` | `saas-foundation-dev_foundation` | 与 foundation 栈共享的外部 Docker 网络 |
| `FOUNDATION_SDK_PATH` | `../../../saas-foundation/sdks/python` | 兄弟目录的 SDK 源码，作为构建上下文注入 |
| `BFF_COOKIE_SECRET` | *（空 —— 必须自行设置）* | 会话 Cookie 签名密钥，≥32 字符 |
| `SCHEDULER_RUNTIME` | `docker` | 开发用 `docker`，microVM 隔离用 `cube` |
| `CLAUDE_SDK_IMAGE` / `AGENT_IMAGE` | 见 `.env.example` | 各 worker 形态对应的沙箱镜像 |
| `POOL_IDLE_LOW` / `POOL_IDLE_HIGH` / `POOL_MAX_TOTAL` | `2` / `10` / `50` | microVM 池容量 |
| `CUBE_CONTROLLER_URL`、`CUBE_TLS_MODE`、`CUBE_API_KEY` | — | CubeSandbox controller（`SCHEDULER_RUNTIME=cube` 时必填） |

启用 microVM 运行时是一套独立流程，见
[docs/runbook/enable-cubesandbox.md](docs/runbook/enable-cubesandbox.md)。

---

## 开发

```bash
make bootstrap     # 为每个服务执行 poetry install，并可编辑安装 foundation SDK
```

```bash
make lint          # 全服务 ruff check + mypy
```

```bash
make test          # 全服务 pytest + 覆盖率
```

```bash
make migrate       # alembic upgrade head（session-orchestrator）
```

```bash
make conformance   # 对运行中的栈执行一致性测试
```

前端在 `apps/web/` —— `npm run typecheck`、`npm run lint`、`npm run test`（vitest）、
`npm run build`。

Worker 在 `worker-claude-sdk/` —— `npm run typecheck`、`npm run test`，以及产出 `dist/`
供沙箱镜像构建使用的 `npm run build`。

GitHub Actions 的分服务流水线位于 `.github/workflows/`。

**注意**：compose 栈里大多数后端服务**不支持热重载**。改完服务代码后需重建：
`make agent-up`（内部会带 `--build`）。

---

## 沙箱镜像

`sandbox-images/` 负责构建 Agent 运行所在的镜像，分层组织：

```
base            Python 3.12 · Agent worker · 非 root uid 1000
                办公文档库 · 科学计算 Python · Noto CJK 字体
  └─ office     + LibreOffice headless（格式转换，如 docx → pdf）
       └─ matlab  + MATLAB（Control System / Aerospace 工具箱分层）
```

**默认镜像本身就具备完整办公能力 —— 不需要额外分层，也不需要任何授权。**
`claude-sdk.Dockerfile` 与 `claude-sdk-cube.Dockerfile` 已预装 `python-docx`、
`openpyxl`、`python-pptx`、`pypdf`、`Pillow`、`lxml`、`xlsxwriter`，可读写
Word / Excel / PowerPoint / PDF；另有 `pandas`、`numpy`、`matplotlib`、`graphviz`、
`diagrams` 用于分析与绘图。镜像还装了 `fonts-noto-cjk` 并写入 `matplotlibrc`，
中文标签能正常渲染而不是变成豆腐块。这些全部是开源包，来自 PyPI / apt。
构建阶段会断言这些库和 CJK 字体确实可用 —— 镜像坏了会在 build 时失败，而不是运行到一半才炸。

独立的 `office` 分层在此之上加 **LibreOffice headless**，只在需要真正的格式**转换**
（docx → pdf、xlsx → csv，走 `unoconv`）时才用；单纯生成文档用默认镜像即可。

```bash
make build-claude-sdk-image
```

```bash
make build-claude-sdk-cube-image
```

> **MATLAB 分层需自备授权。** 本仓库不包含、也不得再分发任何 MATLAB 安装介质、许可证文件或
> 二进制。`matlab*.Dockerfile` 相关目标要求你把 `MATLAB_ISO_DIR` 指向自己合法授权的安装介质。
> 平台其余部分不依赖它们 —— 完全跳过这些构建目标，上面所有能力照常可用。

完整分层矩阵见 [sandbox-images/README.md](sandbox-images/README.md)。

---

## 安全模型

模型生成的代码全程按不可信处理：

- **内核级隔离** —— 生产运行落在 CubeSandbox microVM，而非共享内核的容器
  （[ADR 0001](docs/adr/0001-sandbox-runtime-cube-default-kata-fallback.md)）。
- **默认拒绝的出网** —— 沙箱没有直接互联网路由。出站 HTTP 必须经 `web-fetch-gateway`，
  由其执行白名单并拦截 SSRF（含 fake-IP / DNS 重绑定类攻击）。
- **工具访问经 broker** —— 沙箱只能通过 `tool-broker` 触达 foundation 服务，由其执行策略，
  且不下发租户凭据；调用第三方 MCP 时会剥离入站 `Authorization` 头。
- **短时、单次运行的凭据** —— scheduler 为每次运行签发 run 级 token 与 LLM 虚拟密钥；
  密钥以文件下发后 unlink，而不是留在进程环境变量里 —— Agent 去读 `/proc/*/environ` 也拿不到。
- **DLP 拦截** —— 上传、下载与工具调用的内容都会按 foundation DLP 规则校验。
- **数据密级** —— 文件、工作区与用户都带密级，访问需满足 `文件 ≤ 工作区 ≤ 用户密级`。
- **审计与回放** —— 运行事件汇入 foundation 审计服务；会话回放受独立权限管控。

发现安全问题？请通过
[GitHub security advisory](https://github.com/chenzefeng09/agent-platform/security/advisories/new)
私下报告，不要开公开 issue。

---

## 文档

| 主题 | 文档 |
|---|---|
| 沙箱运行时决策 | [ADR 0001 —— CubeSandbox 默认、Kata 兜底](docs/adr/0001-sandbox-runtime-cube-default-kata-fallback.md) |
| Agent 内核决策 | [ADR 0002 —— 选 AgentScope 而非 OpenHands](docs/adr/0002-agent-kernel-agentscope-over-openhands.md) |
| 沙箱运行时设计 | [docs/design/b-m2-sandbox-runtime-v1.md](docs/design/b-m2-sandbox-runtime-v1.md) |
| foundation SDK 接入 | [docs/foundation-sdk-consumption.md](docs/foundation-sdk-consumption.md) |
| 管理后台规格 | [docs/admin-console-v2-spec.md](docs/admin-console-v2-spec.md) |
| 画布设计 | [docs/canvas-v2-design.md](docs/canvas-v2-design.md) |
| 知识库 | [docs/knowledge-library-v1.md](docs/knowledge-library-v1.md) |
| DLP 防线方案 | [docs/dlp-real-defense-plan.md](docs/dlp-real-defense-plan.md) |
| 启用 CubeSandbox | [docs/runbook/enable-cubesandbox.md](docs/runbook/enable-cubesandbox.md) |
| 备份与恢复 | [docs/runbook/backup-restore-b-m6.md](docs/runbook/backup-restore-b-m6.md) |
| 压力测试 | [docs/runbook/loadtest-b-m6.md](docs/runbook/loadtest-b-m6.md) |
| Worker 线协议 | [contracts/worker-wire-contract.md](contracts/worker-wire-contract.md) |

部分设计文档为英文。

---

## 参与贡献

欢迎提 issue 和 PR。

- 保持 `make lint` 与 `make test` 通过；前端改动需通过 `npm run typecheck` 与 `npm run build`。
- 跨 agent-platform / saas-foundation 边界的改动需要在另一个仓库提配套 PR —— 请在描述中说明。
- 架构性改动请附带 `docs/adr/` 下的 ADR。
- 贡献内容按 Apache 2.0 接受（见下）。

---

## 许可证

采用 **Apache License 2.0** —— 见 [LICENSE](LICENSE)。

```
Copyright 2026 The agent-platform Authors

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

第三方组件遵循各自许可证。其中本项目集成了
[AgentScope](https://github.com/agentscope-ai/agentscope)（Apache 2.0）与
[Claude Agent SDK](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk)
（条款由 Anthropic 规定）。若构建 MATLAB 分层镜像，需自行向 MathWorks 取得授权。
