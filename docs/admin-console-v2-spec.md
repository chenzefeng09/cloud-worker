# 企业级管理后台 v2 — 实施规格

> 目标：把 saas-foundation 底座与 agent-platform 平台服务中"只有 API 没有界面"的企业级能力补成管理后台模块。
> 本文档是 BFF 与前端实现的统一契约。所有新 BFF 端点挂在 `/api/bff/v1` 下，全部经 `require_super_admin_bff` 守卫（复用 `agent_bff/api/v1/admin.py` 中的依赖与 `_raise_for_upstream` / `foundation_headers` / `admin_headers` 模式）。

## 跨租户操作约定

super_admin 属于 Tenant A，但要管理任意租户。凡下游为租户作用域（RLS by `X-Tenant-Id`）的端点，BFF 接受可选 query 参数 `tenant_id`：传了则转发头 `X-Tenant-Id` 用该值覆盖（caller 已通过 platform.admin 校验，安全）；不传则用 caller 自己的 tenant。响应中回填 `effective_tenant_id`（对象响应时）或由前端自行记录（透传列表时不强求）。

## 模块与 BFF 端点

### 1. RBAC（→ foundation-authz，`settings.authz_url`）
文件：`api/v1/admin_authz.py`
- `GET  /admin/authz/roles` / `POST /admin/authz/roles`
- `GET/PATCH/DELETE /admin/authz/roles/{role_id}`
- `GET  /admin/authz/permissions`（权限目录，支持 query 透传如 service）
- `GET  /admin/authz/bindings` / `POST /admin/authz/bindings` / `DELETE /admin/authz/bindings/{binding_id}`
实现前先读 `saas-foundation/contracts/openapi/authz.yaml` 与 authz 路由代码核对路径、鉴权头（是否需要 X-Actor）、响应包裹形式。

### 2. 组织架构（→ foundation-org，`settings.foundation_org_url`）
文件：`api/v1/admin_org.py`
- `GET/POST /admin/org/org-units`，`GET/PATCH/DELETE /admin/org/org-units/{id}`，`POST /admin/org/org-units/{id}/move`
- `GET/POST /admin/org/teams`，`GET /admin/org/teams/{id}/members`，`POST /admin/org/teams/{id}/members`，`DELETE /admin/org/teams/{id}/members/{user_id}`
核对 `contracts/openapi/org.yaml`。

### 3. 用户管理动作（→ foundation-identity）
文件：`api/v1/admin_identity.py`
- `POST /admin/users`（邀请）、`POST /admin/users/{id}/freeze`、`POST /admin/users/{id}/unfreeze`、`POST /admin/users/{id}/password:reset`
identity 的 /users* 是租户作用域端点；核对 identity 路由确认 super_admin 跨租户调用的正确方式（tenant_id 覆盖头）。若某动作底座没有对应端点，跳过并在交付说明中写明。

### 4. 计费与配额可写（→ foundation-billing）
文件：`api/v1/admin_billing.py`（不要动 admin.py 里已有的 GET /admin/billing/quota）
- `GET /admin/billing/plans`
- `GET /admin/billing/subscription?tenant_id=`（当前订阅）
- `POST /admin/billing/subscription/change-plan`、`POST /admin/billing/subscription/cancel`
- `GET /admin/billing/usage?tenant_id=&...`（用量查询，参数透传）
核对 `contracts/openapi/billing.yaml`（change-plan 实际路径形如 /subscriptions/{id}/change-plan，BFF 可先查 current 再转发，或直接要求 body 带 subscription_id —— 以底座契约为准）。

### 5. 审计增强（→ foundation-audit）
文件：`api/v1/admin_audit.py`（admin.py 已有 events:query，不要动）
- `POST /admin/audit/chain:verify`（→ /chain/verify）
- `POST /admin/audit/export`、`GET /admin/audit/export/{id}`

### 6. Feature Flag（→ foundation-feature-flag，dev compose 已运行）
文件：`api/v1/admin_flags.py`；config.py 新增 `foundation_feature_flag_url`（参考 foundation 该服务 api_prefix=/api/feature-flag/v1、port 8012；以 dev.yml 实际端口映射为准），compose 的 bff 环境变量同步加。
- `GET /admin/flags`，`GET/PUT /admin/flags/{key}`，`POST /admin/flags/{key}/kill-switch`

### 7-10. 平台治理（→ agent-platform 自有服务，URL 均已在 config.py）
文件：`api/v1/admin_platform.py`
- 技能：`GET /admin/skills`、`GET /admin/skills/{name}`、`GET /admin/skills/{name}/versions`、`DELETE /admin/skills/{name}`（→ skill_store_url）
- MCP：`GET/POST /admin/mcp/servers`，`GET/PATCH/DELETE /admin/mcp/servers/{id}`（→ mcp_registry_url）
- 知识库：`GET /admin/knowledge/documents`、`GET /admin/knowledge/documents/{id}`、`POST /admin/knowledge/documents/{id}/reingest`（→ knowledge_ingest_url；删除仅在下游支持时提供）
- 运行治理：`GET /admin/ops/runs`（→ scheduler /runs，参数透传）、`POST /admin/ops/runs/{run_id}/cancel`、`GET /admin/ops/sandboxes`、`POST /admin/ops/sandboxes/{id}/stop`、`GET /admin/ops/pool`（→ sandbox-pool /_pool 快照）、`GET /admin/ops/usage/daily`（→ orchestrator /runs/token-usage/daily）
以各服务实际路由为准（先读对应服务 api/v1 源码）。

## 失败语义

- 下游 4xx/5xx：透传 status + RFC9457 body（沿用 `_raise_for_upstream`）。
- 下游服务不可达（ConnectError/Timeout）：返回 502，detail 说明哪个服务；前端用 QuotaPage 的 `isTodoEndpoint` 同款兜底展示"该能力服务未启用"。

## 前端

- `api/client.ts` 在 `api.admin` 下新增命名空间：`authz`、`org`、`billing`（扩展）、`audit`（扩展 verifyChain/export）、`flags`、`skills`、`mcp`、`knowledge`、`ops`；`admin.users` 增加 invite/freeze/unfreeze/resetPassword。类型加到 `api/types.ts`（对照底座 openapi，宽松可选字段）。
- 新页面（`apps/web/src/admin/pages/`）：`RbacPage.tsx`、`OrgStructurePage.tsx`、`FeatureFlagsPage.tsx`、`SkillsAdminPage.tsx`、`McpServersPage.tsx`、`KnowledgeAdminPage.tsx`、`OpsDashboardPage.tsx`；增强现有 `UsersPage.tsx`（动作列）、`QuotaPage.tsx`（改为计费与配额：套餐目录 + 订阅变更 + 用量）、`AuditPage.tsx`（链校验 + 导出）。
- `AdminLayout.tsx` 导航分组：
  - 租户与组织：租户管理、组织架构
  - 用户与权限：用户管理、角色与权限
  - 计费与配额：计费与配额
  - 模型与 AI：模型管理、默认模型设置
  - 平台治理：应用审核、技能管理、MCP 服务器、知识库、运行与沙箱
  - 运营与合规：Feature Flags、审计日志、会话回放
- 风格沿用现有 admin 页面（TenantsPage/ModelManagementPage 的表格、抽屉/弹窗、错误兜底模式），中文文案。

## 不做（本轮）

notification / api-platform / pam / dlp / compliance / lifecycle 管理界面 —— 对应 foundation 服务未进 dev compose，无法端到端验证；列入后续路线图。

## 验收

1. `services/bff` pytest 全绿（每个新 router 有代理测试：鉴权 403 / 透传 / 下游错误透传，仿照 tests/test_admin_proxy.py 的 stub 模式）。
2. `apps/web` vite build 通过。
3. 重建 bff 容器后 curl 新端点未认证返回 401（非 404）。
4. Playwright 以 admin@foundation.local 登录逐页验收，真实数据渲染。
