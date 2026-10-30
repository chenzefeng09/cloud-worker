# DLP 改造方案：从「测试工具页」补齐成「生效防线」

> 状态：**已实现（P0/P1/P2 三阶段全部落地）**。2026-10-30 修订：DLP 无法完成检查时默认拒绝（fail-closed），见「七、失败策略与上线顺序」。
> 范围：`saas-foundation/packages/dlp`（底座）+ `agent-platform`（session-orchestrator / bff / web）
> 撰写依据：对两仓 DLP 相关代码的只读勘察，挂载点已精确到函数。

---

## 落地状态总览（2026-06-12）

| 阶段 | 内容 | 状态 | 验证 |
|---|---|---|---|
| **P1** | 默认规则库（7 条全局 REGEX）+ scan 的 L 级比较 bug 修复 | ✅ | foundation dlp 单测 31 passed |
| **P0** | session-orchestrator 接入：下载 check:download、上传先 scan 后写 blob（DLP 直接打标）、助手回复落库后 scan 打标；无法完成时按 `DLP_FAILURE_MODE`（默认 closed → 503） | ✅ | `test_dlp_guard.py`、`test_startup_dlp_policy.py` |
| **P0-W** | worker PreToolUse：每次工具调用前 scan，只拦 `QUARANTINE`（`SCAN_REQUIRED` 放行并告警）；无法完成时按 `DLP_FAILURE_MODE`（默认拒绝） | ✅ | worker `tests/dlp.test.ts` |
| **P2a** | scan 结果落 dlp_event、/core/events、/core/classifications、/core/stats 读端点、prometheus metrics + /metrics | ✅ | foundation dlp 单测 31 passed |
| **P2b** | BFF 代理三读端点、web client/types、DlpPage 升级为治理面板（统计/历史/密级表 + 手动工具下沉折叠） | ✅ | tsc 0 新错、vite build exit 0 |

**已知遗留（见第六节）**：产物（output artifact）扫描挂载点 3 仍是 TODO（避免对账路径加 S3 IO）；银行卡正则 `\d{16,19}` 偏宽可能误报；`foundation` egress 模式下沙箱能直连 DLP、自带 `X-Actor: SERVICE` 改标签，只有 `broker` 模式防得住。

**已解决**：下载 check 不再用 L3 默认 clearance，由 DLP 按 `X-User-Id` 自查（调用方传的 clearance 会被拒）；无标签资源由 DLP 按 L3 判。下载与上传不再 fail-open（见第七节）。

### 实现细节速查
- **P1 默认规则**：身份证 L4 / 手机 L3 / 银行卡 L4 / 邮箱 L2 / AWS AK L4 / 私钥头 L4 / 通用 api_key L3。migration `0002_dlp_default_rules`（down_revision `0001_dlp_init`），全局 `tenant_id NULL`，`op.execute` 原生 SQL 绕 RLS。
- **P0 挂载**：`dlp_guard.py`（仿 authz_guard）封装 `check_download` / `scan_and_tag`；attachments.py 下载/上传两处、config 新增 `foundation_dlp_url`。
- **P2a 端点**：均挂 `/core` 前缀；scan 命中写 dlp_event（check_type=`SCAN`，detail 存命中明细 JSON）；counter `dlp_scan_total{action}` / `dlp_scan_hits_total` / `dlp_check_total{check_type,decision}`。
- **P2b 面板**：recharts 柱状图（by_decision / by_sensitivity）+ 事件历史表 + 密级资源表，手动三工具收进默认折叠的「手动工具」区。

---

## 一、现状诊断（为什么说它现在不是防线）

当前 DLP 子系统是**「装好了但没接电、也没装弹匣」**的半成品。三个层面都缺：

| 维度 | 现状 | 证据 |
|---|---|---|
| **引擎** | 真实现（regex/keyword 匹配 + clearance 比对 + 密级防降级），但 `dlp_rule` 表**无任何内置规则** → 开箱即用时 scan 对一切返回 `L1/ALLOW` | `dlp_service.py:146-186`；migration / seed 无规则插入 |
| **被动拦截** | **零挂钩**。附件下载、上传、run 产物、消息输出链路无一调用 DLP。SDK 封装好的 `check_download`/`scan_content` 生产代码调用次数为 0 | services/* grep dlp 全为 false-positive；SDK 唯一调用方是测试 |
| **留痕 / 监督** | 标密级（`classification`）、访问检查（`dlp_event`）会落库；但 **scan 结果返回即焚不入库**；**无 metrics**（`/metrics` 是占位）；**无任何 list/聚合端点**（审计表只能写不能读） | `dlp_service.py:35-204`；observability 无 prometheus client |

**结论**：管理页（`DlpPage.tsx`）只覆盖「手动粘一段文本试试 / 给某资源打密级标」，是开发期验证工具被摆到了管理后台的位置，所以点进去会觉得「它啥也没管」。

---

## 二、目标终态

把 DLP 变成一道**自动生效、可监督、可治理**的防线：

1. **被动生效** —— 文件下载/上传、run 产物外发时自动经过 DLP，无需人工点。
2. **空膛 → 装弹** —— 预置一批 PII / 密钥规则，租户可增删。
3. **可监督** —— 扫描历史、命中统计、已标密级资源列表，管理员能「看全局」，而非只能点状操作。

---

## 三、改造分解（按优先级，P0 → P2）

### P0 — 被动拦截集成（核心，没有这块 DLP 就是摆设）

这是把「能点」变成「生效」的唯一关键。所有挂载点都在 **session-orchestrator**（不在 BFF，BFF 只透传）。

#### P0-1　下载前拦截 `check:download`
- **挂载点**：`services/session-orchestrator/src/agent_session_orchestrator/api/v1/attachments.py:151` `get_file_content()`
- **插入位置**：`att = await svc.get_attachment(...)`（:166）拿到元数据后、`blob_store.get(...)`（:172）取字节**之前**。
- **做法**：以 `att.id` 为 `resource_id`、附件类型为 `resource_type`，调 foundation-dlp `POST /core/check:download`（header 带 `X-User-Id`，body 只有 `{resource_type, resource_id, destination}`，clearance 由 DLP 自查）。判定为拒绝则在落字节前返回 403。
- **覆盖面**：这是用户/agent 读取任何文件内容的**唯一入口**，对 input（上传）和 output（产物）都生效——是整条防线的咽喉。

#### P0-2　上传/入库时 `scan`
- **挂载点**：`attachments.py:52` `upload_attachment()`
- **插入位置**：`data = await file.read()`（:67）后、`blob_store.put(...)`（:92）写 S3 **之前**。
- **做法**：拿 `data`/`filename`/`mime` 调 `POST /core/scan`；命中高敏感则**拒绝上传**或**自动打 classification 密级标**（推荐后者：入库即标密，下载时再凭标做 check:download）。
- **已实现**：采用后者，先 scan 再写 blob。scan 以 `X-Actor: SERVICE` 发出，DLP 直接把命中级别写成标签，不再单独调 `classification:tag`；DLP 只让 SERVICE/ADMIN 写标签。

#### P0-3　run 产物入库时 scan + 标密
- **挂载点**：`services/session-orchestrator/src/agent_session_orchestrator/api/v1/messages.py:544` `_persist_artifacts()`（在 `_reconcile_run_completion` 内）
- **背景**：产物由 sandbox worker 产出、scheduler 落 S3，orchestrator 在 run 完成对账时登记成 `kind="output"` 附件（:566 `create_attachment`）。此处产物已在 S3，所以**真正的外发拦截仍靠 P0-1 的 check:download**；这里负责「入库时 scan 一遍并打 classification 标」。
- **可选**：消息正文扫描挂 `messages.py:510 / 471-504`（`create_message` 处）。

#### P0 配套：orchestrator 接入 foundation-dlp 的工程改动
- orchestrator 的 `config.py` **当前没有** `foundation_dlp_url`，需仿照已有的 `authz_url` 新增。
- 调用范式照搬 authz guard：`services/session-orchestrator/src/agent_session_orchestrator/api/authz_guard.py:76` —— 裸 `httpx.AsyncClient`、`X-Tenant-Id` 走 header（不进 body）、紧 timeout。
- **fail-open vs fail-closed（已定，2026-10-30）**：由 `DLP_FAILURE_MODE` 决定，默认 `closed`，下载和入库 scan 都适用，细节见第七节。原先「入库 scan fail-open」的建议作废：没扫过的文件没有标签，下载时只按 L3 判，等于绕过了标签。助手回复是例外：它在 run 过程中已经流式推给用户，scan 失败时照常落库、不打标签，记 ERROR 以便重扫。
- **不要走 BFF admin 代理**：BFF 的 `check:read`/`check:download` 挂在 `require_super_admin_bff` 下，只适合后台手动核验；每次下载的内联拦截应由 orchestrator **直连** foundation-dlp。

### P1 — 默认规则库（让空膛的枪装上弹匣）

- **现状**：`dlp_rule` 表建出来是空的，无任何预置规则；引擎只实现了 `REGEX` 和 `KEYWORD` 两种类型（`ML_MODEL`/`COMPOSITE` 声明了但无处理分支）。
- **改造**：在 `packages/dlp/migrations/` 增加一条 seed migration（或独立 seed 脚本），以 `tenant_id IS NULL`（全局生效）插入一批基础 `REGEX` 规则：
  - 中国大陆身份证号、手机号、银行卡号、邮箱
  - 常见密钥/凭据模式（AWS AK、私钥头 `-----BEGIN ... PRIVATE KEY-----`、通用 `api[_-]?key`、Bearer token 等）
  - 每条规则定 `min_sensitivity`（如身份证/银行卡 → L4，邮箱 → L2）
- **顺带修一个可疑逻辑**：`dlp_service.py:186` 的 `min_sensitivity >= "L4"` 是字符串字典序比较（`"L3" >= "L4"` 走字符序），需改成对 L1–L5 的数值化比较，否则 action 推导会错。
- **保留可配置**：现有 SDK 端点 `POST /sensitive-words`（`dlp_sdk.py:112-132`）允许租户加 KEYWORD 规则，保留；可补一个「列出/删除规则」的管理端点配合 P2 治理面。

### P2 — 监督与治理面（让「结果可监督」成立）

- **scan 结果落库**：当前 `scan_content` 命中只发 outbox 聚合事件、明细不入库。新增一张 `scan_result` 表（或复用 `dlp_event` 加 `check_type='scan'`），把命中明细持久化，否则永远无法回溯「扫到过什么」。
- **新增 list/聚合读端点**（当前完全没有）：
  - 列出扫描历史（按租户/时间/命中等级过滤）
  - 列出已标密级的资源（读 `classification` 表）
  - 命中统计（按规则/资源类型/时间聚合）—— 供管理页做仪表盘
- **接入 metrics**：`observability.py` 目前 `/metrics` 是占位（无 prometheus_client / Instrumentator）。补 DLP 专属计数器：扫描次数、命中次数、按规则命中分布、check 拒绝次数。
- **管理页升级**（`DlpPage.tsx`）：从「点状测试工具」升级为「治理面板」——
  - 上方：命中统计 / 拒绝趋势仪表盘（消费 P2 聚合端点）
  - 中部：扫描历史列表、已标密级资源列表
  - 「手动扫描」「手动标密」降级为面板里的次要工具，而非整页主体。
  - 把前面已做的「资源 ID 可选 + 文案」改动并入。

---

## 四、依赖与顺序

```
P1 默认规则库  ──┐
                ├──→  P0 被动拦截（没有规则，拦截也拦不到东西；建议 P1 与 P0 并行起步，P1 先落）
P0 工程接入   ──┘
                          │
                          ▼
                   P2 监督治理面（依赖 P0/P1 产生的真实数据才有内容可展示）
```

- **最小可用闭环（MVP）**：P1 预置规则 + P0-2 上传 scan 自动标密 + P0-1 下载 check 拦截 → 已是一道真实生效的防线。
- P2 是「可监督」，可在 MVP 上线后跟进。

## 五、各仓改动清单速查

| 仓 / 服务 | 改什么 | 优先级 |
|---|---|---|
| saas-foundation `packages/dlp` migrations | 预置规则 seed；（可选）scan_result 表 | P1 / P2 |
| saas-foundation `packages/dlp` dlp_service.py | 修 `:186` 字典序比较 bug；scan 结果落库 | P1 / P2 |
| saas-foundation `packages/dlp` api | 新增 list/聚合读端点；DLP metrics | P2 |
| agent-platform session-orchestrator config.py | 新增 `foundation_dlp_url` | P0 |
| agent-platform session-orchestrator attachments.py | `get_file_content` 挂 check:download；`upload_attachment` 挂 scan | P0 |
| agent-platform session-orchestrator messages.py | `_persist_artifacts` 挂 scan+标密 | P0 |
| agent-platform bff | 代理 P2 新增的 list/聚合端点 | P2 |
| agent-platform web DlpPage.tsx | 升级为治理面板 | P2 |

## 六、待决策项（需产品/安全拍板）

1. ~~**下载 check 的 fail-open / fail-closed**~~：已定，默认 fail-closed，`DLP_FAILURE_MODE=open` 可改为放行+告警（第七节）。
2. ~~**入库命中高敏感时**~~：已定，放行并由 DLP 打密级标，下载时凭标准入。
3. **默认规则的密级映射**：身份证/银行卡/密钥分别对应 L 几，影响下载准入。
4. **是否扫消息正文**：还是只扫文件（性能/隐私权衡）。
5. ~~**worker 工具调用的拦截门槛**~~：已定（2026-10-30），只拦 `QUARANTINE`（L4 及以上）；`SCAN_REQUIRED`（L2/L3，例如工具入参里的邮箱、手机号）放行，记一条带规则名的告警；契约之外的 action 按 `DLP_FAILURE_MODE` 处理。

## 七、失败策略与上线顺序（2026-10-30）

DLP 检查无法完成（不可达、2 秒超时、非 2xx、响应无法解析）时的处理：

| 组件 | 配置 | DLP 地址为空 | 正常应答 | 无法完成 + `closed`（默认） | 无法完成 + `open` |
|---|---|---|---|---|---|
| worker（PreToolUse） | `DLP_URL` / `DLP_FAILURE_MODE`，由 scheduler 的 `SCHEDULER_DLP_URL` / `SCHEDULER_DLP_FAILURE_MODE` 下发到沙箱 | 全部放行，启动时记一次 | 只拦 `QUARANTINE`，原因带规则名；`SCAN_REQUIRED` 放行并告警 | 拒绝工具调用 | 放行，每次 `console.error` |
| session-orchestrator（附件、工作区文件） | `SESSION_ORCHESTRATOR_FOUNDATION_DLP_URL` / `SESSION_ORCHESTRATOR_DLP_FAILURE_MODE` | 全部放行，启动时记 WARNING | 下载 `allowed=false` → 403；上传命中照常存，由 DLP 打标 | 503 `AGENT.DLP.UNAVAILABLE`，上传什么都不存，下载不发字节 | 放行，每次记 ERROR |

- 生效策略在服务启动时写进日志；无法识别的 `DLP_FAILURE_MODE` 按 `closed` 处理（scheduler 直接拒绝启动）。
- docker compose 用一个 `DLP_FAILURE_MODE`（默认 `closed`）同时喂 scheduler 和 orchestrator；helm 用 `global.foundation.dlpFailureMode`。
- **上线顺序**：先部署（或同时部署）新版 foundation-dlp（任何带租户上下文的调用方都能从 `/core/scan` 拿到判定，只有 SERVICE/ADMIN 写标签），再上新的沙箱镜像和 orchestrator。旧版 DLP 对 worker 的 scan 一律 403，而新 worker 默认 `closed`，会拒绝所有工具调用。
- **管理后台**：BFF 的 `/admin/dlp/classification:tag` 以 `X-Actor: ADMIN` 发出；`/admin/dlp/scan` 故意不带，只给判定、记事件，不写标签（管理员扫的是自己粘贴的文本，空 resource id 还会落成 `adhoc-scan` 占位）。
