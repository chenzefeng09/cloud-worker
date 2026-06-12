# 大附件准入名单:多主体扩展契约（USER / TEAM / ORG_UNIT）

> 状态：设计已定稿，待 lifecycle 后端落地。
> 决策：单表 + `subject_type`/`subject_id`；resolve 并集判断由 lifecycle 内部调 foundation-org 取归属。
> 不进 RBAC —— 大附件准入是 entitlement（成本/灰度），与 permission（安全）并列。

---

## 1. 背景与目标

「大附件能力」是两层准入：租户级总开关 + 主体白名单。命中阈值（默认 50 MiB）的附件，
若主体在白名单内，run 进入大内存沙箱档位（4g），否则走小档（1g）。

现状：白名单**只支持单个用户**（`user_id` 硬编码），从类型 → API 路径 → DELETE path param →
lifecycle 数据表全链路写死 user 主体。

目标：白名单粒度扩展为 **用户 / 用户组(TEAM) / 组织单元(ORG_UNIT)** 三种主体，
准入判断改为「用户 ∈ 其所属 TEAM/ORG_UNIT ∈ 白名单」的并集查询。

主体类型枚举与 RBAC（`foundation-authz` 的 `BindingSubjectType`）对齐：`USER | TEAM | ORG_UNIT`。

---

## 2. 数据模型（lifecycle 侧 —— 由后端实现）

将原 `user-allows` 表泛化为 **subject-allows** 单表。

```
large_attachment_subject_allow
  id            uuid / string   主键
  tenant_id     string          租户隔离（来自 FoundationContext，非 body）
  subject_type  enum            USER | TEAM | ORG_UNIT
  subject_id    string          user_id / team_id / org_unit_id
  allowed       bool            true=放行 false=显式禁用
  updated_at    timestamp
  updated_by    string          操作人 user_id

  UNIQUE (tenant_id, subject_type, subject_id)
```

### 向后兼容（迁移）
- 原 `user-allows` 表的每行迁移为 `subject_type=USER, subject_id=<旧 user_id>`。
- 旧端点 `/large-attachment/user-allows*` **保留为别名**（内部转写 `subject_type=USER`），
  避免任何在途调用方 404。新代码一律走 `/subject-allows`。

---

## 3. lifecycle 上游端点契约（由后端实现）

租户作用域永远来自 `X-Tenant-Id`（FoundationContext），**不从 body 取**。
所有端点 scope-gated：`large_attachment.admin`。

### 3.1 列表
```
GET /large-attachment/subject-allows
  → { items: [ SubjectAllow, ... ] }     # 保持 {items:[]} 包裹（BFF 两种都容忍）
```

### 3.2 写入（upsert）
```
PUT /large-attachment/subject-allows
  body: { subject_type: "USER"|"TEAM"|"ORG_UNIT", subject_id: string, allowed: bool }
  → SubjectAllow
```
- 校验：`subject_type` 必须是枚举之一；TEAM/ORG_UNIT 的 `subject_id` **建议**校验在本租户
  foundation-org 中存在（不存在则 422），USER 可不强校验（与现状一致）。

### 3.3 删除
```
DELETE /large-attachment/subject-allows/{subject_type}/{subject_id}
  → { deleted: true, subject_type, subject_id }
```
> 注意：DELETE 路径从单 `{user_id}` 改为 `{subject_type}/{subject_id}` 两段，
> 因为 subject_id 在不同类型间不保证全局唯一。

### 3.4 SubjectAllow 响应形状
```
SubjectAllow:
  id?:           string | null
  tenant_id?:    string
  subject_type:  "USER" | "TEAM" | "ORG_UNIT"
  subject_id:    string
  allowed:       bool
  updated_at?:   string | null
  updated_by?:   string | null
```

---

## 4. resolve 并集判断（lifecycle 侧 —— 关键逻辑，由后端实现）

调用方（BFF `sessions.py` 的 `/large-attachment/resolve?user_id=...`）**契约不变**，仍只传 `user_id`。
并集判断完全收敛在 lifecycle 内部：

```
resolve(tenant_id, user_id):
  if not tenant_setting(tenant_id).large_attachment_enabled:
      return { allowed: false, reason: "tenant_disabled" }

  # 1) 直接命中 USER 白名单
  if subject_allow(tenant_id, USER, user_id).allowed:
      return { allowed: true, reason: "user" }

  # 2) 取用户在本租户的组织归属（调 foundation-org）
  teams      = foundation_org.teams_of(tenant_id, user_id)        # team_id[]
  org_units  = foundation_org.org_units_of(tenant_id, user_id)    # org_unit_id[]（含祖先链，见下）

  # 3) 任一归属命中且 allowed=true → 放行
  for t in teams:      if subject_allow(tenant_id, TEAM, t).allowed:      return { allowed: true, reason: "team", via: t }
  for o in org_units:  if subject_allow(tenant_id, ORG_UNIT, o).allowed:  return { allowed: true, reason: "org_unit", via: o }

  return { allowed: false, reason: "not_listed" }
```

### 需后端拍板的两个语义点
1. **org_unit 是否沿祖先链继承**：用户挂在 `/eng/infra`，白名单加了 `/eng`，是否放行？
   建议**继承**（OrgUnit 有物化 `path`，取祖先链各 id 一并参与匹配），更符合「按组织授权」直觉。
2. **`allowed=false` 的否决优先级**：若用户既在某放行的 TEAM、又被单独 `USER allowed=false`，
   以谁为准？建议 **USER 级显式 false 优先否决**（细粒度覆盖粗粒度），与多数 ACL 语义一致。

> foundation-org 侧需要的反查能力：「给定 user_id，列出其所属 team_id / org_unit_id」。
> 若现成接口只有 `/teams/{id}/members`（正查），后端需新增反查端点或在 lifecycle 侧聚合。
> 这一步是后端落地时要确认的依赖。

---

## 5. BFF 代理改动（我方实现）

`services/bff/src/agent_bff/api/v1/admin_large_attachment.py` —— 纯透传，新增 subject-allows 三个路由：
```
GET    /admin/large-attachment/subject-allows
PUT    /admin/large-attachment/subject-allows                        body 透传
DELETE /admin/large-attachment/subject-allows/{subject_type}/{subject_id}
```
保留旧 `user-allows` 三路由作别名（向后兼容），sessions.py 的 resolve 调用不变。
scope 头 `large_attachment.admin`（已有）继续套用。

---

## 6. 前端改动（我方实现）

- `api/types.ts`：新增 `LargeAttachmentSubjectAllow`（含 `subject_type`/`subject_id`），
  保留 `LargeAttachmentUserAllow` 直到旧路由下线。
- `api/client.ts`：`largeAttachment` 下加 `subjectAllows()` / `setSubjectAllow()` / `removeSubjectAllow()`。
- `LargeAttachmentPage.tsx`：
  - 加「主体类型」切换（用户 / 用户组 / 组织单元）。
  - USER → 复用已有 `SearchableSelect`（按姓名/邮箱/ID 搜，排除已加项）。
  - TEAM → 选项来自 `api.admin.org.teams.list(tid)`。
  - ORG_UNIT → 选项来自 `api.admin.org.orgUnits.list(tid)`（label 用 `path`）。
  - 白名单表格加「类型」列，行展示 `subject_type` + 名称解析（id→名）。

---

## 7. 落地顺序

1. **后端（你）**：建表 + 迁移 + subject-allows 端点 + resolve 并集 + foundation-org 反查依赖。
2. **BFF（我）**：加 subject-allows 透传路由，保留旧别名。
3. **前端（我）**：类型 / client / 页面三主体 picker + 表格类型列。

BFF/前端可在后端落地前先按本契约实现；后端未就绪时新端点返回 502/501，
前端按现有 `isTodo` 逻辑显示「能力服务未启用」，不破坏现状。
