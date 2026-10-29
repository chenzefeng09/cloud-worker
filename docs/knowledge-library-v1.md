# 知识库 v1 设计（文件夹层级 + 密级 + 检索工具化）

> **历史说明（2026-10）**：本文中的"inline 工具循环"及其知识检索工具已随 inline 模式一并移除（见
> [ADR 0003](adr/0003-claude-agent-sdk-sole-agent-runtime.md)）。当前所有运行都由沙盒内的 Claude Agent SDK
> worker 执行，知识库只做每次运行一次的 top-k 预取（追加到 `sys_prompt`），按需检索工具尚未在新 worker 中提供。

> 状态：2026-07-19 定稿并实施中。本文档同时是三个并行实施任务的接口契约，实现与本文冲突时以本文为准（发现契约不合理先改本文再改代码）。

## 产品定位

知识源是**租户级资源库**：租户管理员上传维护，全租户用户在密级约束下检索使用。

- **无用户间共享体系**（v1 明确砍掉：无 share_grants、无工作区挂载、无指定用户共享）。
- 普通用户对资源库知识源 tab **只读**；上传、建文件夹、改密级、移动、删除均为租户管理员操作。
- 会话记忆文档（`origin_session_id`/`origin_user_id` 标记的 `[memory]` 文档）不受本设计影响，继续用户私有、默认隐藏。
- 预留 `uploaded_by_user_id`，将来若加"个人私人知识源"只需放开可见性过滤，不动表结构。

## 两道闸模型

- **可达性**：知识源全租户可达（`tenant_id` 界定）；记忆文档仅本人可达。
- **可见性**：达到者还必须 clearance ≥ 文档 sensitivity。密级枚举复用平台现有 `DlpSensitivity`（L1 < L2 < L3 < L4 < L5，见 apps/web/src/api/types.ts `DlpSensitivity` 与 orchestrator workspace 密级实现）。
- 两闸独立判断，检索、列表、预览、下载全部生效（列表也过闸——标题本身可能泄密）。

## 数据模型（knowledge-ingest）

新表 `folders`：
- `id`, `tenant_id`, `parent_id`（自引用 FK，NULL=根层）, `name`, `default_sensitivity`（默认 L1）, `created_by_user_id`, 时间戳。
- 同一 parent 下同名拒绝（409）。层级解析用 parent_id + 应用层/递归 CTE 即可（量级小，不用 ltree）。
- 删除仅允许空文件夹（无子文件夹且无文档），否则 409。

`documents` 加列：
- `folder_id`（nullable FK，NULL=根目录）、`sensitivity`（String(8)，默认 L1）、`uploaded_by_user_id`（nullable）。
- 回填：存量非 memory 文档 → `folder_id=NULL`、`sensitivity='L1'`。

密级规则（文件/文件夹两级设置）：
- 上传未指定 sensitivity → 取所在文件夹 `default_sensitivity`（无文件夹 → L1）。
- 文档移入 default 更高的文件夹 → 自动抬升 `sensitivity = max(doc, folder.default)`（只升不降，降级须显式 PATCH）。
- 修改文件夹 default 只影响之后的上传（v1 不做存量 bulk-apply）。

## knowledge-ingest API（/api/knowledge/v1）

身份与密级经 header 传递：沿用 ki 现有身份头约定（X-User-Id 等，以代码为准），新增 `X-User-Clearance`（值 L1..L5；**缺省按 L1 保守处理**）。

- `POST /folders` `{name, parent_id?, default_sensitivity?}` → folder
- `GET /folders` → 平铺列表（含 parent_id，前端拼树）
- `PATCH /folders/{id}` `{name?, parent_id?, default_sensitivity?}`
- `DELETE /folders/{id}`（非空 409）
- `POST /documents`：现有 multipart 增加 `folder_id?`、`sensitivity?`
- `PATCH /documents/{id}` `{folder_id?, sensitivity?}`（移动触发抬升规则）
- `GET /documents`：增加 `folder_id` 过滤；响应含 `folder_id`、`sensitivity`、`uploaded_by_user_id`；带 X-User-Clearance 时按密级过滤
- `GET /search`：参数 `q`, `k`, `scope=knowledge|memory|all`（默认 all，向后兼容）, `workspace_id?`, `exclude_origin_session_id?`
  - scope=knowledge：`origin_session_id IS NULL` 的租户知识源，密级 ≤ clearance
  - scope=memory：`origin_user_id = 当前用户` 的记忆文档，排除 `exclude_origin_session_id`
  - **相似度阈值**：cosine distance > `KNOWLEDGE_SEARCH_MAX_DISTANCE`（settings，默认 0.6）的命中丢弃，宁缺毋滥
  - 响应 hit：`{document_id, document_name, folder_path, chunk_id, text, score, scope}`（folder_path 如 `产品/合同模板`，根目录为空串）

## 会话消费侧（session-orchestrator）

- **有效 clearance** = min(用户 clearance, 会话所属工作区密级)；无工作区则取用户 clearance。以此值发 `X-User-Clearance` 给 ki（file≤workspace≤user 矩阵在检索侧的落点）。
- `create_run` 预取拆两段：scope=memory 与 scope=knowledge 各取 top-k（k=5），system 注入分两块：
  - `相关记忆（供参考）：…`（维持现状措辞）
  - `知识库资料：`每条带出处 `【文档名 | 文件夹路径】`
- **检索工具化**：inline 工具循环（复用 canvas/read_skill 的注册模式）新增：
  - `search_knowledge(query, k=5)` → ki `/search?scope=knowledge`，带有效 clearance
  - `list_knowledge()` → ki `GET /documents`（带 clearance 头），返回 名称/路径/密级/状态
- system prompt 增加知识库说明段（存在租户知识库、可用上述工具），与 `workspace_empty_notice`（基础文件）明确分开——空知识库就说知识库为空，不再和"工作区基础文件"混为一谈。

## BFF

- `/library/folders` CRUD 代理、`/library/documents` 扩展参数/响应透传。
- **写操作**（上传、ingest、folder CRUD、doc PATCH/DELETE）套现有租户管理员分档守卫（同 admin_platform 模式）；**读操作**全员放行。
- 所有 library 知识源代理**必须转发 X-User-Clearance 与用户身份头**（历史教训：chat 路径曾漏转发 X-User-Clearance 导致密级闸失效）。
- `contracts/openapi/bff.yaml` 同步更新。

## Web（LibraryPage 知识源 tab）

- 文件夹导航：面包屑 + 当前层子文件夹列表 + 文档列表；上传落当前文件夹。
- 每行密级徽章（复用现有 DlpSensitivity 展示样式）。
- 管理操作（上传/新建文件夹/重命名/移动/改密级/删除）按租户管理员角色门控（roles 取现有前端权限门控模式，来自 KC），普通用户只读。
- 不引入新 npm 依赖。

## 实施边界（并行任务防交错）

| 任务 | 只准改动 |
|---|---|
| K（存储与检索） | `services/knowledge-ingest/**`、`contracts/openapi/knowledge-ingest.yaml` |
| O（会话消费） | `services/session-orchestrator/**` |
| W（BFF+前端） | `services/bff/**`、`apps/web/**`、`contracts/openapi/bff.yaml` |

三方以本文契约对齐，不互相读改对方目录；均不 git commit；仓库内其他未提交改动一律不动。
