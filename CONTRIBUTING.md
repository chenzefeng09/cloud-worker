# Contributing to Agent Platform

Thanks for your interest in contributing! Agent Platform is an isolated, multi-tenant
runtime for AI agents, built on top of [SaaS Foundation](https://github.com/chenzefeng09/saas-foundation).
This guide covers how to set up a local development environment, the day-to-day make
targets, and the conventions we follow for branches, commits, pull requests, and
architectural changes.

By participating in this project, you are expected to interact respectfully and
constructively with other contributors. Report unacceptable behavior to
<chenzefeng09@gmail.com>.

## Table of contents

- [Repository layout](#repository-layout)
- [Prerequisites](#prerequisites)
- [Development environment setup](#development-environment-setup)
- [Key make targets](#key-make-targets)
- [Running the stack](#running-the-stack)
- [Branch and pull-request conventions](#branch-and-pull-request-conventions)
- [Commit message style](#commit-message-style)
- [How CI works](#how-ci-works)
- [Proposing architectural changes (ADRs)](#proposing-architectural-changes-adrs)
- [License](#license)
- [中文贡献指南](#中文贡献指南)

## Repository layout

| Path | What lives here |
| --- | --- |
| `services/` | The Python backend services, each its own Poetry project (`bff`, `session-orchestrator`, `agent-scheduler`, `sandbox-pool`, `app-registry`, `skill-store`, `mcp-registry`, `tool-broker`, `knowledge-ingest`, `cron-runner`, `web-fetch-gateway`). |
| `apps/web/` | The React 19 + Vite frontend (the operator/user console). |
| `worker-claude-sdk/` | The TypeScript/Node sandbox worker built on the Claude Agent SDK — the only agent runtime. Honors the `task.json` → `result.json` contract in [`contracts/worker-wire-contract.md`](./contracts/worker-wire-contract.md). |
| `sandbox-images/` | OCI Dockerfiles for the `claude-sdk*` sandbox images (Docker dev image, Cube-native image, MATLAB layers, worker refresh). |
| `libs/feature-flag-client/` | Shared feature-flag client used by several services. |
| `contracts/openapi/` | OpenAPI contracts (linted in CI). |
| `deploy/` | Docker Compose, Helm chart, KEDA operator, CubeSandbox egress policy, backup/load-test scripts. |
| `docs/` | Architecture, ADRs (`docs/adr/`), design notes, and runbooks. |
| `tests/conformance/` | Cross-service conformance tests. |

## Prerequisites

Agent Platform is built **on top of** SaaS Foundation and cannot build, start, or run
without a sibling checkout of it. Make sure you have:

- **GNU make + bash** — the top-level `Makefile` runs with `bash -eu -o pipefail`.
- **Docker + Docker Compose** (Compose v2 / `docker compose`).
- **A sibling `saas-foundation` checkout at `../saas-foundation`.** Agent Platform
  imports `foundation_sdk` and calls Foundation services (identity / authz / billing /
  audit / LLM gateway) at runtime. Clone it next to this repo:

  ```bash
  git clone https://github.com/chenzefeng09/saas-foundation.git ../saas-foundation
  ```

- **Python 3.12 + [Poetry](https://python-poetry.org/)** — every backend service has its
  own Poetry-managed virtualenv.
- **Node.js 20+ / npm** — for the frontend (Vite 6). Node 22 is used for the
  `worker-claude-sdk` build.
- **PostgreSQL 16 + pgvector, Redis, MinIO/S3, Keycloak** — these are provided by the
  Foundation dev stack, not by this repo.

Optional, for specific workflows:

- **A Linux host with `/dev/kvm`** plus the CubeSandbox controller image, only if you
  want to exercise microVM sandboxes locally (see `docs/runbook/enable-cubesandbox.md`).
- **Kubernetes 1.27+, Helm, KEDA v2.13+** for the Helm-based deployment path.

> **Note:** the `Makefile` resolves the Foundation checkout through `FOUNDATION_DIR`.
> If your sibling checkout lives somewhere other than `../saas-foundation`, override it:
> `make bootstrap FOUNDATION_DIR=/path/to/saas-foundation`.

## Development environment setup

Each backend service is an independent Poetry project, and the shared `foundation_sdk`
lives in the sibling repo (it is not on public PyPI yet). The bootstrap target installs
both for you.

```bash
# 1. Clone the sibling Foundation repo (if you have not already)
git clone https://github.com/chenzefeng09/saas-foundation.git ../saas-foundation

# 2. Install every service's deps + an editable foundation_sdk into each venv
make bootstrap
```

`make bootstrap` does two things:

1. Runs `poetry install` for every service under `services/`.
2. Runs `make install-sdk`, which `pip install -e`'s
   `foundation_sdk[fastapi]` from `$(FOUNDATION_DIR)/sdks/python` into **each service's
   Poetry venv**, so local development and CI behave the same way. You must re-run
   `make install-sdk` after a fresh `poetry install` in any service.

For the frontend:

```bash
make web-install   # npm install in apps/web
make web-dev       # start the Vite dev server (http://localhost:5173)
```

## Key make targets

Run `make help` to see the full list. The targets you will use most:

| Target | What it does |
| --- | --- |
| `make bootstrap` | Full local-dev bootstrap: `poetry install` for every service + editable `foundation_sdk` into each venv. |
| `make install-sdk` | (Re)install the editable `foundation_sdk[fastapi]` into each service venv. |
| `make lint` | Quality gate: `ruff check` **+** `mypy src/` across all services. |
| `make format` | Auto-format with `ruff format` across all services. |
| `make test` | Run `pytest` (with coverage) across all services. |
| `make conformance` | Run the cross-service conformance tests against the running stack (variants: `conformance-docker`, `conformance-static`). |
| `make migrate` | Run Alembic migrations for `session-orchestrator`. |
| `make web-install` / `make web-dev` | Install frontend deps / start the Vite dev server. |
| `make build-claude-sdk-image` | Compile `worker-claude-sdk` (`npm ci && npm run build`) then build the `claude-sdk-sandbox` image. |

Before opening a pull request, please run the quality gate locally:

```bash
make format
make lint     # ruff + mypy
make test     # pytest
```

For frontend changes, run the equivalent in `apps/web`:

```bash
cd apps/web
npm run lint
npm run typecheck
npm test
```

## Running the stack

```bash
make env       # create deploy/docker-compose/.env from the example (edit before use)
make up        # start the Foundation dev stack + all agent services
```

This brings up the agent services (BFF `:8005`, session-orchestrator `:8006`,
agent-scheduler `:8007`, sandbox-pool `:8008`, tool-broker `:8024`, and others) plus the
web console on `:5173`. If Foundation is already running, use `make agent-up` to start
only the agent services. Useful helpers: `make ps`, `make logs SVC=bff`, `make down`,
`make nuke` (stop everything and destroy volumes).

By default sandboxes run as sibling Docker containers (`SCHEDULER_RUNTIME=docker`) in
broker egress mode — no KVM required. See `docs/ARCHITECTURE.md` and the runbooks for the
CubeSandbox microVM and Kubernetes paths.

## Branch and pull-request conventions

- **Branch off `main`.** Use a short, descriptive branch name with a type prefix, e.g.
  `feat/skill-store-pagination`, `fix/scheduler-lease-expiry`, `docs/security-policy`,
  `chore/bump-ruff`.
- **Keep pull requests focused.** One logical change per PR. Touch a single service where
  possible — CI is per-service, and small PRs review faster.
- **Describe the change.** In the PR description, explain *what* changed and *why*, link
  any related issue, and call out user-facing or API-contract impact. If you change an
  OpenAPI contract under `contracts/openapi/`, mention it.
- **Update docs and tests** alongside code. New behavior should ship with tests; changed
  behavior should update the relevant docs/runbooks.
- **Green CI is required.** All per-service workflows that your change triggers must pass
  before review/merge.
- **Update the changelog.** Add a bullet to the `[Unreleased]` section of
  [`CHANGELOG.md`](./CHANGELOG.md) for any user-visible change.

## Commit message style

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<optional scope>): <short summary>

<optional body explaining what and why>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `ci`,
`build`. Scope is usually the service or area, e.g.:

```
feat(skill-store): paginate the versions listing endpoint
fix(tool-broker): reject leases with an expired exp claim
docs(security): add private vulnerability-reporting policy
```

Keep the summary in the imperative mood and under ~72 characters. Reference issues in the
body (`Closes #123`) rather than the summary line.

## How CI works

CI is **per-service**: each service (and the frontend and the OpenAPI contracts) has its
own GitHub Actions workflow under `.github/workflows/`, scoped with `paths:` filters so a
change to one service only runs that service's pipeline.

- **Python service workflows** (e.g. `ci-bff.yml`, `ci-agent-scheduler.yml`,
  `ci-session-orchestrator.yml`, `ci-sandbox-pool.yml`, `ci-skill-store.yml`,
  `ci-mcp-registry.yml`, `ci-knowledge-ingest.yml`) run three jobs:
  - **lint** — `ruff check .` and `mypy src/`.
  - **test** — `pytest` with coverage (services enforce a coverage floor, e.g.
    `--cov-fail-under=80`).
  - **security** — `gitleaks` secret scanning and a `pip-licenses` check that fails on
    copyleft (`GPL`/`AGPL`) dependencies.

  These workflows use a shared composite action (`.github/actions/setup-service`) that
  checks out the Foundation repo and installs `foundation_sdk`, so CI mirrors the local
  `make bootstrap` flow.

- **`ci-worker-claude-sdk.yml`** runs `npm ci`, `npm run typecheck`, `npm test` and
  `npm run build` for `worker-claude-sdk/`.

- **`ci-frontend.yml`** runs `npm run lint`, `npm run typecheck`, `npm test` (Vitest),
  and `npm run build` for `apps/web` on Node 22.

- **`ci-contracts.yml`** lints the OpenAPI contracts in `contracts/openapi/` with
  Spectral, failing the build on `error`-severity findings.

Because workflows are path-filtered, make sure your change lives under the path the
relevant workflow watches; otherwise its checks will not run.

## Proposing architectural changes (ADRs)

Significant or hard-to-reverse decisions are recorded as **Architecture Decision Records**
in [`docs/adr/`](./docs/adr/). Existing records include the choice of sandbox runtime
(`0001-sandbox-runtime-cube-default-kata-fallback.md`), the original agent kernel
(`0002-agent-kernel-agentscope-over-openhands.md`, superseded) and the current agent
runtime (`0003-claude-agent-sdk-sole-agent-runtime.md`).

To propose an architectural change:

1. Copy the numbering/format of an existing ADR and create
   `docs/adr/NNNN-short-title.md` (next sequential number).
2. Capture **Context**, **Decision**, **Alternatives considered**, and **Consequences**.
3. Open a pull request with the ADR. Discussion happens on the PR; once accepted, the ADR
   is merged with status **Accepted**. Superseded ADRs are not deleted — they are marked
   superseded and link to the record that replaces them.

Open an ADR PR before (or alongside) a large implementation PR so the design can be
agreed first.

## License

By contributing, you agree that your contributions will be licensed under the project's
[Apache License 2.0](./LICENSE). See also the [`NOTICE`](./NOTICE) file.

---

## 中文贡献指南

感谢你为 Agent Platform 做出贡献！Agent Platform 是一个面向 AI Agent 的隔离式多租户运行时，
构建在 [SaaS Foundation](https://github.com/chenzefeng09/saas-foundation) 之上。参与本项目即表示
你愿意以尊重、友善、建设性的态度与其他贡献者协作；如遇不当行为，请邮件联系 <chenzefeng09@gmail.com>。

**前置条件**

- GNU make + bash、Docker 与 Docker Compose v2。
- 在 `../saas-foundation` 处放置一个并列的 `saas-foundation` 检出（Agent Platform 依赖
  `foundation_sdk` 并在运行时调用 Foundation 的身份/授权/计费/审计/LLM 网关服务）。
- Python 3.12 + Poetry；Node.js 20+ / npm（前端）。
- PostgreSQL 16 + pgvector、Redis、MinIO/S3、Keycloak 由 Foundation 开发栈提供。

**环境搭建**

```bash
git clone https://github.com/chenzefeng09/saas-foundation.git ../saas-foundation
make bootstrap     # 为每个服务执行 poetry install，并把可编辑的 foundation_sdk 装入各自的 venv
make web-install   # 安装前端依赖
```

每个后端服务都是独立的 Poetry 工程；共享的 `foundation_sdk` 来自并列的 Foundation 仓库，
由 `make install-sdk` 以可编辑方式安装到每个服务的虚拟环境中。

**常用 make 目标**

- `make lint` — 全部服务执行 `ruff check` 与 `mypy`。
- `make format` — 使用 `ruff format` 格式化。
- `make test` — 全部服务执行 `pytest`（带覆盖率）。
- `make conformance` — 针对运行中的服务栈执行一致性测试。
- `make up` / `make agent-up` — 启动 Foundation + 全部服务 / 仅启动 Agent 服务。

提交 PR 前请在本地依次运行 `make format`、`make lint`、`make test`（前端在 `apps/web` 下运行
`npm run lint`、`npm run typecheck`、`npm test`）。

**分支与提交规范**

- 从 `main` 切出分支，使用带类型前缀的简短分支名（如 `feat/...`、`fix/...`、`docs/...`）。
- 遵循 [Conventional Commits](https://www.conventionalcommits.org/) 提交规范，例如
  `fix(tool-broker): reject leases with an expired exp claim`。
- 保持 PR 聚焦，单一逻辑变更；补充测试与文档；为用户可见的变更在 `CHANGELOG.md` 的
  `[Unreleased]` 区块新增条目。CI 必须全部通过。

**CI 工作方式**

CI 按服务划分：每个服务、前端以及 OpenAPI 契约各有独立的 GitHub Actions 工作流（带 `paths:`
过滤）。Python 服务工作流包含 lint（ruff + mypy）、test（pytest + 覆盖率）、security
（gitleaks + 许可证检查）三个作业。

**架构变更（ADR）**

重要的架构决策记录在 [`docs/adr/`](./docs/adr/)。如需提出架构变更，请按现有 ADR 的编号与格式新增
`docs/adr/NNNN-标题.md`，写明背景、决策、备选方案与影响，并通过 PR 讨论与合入。

本项目以 [Apache-2.0](./LICENSE) 许可证发布；提交贡献即表示同意以该许可证授权。
