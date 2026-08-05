# 画布 v2 设计：一等对象化 + 作用域编辑 + 建议-确认制

状态：设计定稿待评审 · 2026-07-10
作者：架构会话（对标 ChatGPT Writing Block 实测结论 + 本仓真栈取证）

---

## 1. 背景与目标

### 1.1 现状问题（全部有真栈取证）

| 问题 | 根因 | 现状 |
|---|---|---|
| 首页创作 prompt 正文黑洞 | 画布创建是前端内存态状态机（猜意图→pending→藏消息→灌 draft→渲卡），流式块 id→落库 id 二次替换竞态击穿 | 已止血（重锚 effect +「不渲染就不隐藏」门控），结构性风险仍在 |
| 402 错误被当正文落库 | 错误与内容走同一条文本通道 | 待修（后台任务已挂） |
| 模型回显原文需重叠修剪 | 编辑作用域靠文本匹配，模型被迫复述上下文 | `normalize_selection_replacement` ~100 行启发式在扛 |
| 创作意图误触发面大 | 前端正则（动词+题材词）做语义决策 | `inferCanvasAuthoringIntent` 兜底行 `一篇|一份|一个|内容|文案` 极易误伤 |
| 画布内容提取脆弱 | 从 assistant 散文里二次提取（剥前言/尾注/抽标题） | `stripLeadingAssistantMeta` 等 ~200 行启发式 |
| 无版本/无撤销 | 编辑直接覆盖 textarea 内容 | 仅 localStorage 草稿 |

### 1.2 对标结论（ChatGPT Writing Block 实测）

四个结构性决策：**① 画布是带 id/分段的一等对象**（非散文提取）；**② 触发是模型工具调用**（非前端猜测）；**③ 局部编辑走独立端点 + 显式作用域**（blockId+segment，模型只生成不定位）；**④ diff + 接受/撤销确认制**（不直接覆盖）。

### 1.3 v2 目标

1. 画布升格为服务端一等实体：独立表、块级分段、版本化，随会话做 RLS/保留期/软删。
2. 编辑协议 magic-edit 化：显式块寻址 + 结构化输出，退役重叠修剪/apply_to/散文提取三套启发式。
3. 建议-确认制：编辑产出 suggestion，accept 才落版本，天然获得撤销与历史。
4. 触发权分阶段交还模型：`create_canvas` 工具调用（经 Redis 通道物化），前端正则退役。
5. 错误通道隔离：所有画布 API 走 problem+json，错误永不入正文。

不做（本期明确排除）：编辑器底座换 ProseMirror/Tiptap（P2 后置，块模型已为其铺路）、多人协同/CRDT、跨会话画布库。

---

## 2. 总体架构

```
Phase A（旗标 canvas.v2）                Phase B（模型触发）
─────────────────────────                ─────────────────────
用户发创作 prompt                         worker: create_canvas 工具
  │ run 正常跑完，正文落 message            │ 写 Redis run-state（同 plan_steps 通道）
  ▼                                       ▼
前端（收紧后的意图检测）                    orchestrator 物化 canvas_document/version
  │ POST /canvases {source_message_id}     │ SSE: canvas_created
  ▼                                       ▼
orchestrator 服务端一次性物化              前端渲染 CanvasMessageCard(canvas_id)
  │ 返回 canvas 对象
  ▼
前端渲染卡片（绑定 canvas_id，不再提取散文）

编辑（两阶段共用）：
前端选块 + 指令 → BFF /canvases/{id}/edit（LLM 边缘：虚拟密钥 + 结构化输出）
  → 产出 suggestion（经 orchestrator 落库）→ 前端 orig/sugg 对照
  → accept → orchestrator 事务落新版本 / reject → 丢弃
```

职责划分：**session-orchestrator = 画布的 system of record**（实体 CRUD、版本、建议落库、RLS、保留期级联）；**BFF = LLM 交互边缘**（虚拟密钥解析、gateway 调用、结构化输出校验，复用现有 `_resolve_virtual_key` / `_resolve_default_model` / problem+json 惯例）。worker 与 orchestrator 之间没有 HTTP 回调路径（现状事实），Phase B 走 Redis run-state 通道物化。

---

## 3. 数据模型（session-orchestrator，迁移 `0009_canvas.py`）

遵循本仓约定：id = `new_id(prefix)`（uuid4 hex 前 26 位大写），列宽 String(30)，schema `session_orchestrator`，每表 ENABLE+FORCE RLS、四条独立 policy（SELECT/INSERT/UPDATE/DELETE 分开写，不用 FOR ALL）、`TO app_user`、谓词 `tenant_id = current_setting('app.tenant_id', true)`。

### 3.1 `canvas_document`（前缀 `cvs`）

| 列 | 类型 | nullable | 说明 |
|---|---|---|---|
| id | String(30) PK | 否 | `new_id("cvs")` |
| tenant_id | String(30) idx | 否 | |
| session_id | String(30) FK→session.id, idx | 否 | 随会话保留期/软删级联 |
| user_id | String(30) | 否 | 创建者 |
| kind | String(16) | 否 | 对齐前端 `CanvasObjectKind`（article/brief/email/report/proposal/social_post/code_note） |
| title | String(200) | 否 | 对齐 session.title 宽度 |
| current_version | Integer | 否 | default 1 |
| source_message_id | String(30) | 是 | Phase A 物化来源 |
| source_run_id | String(30) | 是 | Phase B 工具调用来源 |
| created_at / updated_at | DateTime(tz) | 否 | |
| deleted_at | DateTime(tz) | 是 | 软删，语义同 session.deleted_at |

### 3.2 `canvas_version`（前缀 `cvv`）

| 列 | 类型 | nullable | 说明 |
|---|---|---|---|
| id | String(30) PK | 否 | `new_id("cvv")` |
| tenant_id | String(30) idx | 否 | |
| canvas_id | String(30) FK→canvas_document.id, idx | 否 | |
| version | Integer | 否 | 从 1 递增；UNIQUE(canvas_id, version) |
| blocks | JSONB | 否 | 全量快照 `[{id, type, text}]`，见 §4 |
| change_summary | Text | 是 | 该版变更摘要（模型产出或 "手动编辑"） |
| created_by | String(10) | 否 | `user` / `model` |
| base_version | Integer | 是 | 由哪个版本演化而来（revert 时可与 version-1 不同） |
| created_at | DateTime(tz) | 否 | |

版本即快照：不存 delta。画布是短文档（几 KB），快照简单、revert = 复制旧 blocks 落新版本，永不丢历史。

### 3.3 `canvas_suggestion`（前缀 `csg`）

| 列 | 类型 | nullable | 说明 |
|---|---|---|---|
| id | String(30) PK | 否 | `new_id("csg")` |
| tenant_id | String(30) idx | 否 | |
| canvas_id | String(30) FK, idx | 否 | |
| base_version | Integer | 否 | 基于哪个版本提出；accept 时乐观锁校验 |
| status | String(10) | 否 | `proposed` / `accepted` / `rejected`，default proposed |
| instruction | Text | 否 | 用户指令原文 |
| diffs | JSONB | 否 | `[{block_id, op, orig, sugg}]`，op ∈ replace/delete/insert_after；**统一用 block_id**——op=insert_after 时其语义为锚点块（插在其后）。after_block_id 只出现在 §5.2 面向模型的输出 schema，由 BFF 换算 |
| change_summary | Text | 是 | |
| created_at / resolved_at | DateTime(tz) | 否/是 | |

### 3.4 message 关联

`message` 表新增列 `canvas_id String(30) NULL`（同一迁移内）。Phase B 中模型的回复消息只带简短说明文字 + `canvas_id` 引用；前端按引用渲染卡片，**不再从 content 提取**。Phase A 物化后由前端把卡片锚定到 `source_message_id`，message 行不改。

### 3.5 保留期级联

`purge_expired_sessions`（合规 30 天清理）扩展：软删 session 时同事务软删其 canvas_document；物理清除时级联删除 version/suggestion。写进同一个迁移评审 checklist，避免第二个「软删孤儿」类泄漏。

---

## 4. 块模型（Block）

- 物化/创建时服务端做一次分块：按 markdown 段落切（`\n{2,}` 分隔；标题行独立成块），块 id = `blk_` + uuid4 hex 前 8 位。
- 块类型 `type` ∈ `heading` / `paragraph`（v2 只需这两种；列表/代码块先按 paragraph 整块处理，为 Tiptap 化留扩展位）。
- **id 稳定性规则**：replace 保留原 id（换 text）；delete 移除；insert_after 生成新 id。未被编辑触碰的块 id 跨版本不变——这是「模型只生成不定位」的地基。
- 前端手动编辑（textarea 直改）：保存时按最小 diff 对齐块（顺序扫描 + 文本相等匹配），对不齐的整段重建 id；手动编辑也落新版本（created_by=user）。

---

## 5. API 契约

### 5.1 orchestrator（system of record，前缀 `/api/session/v1`，BFF 透传代理）

```
POST   /sessions/{sid}/canvases                  创建（Phase A 物化）
  body: {source_message_id?: str, kind?: str, title?: str, content?: str}
  → 201 {canvas}   # source_message_id 与 content 二选一；前者服务端读 message.content 分块
GET    /sessions/{sid}/canvases                  列表（不含 blocks）
GET    /canvases/{cid}                           详情（含 current version blocks）
GET    /canvases/{cid}/versions                  版本列表（含 change_summary）
GET    /canvases/{cid}/versions/{n}              指定版本
POST   /canvases/{cid}/versions                  手动编辑落版本
  body: {blocks: [...], base_version: int}
  → 201 {canvas}；base_version != current_version → 409
POST   /canvases/{cid}/suggestions               落建议（BFF 内部调用）
POST   /canvases/{cid}/suggestions/{sgid}/accept → 200 {canvas}（事务：校验 base_version→应用 diffs→新版本→suggestion=accepted）
POST   /canvases/{cid}/suggestions/{sgid}/reject → 200
POST   /canvases/{cid}/revert                    body: {to_version: int} → 201 新版本
```

canvas 对象 wire 格式：

```json
{
  "id": "cvs_…", "session_id": "ses_…", "kind": "article",
  "title": "AI 时代的职业转变", "current_version": 3,
  "blocks": [
    {"id": "blk_a1b2c3d4", "type": "heading", "text": "# AI 时代的职业转变"},
    {"id": "blk_e5f6a7b8", "type": "paragraph", "text": "人工智能正以前所未有的速度…"}
  ],
  "updated_at": "…"
}
```

鉴权与 RLS：全部经现有 session 鉴权路径（同 messages 端点），tenant RLS 兜底。错误走 problem+json，错误码前缀 `AGENT.SESSION.CANVAS.*`（NOT_FOUND / VERSION_CONFLICT / SUGGESTION_RESOLVED …），带中文 `user_message`。

### 5.2 BFF 编辑端点（LLM 边缘，替换现有 `/canvas/edit`）

```
POST /api/bff/v1/sessions/{sid}/canvases/{cid}/edit
  body: {
    instruction: str,                    # 用户指令
    scope: {type: "blocks"|"document", block_ids?: [str]}
         | {type: "selection", segments: [{block_id, start, end, text}]},
    model?: str,                         # 缺省走 _resolve_default_model
    base_version?: int                   # 前端所见版本；与当前版本不符→立即 409，不调 LLM（前置乐观锁，省掉白烧的模型调用）
  }
  → 200 {suggestion: {id, base_version, diffs: [{block_id, op, orig, sugg}], change_summary}}
```

流程：① GET canvas 当前版本（orchestrator）；② 组 prompt——只带目标块（带 block_id 标注）+ 少量相邻块作只读上下文，明确「只返回目标块的新内容，禁止复述未修改内容」；③ 调 gateway，**json 输出模式**（deepseek 支持 json_object；服务端按 schema 校验，不合法重试一次，仍失败 → 502 problem `CANVAS.BAD_MODEL_OUTPUT`）；④ POST suggestion 落库；⑤ 返回。

字符选区走更窄的 magic-edit 分支：服务端先校验 `base_version + block_id + start/end + text` 与当前版本完全一致，再要求模型只返回每个 slot 的替换片段。BFF 在服务端 splice 成完整 block diff，选区外前后缀不由模型生成，因此可保证字节级不变；suggestion 额外保留 `selection_start/end + selected_orig/sugg` 供前端在原位置展示。

LLM 输出 schema：

```json
{
  "edits": [{"block_id": "blk_…", "op": "replace|delete", "new_text": "…"}],
  "insertions": [{"after_block_id": "blk_…", "new_text": "…"}],
  "change_summary": "一句话变更说明"
}
```

**整套 `normalize_selection_replacement` / `_longest_*_overlap` / `_fingerprint` / apply_to 降级、`<canvas-replacement>` 标签解析退役**——模型只往显式槽位里生成，回显无害（落进对应块）。虚拟密钥缓存、默认模型解析、超时与 problem+json 错误映射按现 `canvas.py` 原样保留。

### 5.3 快捷指令

润色/扩写/压缩按钮 = 预置 instruction 的同一端点调用（scope=document 或当前选中块），无独立契约。

---

## 6. 创建通路

### Phase A（随旗标 `canvas.v2` 上线）

1. 前端意图检测**收紧**：删除 `inferCanvasAuthoringIntent` 的兜底行（`canvasObjects.ts:551` 的 `一篇|一份|一个|内容|文案`），只留强创作动词+明确题材词组合。宁可漏（有「放入画布」手动兜底），不可误劫持。
2. 命中意图 → 正常跑 run → `run_complete` 且 assistant 消息落库后，前端调 `POST /canvases {source_message_id}`；服务端读 message.content、做**一次性**分块物化（轻量剥离：只剥首尾的确认性口水句，规则保守，剥空则原样整段成块——物化失败不阻塞，正文本来就在气泡里）。
3. 拿到 canvas 对象 → 在该消息位置渲染卡片。**保留今天落地的硬门控：卡片渲染成功且内容非空之前，绝不隐藏原气泡。**
4. 会话内编辑路径同步切到 §5.2 新端点。

### Phase B（触发权交还模型）

1. worker 注册 `create_canvas(kind, title, content_markdown)` 客户端工具——沿用现有 toolkit append 机制（同 `web_fetch`/`ask_user`：包裹 `hooks.build_toolkit`，append 到 `toolkit.tool_groups[0].tools`）。deepseek 经 gateway 走客户端工具已验证可行，但需系统提示强指令（本仓已知：deepseek 要强指令才调工具）。
2. 工具执行 = 把 payload 写入 Redis run-state（复用 `RunStateClient`，模式对齐 `plan_steps`：worker 只写，orchestrator 读）。**不走 HTTP 回调**（worker→orchestrator 无内部端点，架构事实）。
3. orchestrator 流式循环读到 canvas payload → **服务端二次过滤守卫**（内容 <200 字符或 <2 段 → 跳过物化，log 后 fail-open 到气泡；模型判断之外的权威护栏，防过度触发）→ 物化 canvas_document/version → SSE 推 `canvas_created {canvas_id, run_id, message_id?}`；run 结束的对账路径同样检查未物化的 payload（对齐 result.json 兜底模式），保证掉流也不丢。
4. 前端 `useChat.ts` 的 `SSE_HANDLERS` 增加 `canvas_created`：拉取 canvas、渲染卡片。系统提示同步要求模型：调用工具后正文回复只写一句简短说明（内容在画布里），从供给侧消除「散文提取」需求。
5. Phase B 稳定后：前端意图正则整体退役，`maybeStartCanvasFromPrompt` / `pendingCanvasCreation` 状态机删除。

### 6.3 三态意图路由：create / update / chat（对标 ChatGPT 实测补强）

会话存在**活跃画布**（本会话最近创建/编辑的 canvas）时，聊天框里的修改类指令（「把这段改简洁」「换个标题」「精简第二段」）必须路由到**编辑通路而绝不新建块**：

- **Phase A（前端路由，随 PR-4）**：旗标开 + 活跃画布存在 + 消息命中修改意图（指代词/修改动词，谓词从窄）→ 直接调 §5.2 编辑端点（scope=document，携 base_version），suggestion 在卡内呈现；不建 run、不落聊天消息。未命中 → 正常聊天（含创作意图检测）。
- **Phase B（模型路由）**：worker 增配套工具 `update_canvas(canvas_id, instruction)`——系统提示注入当前会话活跃画布的 id/title/摘要，模型自主选择 create/update/chat 三态；`update_canvas` 经同一 Redis 通道由 orchestrator 转发到编辑流程。
- 铁律：存在活跃画布且用户在指代它时，禁止 create——「误新建第二块」与「误伤原文」同级严重。

---

## 7. 前端改造

### 7.1 状态：新建 `stores/canvasStore.ts`（Zustand，对齐 chatStore 风格）

```ts
interface CanvasStore {
  canvasesById: Record<string, CanvasDocument>;        // 服务端对象缓存
  suggestionsByCanvasId: Record<string, CanvasSuggestion | null>;
  panelCanvasId: string | null;                        // 侧栏打开的画布
  revisingCanvasIds: Set<string>;
  fetchCanvas(id): Promise; applySuggestion(id, sgid, accept): Promise;
  requestEdit(id, req): Promise;                       // 调 BFF edit
}
```

**退役清单**（Phase A 完成即删）：`canvasDraft` + localStorage `article-canvas-draft-v1:*`（真相移到服务端，本地只留未保存输入的暂存）、`pendingCanvasCreation`、`hiddenCanvasMessageIds`（隐藏概念消失：Phase B 消息本来只有一句说明；Phase A 卡片与气泡共存，卡片渲染后气泡折叠为「已放入画布」单行，点开可展开——内容永远可达）。

### 7.2 组件

- `CanvasMessageCard` / `ArticleCanvasPanel` props 从 `draft: CanvasDocumentDraft` 改为 `canvasId: string`（store 取对象）。v2 编辑 DOM 为每个服务端块保留 `data-canvas-block-id`，浏览器字符选区被换算为 block 内坐标；手动保存也从 DOM 读取稳定 id，避免插删前置段落后位置复用导致错绑。
- 新增 `SuggestionDiffView`：按块渲染 orig（删除线底色）/sugg（高亮底色）对照 + 每块与整体的 接受/放弃；textarea 时代即可实现（只读 diff 列表 + 按钮）。
- 错误态：编辑失败在卡内渲染 error chip（复用 problem+json 的 `user_message`），**任何错误字符串不得写入 blocks**。

### 7.3 SSE

`SSE_HANDLERS` 新增 `canvas_created`（Phase B）。顺带核实两个悬案 handler（`message_created`、`plan`）的真实产生方，无来源则删（探查中未在 orchestrator 找到发出点）。

---

## 8. 鲁棒性不变量（评审与测试的验收基线）

1. **内容永远可见**：消息隐藏/折叠必须以「其内容已渲染在某个画布卡里且非空」为前提（已落地的门控保留为回归测试）。
2. **错误永不入正文**：LLM/gateway 错误 → problem+json → UI 错误态；run 层面错误 → run.FAILED + error_message（关联已挂的 402 修复任务）。
3. **建议永不自动应用**：所有模型编辑必经 accept；accept 带 base_version 乐观锁，冲突 → 409 → 前端提示刷新建议。
4. **版本单调且可回退**：任何落库变更（模型 accept / 手动保存 / revert）都是追加新版本，历史不可变。
5. **物化失败不丢内容**：Phase A 物化/Phase B 工具通道任何一环失败，正文仍以气泡完整呈现（fail-open 到聊天）。

---

## 9. 灰度与迁移

- 旗标：自研 feature-flag 服务（**非 Unleash**），key `canvas.v2`。后端用 `libs/feature-flag-client` 的 `require_flag("canvas.v2")` 守卫新端点；前端 `useFeatureFlag('canvas.v2')` 切换卡片渲染路径（参考现有 `ai.thinking_ui` 用法）。kill-switch 即回旧路径。
- 旧数据零迁移：旗标开启前的会话没有 canvas 行，历史消息按普通气泡渲染 +「放入画布」按钮（点击即 Phase A 物化端点，天然兼容）。
- worker 侧 Phase B 改动需重建沙箱镜像（本仓惯例），随 scheduler 旗标 `enable_canvas_tool` 独立灰度，与前端旗标解耦。

---

## 10. 测试矩阵

| 层 | 内容 |
|---|---|
| orchestrator 单测 | CRUD/RLS（跨租户不可见）、版本乐观锁 409、accept 事务原子性、revert、软删级联、分块函数（标题/段落/空内容/纯口水句剥空回退） |
| BFF 契约测试 | 结构化输出 happy path、非法 JSON 重试一次后 502、block_id 不存在 422、gateway 402/超时 → problem+json（复用现 fake gateway 测试基建，参考 test_canvas_edit.py） |
| 前端 vitest | canvasStore、SuggestionDiffView 渲染、不变量 1 的门控（保留今天新增的 canvasAnchoring 用例）、blocks↔textarea 对齐 |
| Playwright e2e | ①首页创作 prompt→卡片带正文；②会话内创作→同；③非创作 prompt→纯气泡；④选块编辑→diff→accept→版本+1；⑤reject→原文不变；⑥配额 402→卡内错误态且 blocks 未污染；⑦刷新后卡片从服务端恢复（localStorage 清空也不丢） |

---

## 11. 交付切分（每个可独立合并/回滚）

1. **PR-1** orchestrator：迁移 0009 + 模型 + CRUD/版本/建议端点 + 单测（纯新增，无行为变化）。
2. **PR-2** BFF：新 edit 端点（结构化输出）+ suggestion 落库集成 + 契约测试；旧 `/canvas/edit` 保留待 PR-6 删。
3. **PR-3** 前端：canvasStore + 卡片/面板绑定 canvas_id + Phase A 物化调用 + 旗标切换（`canvas.v2` off 时走旧路径）。
4. **PR-4** 前端：SuggestionDiffView + accept/reject/revert UX + e2e ①-⑦。
5. **PR-5** worker + orchestrator：create_canvas 工具 + Redis 物化 + SSE `canvas_created` + 沙箱镜像重建（旗标 `enable_canvas_tool`）。
6. **PR-6** 清理：旧 `/canvas/edit`、重叠修剪/标签解析、前端意图正则与劫持状态机、localStorage 草稿迁移代码退役。

依赖链：PR-1 → PR-2 → PR-3 → PR-4；PR-5 依赖 PR-1；PR-6 依赖全部灰度验证。

---

## 12. 开放问题

1. **编辑器底座**：块模型落地后，Tiptap（ProseMirror）替换 textarea 的收益主要在选区交互与 diff 装饰的原生化；建议 PR-4 验证 diff UX 后再立项，块 schema 无需变更。
2. **LLM 调用位置**：本设计放 BFF（复用虚拟密钥/默认模型基建）。若后续画布编辑要进 run 语境（带会话历史），再评估移到 orchestrator 或 worker。
3. **deepseek json 模式稳定性**：schema 校验 + 单次重试是否足够，灰度期看 `CANVAS.BAD_MODEL_OUTPUT` 错误率决定是否加第二重（few-shot 或切换支持严格 json_schema 的模型档）。
4. **悬案 SSE handler**：`message_created` / `plan` 的产生方核实（PR-3 顺带）。

### 12.1 对标 ChatGPT magic-edit 的取舍备忘（评审输入）

**已吸收**：编辑请求 `base_version` 前置乐观锁（§5.2）；三态意图路由 create/update/chat（§6.3）；物化前服务端二次过滤守卫（§6 Phase B）。

**刻意不同（不改）**：① suggestion 服务端落库持久化——对标实现的 diff 只活在前端内存、刷新即丢，与本仓「关键状态不放前端内存」的教训相悖；② 编辑走结构化 JSON 输出——对标实现让模型返回裸替换文本，正是我们退役的「回显+解析脆弱」老路；③ diffs 支持 insert_after/delete——对标只支持段替换。

**已提前采纳（不依赖 Tiptap）**：块内字符子选区（block 内 start/end + selectedText 冗余校验）；diff 以 `data-me-role="orig|sugg"` / `data-me-id` / `data-me-segment` 标记在原位置渲染；选区浮动工具栏。未来换 ProseMirror/Tiptap 时只需把坐标来源替换为 PM selection，服务端协议无需再次变更。
