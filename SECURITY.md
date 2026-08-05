# Security policy

Agent Platform runs untrusted, model-generated code on behalf of tenants, so we take
security seriously. This document explains which versions receive security fixes, how to
report a vulnerability privately, and the high-level security model the platform relies
on.

> English | [中文](#中文安全策略)

## Supported versions

Agent Platform is pre-1.0. Security fixes are provided for the latest `0.1.x` release line
only.

| Version | Supported |
| --- | --- |
| `0.1.x` | :white_check_mark: |
| `< 0.1` | :x: |

As later release lines appear, this table will be updated; older lines stop receiving
fixes once superseded.

## Reporting a vulnerability

**Please do not open a public GitHub issue, pull request, or discussion for security
vulnerabilities.** Public disclosure before a fix is available puts users at risk.

Instead, report it privately by email to **chenzefeng09@gmail.com**. Where possible,
include:

- A description of the vulnerability and its impact.
- The affected service(s)/component(s) and version or commit.
- Steps to reproduce, a proof of concept, or a minimal example.
- Any suggested remediation, if you have one.

If you believe the issue also affects the underlying
[SaaS Foundation](https://github.com/chenzefeng09/saas-foundation) project (identity,
authz, billing, audit, or the LLM gateway), please say so — several controls span both
projects.

### What to expect

- **Acknowledgement** of your report within **3 business days**.
- An initial **assessment and severity triage** within **7 business days**.
- Regular updates as we investigate and prepare a fix; we will coordinate a disclosure
  timeline with you.
- **Credit** for the discovery once a fix ships, unless you prefer to remain anonymous.

We ask that you give us a reasonable opportunity to remediate before any public
disclosure, and that your research avoids privacy violations, data destruction, and
service degradation for other users.

## Security model

Agent Platform is designed so that untrusted agent code is contained by multiple
independent layers rather than any single control. A summary of the load-bearing controls
follows; the authoritative description lives in [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)
and the sandbox-runtime design and ADRs under `docs/`.

- **MicroVM sandbox isolation.** Model-generated code runs inside a per-run CubeSandbox
  microVM (default) or Kata microVM (fallback), not a host-kernel-sharing container;
  Docker is used only for local development and CI. One agent runs one task per sandbox,
  so the per-run microVM is the tenant isolation, audit, and billing boundary.
- **Default-deny broker egress.** In broker mode a sandbox joins only an internal
  network and can reach **only** the tool-broker façade plus DNS — not Redis, object
  storage, the identity provider, the public internet, or cloud metadata endpoints.
  Egress is enforced at the microVM layer (eBPF allow-list overlay with a host-firewall
  fallback); an empty allow-list fails closed.
- **Per-run capability leases.** The scheduler signs a short-lived HMAC lease per run.
  Every call through the tool-broker (LLM, MCP, skills, web-fetch, state, object storage)
  must carry it; the broker verifies tenant/user/run/scope/expiry and strips any
  sandbox-supplied identity headers. Real infrastructure credentials live only on the
  broker — sandboxes never hold them.
- **Short-lived, scope-limited tokens.** Each run is issued a short-lived, scope-limited
  Foundation JWT. Control-plane secrets are delivered to the sandbox via a mode-0600
  file that the worker loads into userspace and immediately unlinks, keeping them out of
  the container environment and `/proc/1/environ`.
- **LLM-gateway enforcement.** All sandbox LLM traffic is routed through the Foundation
  LLM gateway with a per-tenant virtual key (quota, audit, model routing). When enabled,
  no raw provider key is injected, and a missing virtual key fails the run rather than
  falling back to direct credentials.
- **Data Loss Prevention (DLP).** A default rule library detects sensitive data (IDs,
  phone/bank-card numbers, emails, cloud keys, private keys, API keys) and is enforced on
  attachment upload, on download, on run-artifact persistence, and on output/transcript
  paths, with results recorded for the governance console.
- **SSRF-controlled web egress.** The web-fetch gateway is the only service with outbound
  internet access and enforces domain allow-listing (empty = fail-closed), private-range
  and metadata-IP blocking, content-type restrictions, response-size caps, and scope auth.

For the full data-flow, threat boundaries, and deployment hardening (least-privilege
Docker access, hardened pod security contexts, image supply chain), see
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md).

---

## 中文安全策略

> [English](#security-policy) | 中文

Agent Platform 会代表租户运行不可信的、由模型生成的代码，因此我们高度重视安全。本文档说明哪些版本
会收到安全修复、如何私密地报告漏洞，以及平台所依赖的总体安全模型。

### 受支持的版本

Agent Platform 处于 1.0 之前阶段，安全修复仅面向最新的 `0.1.x` 发布线。

| 版本 | 是否支持 |
| --- | --- |
| `0.1.x` | :white_check_mark: |
| `< 0.1` | :x: |

### 报告漏洞

**请不要为安全漏洞创建公开的 GitHub issue、PR 或讨论。** 请通过邮件私密报告至
**chenzefeng09@gmail.com**，并尽量提供：漏洞描述与影响、受影响的服务/组件及版本或提交、复现步骤或
PoC，以及（如有）修复建议。若该问题也影响底层的
[SaaS Foundation](https://github.com/chenzefeng09/saas-foundation) 项目，请一并说明。

预期响应时间：3 个工作日内确认收到，7 个工作日内完成初步评估与定级，并在调查与修复期间持续同步进度。
修复发布后我们会致谢报告者（除非你希望匿名）。请在我们完成修复前给予合理的处置时间，并避免侵犯隐私、
破坏数据或影响其他用户的服务。

### 安全模型

平台依靠多层相互独立的控制来约束不可信的 Agent 代码，权威描述见
[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md)：

- **微虚拟机沙箱隔离**：模型生成的代码运行在每次运行独立的 CubeSandbox 微虚拟机（默认）或 Kata 微虚拟机
  （后备）中，而非共享宿主内核的容器；Docker 仅用于本地开发与 CI。每个沙箱一个 Agent、一个任务，
  该微虚拟机即租户隔离、审计与计费边界。
- **默认拒绝的 Broker 出口**：在 broker 模式下沙箱仅加入内部网络，只能访问 tool-broker 门面与 DNS，
  无法访问 Redis、对象存储、身份提供方、公网或云元数据端点；出口在微虚拟机层强制（eBPF 白名单叠加，
  以宿主防火墙作为后备），空白名单时按失败关闭处理。
- **每次运行的能力租约**：调度器为每次运行签发短期 HMAC 租约，经 tool-broker 的每次调用都必须携带；
  broker 校验租户/用户/运行/作用域/有效期，并剥离沙箱自带的身份头。真实基础设施凭据仅存于 broker。
- **短期、限定作用域的令牌**：每次运行签发短期、限定作用域的 Foundation JWT；控制面密钥通过 0600
  权限文件投递，worker 载入用户态后立即删除，避免泄露至容器环境与 `/proc/1/environ`。
- **LLM 网关强制**：所有沙箱 LLM 流量经 Foundation LLM 网关并使用按租户的虚拟密钥（配额、审计、模型路由）；
  缺失虚拟密钥时按失败处理，不回退到直连凭据。
- **数据防泄露（DLP）**：默认规则库识别敏感数据（身份证、手机号/银行卡、邮箱、云密钥、私钥、API key），
  在附件上传、下载、运行产物持久化与输出/会话记录路径上强制执行，并记录至治理控制台。
- **SSRF 受控的网页出口**：web-fetch 网关是唯一具备公网出口的服务，强制域名白名单（空=失败关闭）、
  私有网段与元数据 IP 阻断、内容类型限制、响应大小上限与作用域鉴权。
