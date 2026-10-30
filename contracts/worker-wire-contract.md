# Worker Wire Contract — task.json / Redis 流 / 平台 SSE 事件

> **这是谁的契约**:沙箱 worker 与平台(agent-scheduler / session-orchestrator / 前端)之间的
> 全部线上格式。唯一的 worker 实现是 Claude Agent SDK worker(`worker-claude-sdk/`,入口
> `node /app/dist/main.js`,镜像 `ghcr.io/agent-platform/claude-sdk-sandbox:0.1`),见
> [ADR 0003](../docs/adr/0003-claude-agent-sdk-sole-agent-runtime.md)。早期的 AgentScope worker
> (`agentscope-ext/`)已移除。
> **只要 worker 遵守本文件,orchestrator 与前端零改动。**
> SDK 内部事件名(如 Claude SDK 的 `content_block_delta`)一律不得外漏:worker 在自己内部
> (`src/event_translator.ts`)把它们翻译成本契约的 Redis 条目。
>
> 前端侧的对应单点:`apps/web/src/hooks/useChat.ts` 的 `SSE_HANDLERS` 映射表。
> 维护纪律:改任何一层,三处同步——本文件、orchestrator 归一层、`SSE_HANDLERS`。

## 1. 输入:`/workspace/task.json`(scheduler 写,worker 读)

scheduler 按**显式字段清单**组装(docker: `docker_runtime._write_task_file`;
cube: `cube_runtime.spawn_run` 的 `task_json`,经 `AGENT_TASK_JSON` env)。
**新增字段必须三层穿线**:orchestrator 发 → scheduler `api/v1/runs.py` 读 body →
`spawn_run` 形参 → payload(两个 runtime 都要),否则静默丢失。

```jsonc
{
  "task": "string",              // 用户指令(>32KiB 截断带哨兵后缀)
  "mcp_servers": [],             // 【历史遗留,恒空,不是选择器】见 §1a
  "mcp_server_ids": [...],       // 【三态,按"键是否存在"判定】见 §1a
  "skills": ["name", ...],       // worker 据此在 /home/skills/<name> 下解析 SKILL.md
  "files": [{"key","filename","mime"}, ...],   // worker 下载到 /workspace/inputs/
  "memory": "string",            // 预取记忆片段,worker 前置进 system prompt
  "history": [{"role","type",...}, ...],  // 多轮历史(有损 dict),每一腿(含 resume)都带;
                                 // 过长时最旧的若干条换成首条 history_omitted 标记,见下
  "model_override": "string|null",
  "reasoning_effort": "string|null",
  "sys_prompt": "string",        // App harness 系统提示;orchestrator 预取的租户知识库
                                 // top-k 命中也追加在这里(聊天 run 无命中时可为空串)
  "sampling_params": {},         // App harness 采样覆盖
  "hitl": true,                  // false=一刀切 BYPASS(定时/后台 run)
  "resume": null                 // 或 {"kind":"confirm"|"answer", "tool_call_id":str,
                                 //     "confirmed":bool|null, "answer":str|null,
                                 //     "agent_state":{...}}   ← 见 §4
}
```

**大小上限**:cube runtime 把整个 task JSON 放进单个环境变量 `AGENT_TASK_JSON`,而 Linux
限制单个环境变量 128 KiB(MAX_ARG_STRLEN)——超了 worker 起不来,scheduler 却照样报成功。
所以 orchestrator 每次派发(首腿 `create_run`、每个续跑腿 `respond_run`)前把载荷压在
112 KiB 内(`runs._DISPATCH_MAX_BYTES`,给 scheduler 追加的字段留余量):从最旧的 history
丢起,换成**一条**放在首位的标记
`{"role":"system","type":"history_omitted","omitted":N,"content":"[N earlier message(s) omitted: …]"}`
(续跑时与已有标记合并,不叠加),并记 warning 日志;history 丢光仍超限再记一条 warning。
worker 渲染 history 时另有约 8000 token 的预算,也从最旧丢起(工具调用连同结果一起丢);
两处丢掉的条数合计,以 `[N earlier message(s) of this conversation are not shown]` 写在
system prompt 的 history 段开头,让模型知道对话不完整。

## 1a. MCP 选择:`mcp_server_ids`(三态)与 `mcp_servers`(遗留空键)

**`mcp_server_ids` 是 run 级"本次启用哪些 MCserver"的唯一选择器**,取值为
mcp-registry 的 server `id`(形如 `mcp_<26HEX>`)。它是**三态**的,三态的区分靠
**键是否存在**,不是靠真值:

| task.json 里的样子 | 语义 |
| --- | --- |
| **键不存在** | 用租户默认启用集——即注册该租户**全部** enabled 的 server(历史全注入行为) |
| `[]`(显式空表) | 本次 run **全部禁用** MCP,一个都不注册 |
| 非空列表 | **只**注册 id 命中列表的 server |

> ⚠️ **必须用 presence 判定(`"mcp_server_ids" in task` / `!== undefined`),
> 绝不能用真值判定。** `[]` 在 Python 和 JS 里都是 falsy,一个
> `if not task.get("mcp_server_ids")` 或 `if (task.mcp_server_ids)` 会把
> "全部禁用"和"键不存在"两种**相反**的意图折叠成同一个分支,结果是用户按了
> "全关"却拿到了全租户的 MCP。同一份契约里的 `skills` 没有这个陷阱(它的 `[]`
> 和缺省同义),**不要照抄 `skills` 的 `?? []` 兜底写法**。

**容错**:键存在但形状非法(例如给了字符串而不是列表)一律**降级为"键不存在"**
(走租户默认),而不是降级为"全部禁用"——scheduler 发错形状时应退回既有行为,
而不是静默剥光用户的工具。

**与 `mcp_servers` 的关系:没有关系。`mcp_servers` 不是选择器,永远不要读它来做启用判断。**
它是早期设计的残留,scheduler 在两个 runtime 里都把它**硬编码写成 `[]`**
(`docker_runtime.py`,注释 "the worker fetches live"),因为 server
清单是 worker 在沙箱内实时向 mcp-registry 拉的,不经 task.json 下发。
**若有人把 `mcp_servers` 当成选择器读,由于它恒为 `[]`,每个 run 的 MCP 都会被全部禁用。**
worker 有回归测试钉住这一点(`"is unaffected by the legacy mcp_servers key"`),改动时不要绕过。

**消费端**(worker 在**注册前**过滤,且**先按选择过滤、再做 transport 过滤**——
选择是"用户选没选中"的身份判断,transport 是"本运行时支不支持"的能力判断,两者不得混淆;
先过滤保证未选中的 server **完全不被连接/探测**,也保证 transport 的丢弃告警只对用户真正
选中的 server 报出):

* `worker-claude-sdk/src/task.ts`(解析,保留 `undefined`)→
  `src/worker.ts` `loadMcpServers(env, selectedIds)`。显式 `[]` 直接短路,连 registry 都不请求。

## 1b. 输入:`/workspace/.secrets.json`(scheduler 写,worker 读后即删)

**用途**:凭据**绝不**以容器 env 下发。内核把进程 environ 暴露在 `/proc/1/environ`,
沙箱内任意代码 `cat /proc/1/environ` 即可读到全部 env,绕过 worker 的 env-scrub 防线。
因此 scheduler 把凭据子集从 `environment=` 中剥离,改写入 `/workspace` 绑定挂载下的
0600 文件(docker: `docker_runtime.spawn_run`)。其余路由/元数据(各服务 URL、租户/
用户/run id、模型、egress 模式、scope、bucket/region 等)仍走 env。

**文件格式**:扁平 JSON 对象,模式 `0600`:

```jsonc
{ "KEY": "value", ... }
```

**可能出现的键(至多这 6 个)**:`FOUNDATION_API_TOKEN`、`TOOL_BROKER_LEASE`、
`LLM_VIRTUAL_KEY`、`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`、`REDIS_URL`。
`TOOL_BROKER_LEASE` 仅在非空时出现。`LLM_VIRTUAL_KEY` 是租户级 LLM 网关虚拟密钥:
scheduler 解析不到虚拟密钥时**直接让 spawn 失败**,不存在直连模型厂商的回退
(已无 `LLM_API_KEY`)。键缺失即语义上"未配置",worker 不得猜测。

**worker 契约**:启动后、**任何工具子进程运行前**,worker 必须把本文件读进自身
进程 env,然后 `unlink` 删除该文件(避免凭据残留在工作区被产物收集器或后续步骤
读到)。这与 §7 "工具执行前清理控制面 env" 是同一道纪律的两端:env 里本就不该有
这些凭据,文件读完即焚。

## 2. 输出①:Redis token 流 `agent:run:{run_id}:tokens`(worker XADD,orchestrator XREAD)

orchestrator 的 `_stream_tokens` 从 `0-0` 全量重放——**重连/续跑会重放整段**,
消费端(前端)靠 `run_started` 清块 + offset 水位实现幂等;worker 只管 append,不重写。
每次写入都会刷新流的 TTL(30 分钟,暂停时改为 7 天),MAXLEN 约 100k。
运行中若连续 5 分钟没有任何写入(比如一个长时间不出声的工具调用),worker 写一条
`heartbeat` 并重写 `agent:run:{run_id}:state`,流和热状态键都不会在 run 还活着时过期;
run 暂停或结束即停止。

| 条目 `type` | 其余字段 | 语义 |
|---|---|---|
| `delta` | `text` | 助手正文增量(一条可合并多个 token,约 50 ms / 512 字符) |
| `thinking_delta` | `text` | 推理增量,实时流出 |
| `thinking` | `text` | 推理整块——**只用于未流式输出过的块**,在该条消息正文之后到达 |
| `tool_call` | `tool_call_id`, `name`, `args`(JSON 字符串) | 工具调用,参数完整;**同一 id 至多一条** |
| `tool_result` | `tool_call_id`, `name`, `result`, `state` | 工具结果 |
| `pending_confirmation` | `tool_call_id`, `name`, `args` | HITL:等用户批准(一个待批工具一条) |
| `pending_question` | `tool_call_id`, `question` | HITL:ask_user 等用户回答 |
| `error` | `message` | 失败原因(固定的、可展示给用户的短句),在 `end{failed}` 之前 |
| `heartbeat` | (无) | 保活,只为刷新 TTL(见上);读端一律忽略,不转发、不落库 |
| `end` | `text: ""`, `status` | 每个 worker 进程**恰好一条**,之后不再写。`status` ∈ `completed` / `failed` / `cancelled` / `paused`;缺省视为未知(旧 worker) |

暂停与续跑共用**同一条流**:`end{paused}` 写在 pending 条目之后;续跑腿在其后继续
append,直到新的 `end`。因此读端遇到 `end{paused}` 必须继续读,不能当作结束。

orchestrator 也会写两类条目:

| 条目 `type` | 写入方 | 语义 |
|---|---|---|
| `pending_resolved` | `respond_run` | 用户已回应,`tool_call_id` 对应的卡片作废(该次暂停的每个 id 各一条);续跑时模型会以**新的** `tool_call_id` 重新发起调用 |
| `end{status: cancelled}` | 取消 / 新一轮对话废弃暂停的 run | 让正在跟流的连接立即停止,重放时视为已结束 |

orchestrator 还在 `agent:run:{run_id}:dispatch` 存放该 run 的原始派发载荷(模型、
skills、MCP、文件、history、memory、system prompt、HITL;TTL 8 天),续跑时以它为底重派
(加上 `resume`,再按 §1 的大小上限裁一次 history),run 进入终态后删除。

## 3. 输出②:`/workspace/result.json`(worker 写,scheduler `get_run_status` 透出)

```jsonc
{
  "status": "OK" | "FAILED" | "paused",   // paused 必须配合容器 exit 0
  "summary": "string",          // 最终助手文本(reconcile 落库为助手消息)
  "error": "string|null",
  "usage": {"input_tokens","output_tokens","total_tokens"},
  "plan_steps": []              // RUNNING 期间也会被读,实时计划面板
}
```

scheduler 把 `status` 以顶层 `result_status` 透传;orchestrator reconcile 规则:
容器退出 + `result_status=="paused"` → Run 置 `PAUSED`(非终态,不落助手消息);
`FAILED` 提升覆盖 exit-0;其余 → COMPLETED/FAILED。

`usage` 是**整个 run 的累计用量**,不是单段:
- worker 每次工具调用结束、每段结束(完成/失败/暂停/SIGTERM 取消)都重写它;同一进程内
  只增不减(崩溃兜底写入也不会把已记录的数清零)。
- 暂停时用量随 `agent_state.usage` 写入暂停状态(§4),续跑段从这个数接着累计,所以任何
  时刻文件里的数都覆盖此前所有段。
- scheduler 起新一段时先删掉上一段留在同一目录的 `result.json`,读到的永远是当前段的文件。
- scheduler 的 cancel 在停掉沙箱后读一次 `usage` 并在响应里返回(被取消的 run 不会再被
  轮询)。
- orchestrator 在暂停、终态、取消时各记一次,规则是**只取较大值、从不调小**:同一段被多处
  观察到、观察顺序颠倒都不会重复计算或回退。沙箱已消失(scheduler 404)的路径拿不到文件,
  保留已记录的值。

## 4. 输出③:暂停状态 `agent:run:{run_id}:paused`(SET,TTL 7 天)

```jsonc
{
  "kind": "confirm" | "question",
  "reply_id": "string",                  // 续跑时 reply_id 延续,不再发 ReplyStart
  "tool_calls": [{"id","name","args"}],
  "question": "string|null",
  "agent_state": {...},                  // worker 自定义的完整可恢复状态
                                         // (Claude SDK 会话恢复载荷),格式由 worker 定,
                                         //   平台只负责原样回传,不解析;其中
                                         //   `usage` 是暂停时整个 run 的累计用量(§3)
  "paused_at": "iso8601"
}
```

续跑:orchestrator `POST /sessions/{sid}/runs/{run_id}/respond` 读此 key →
以条件更新抢占同一 Run 行(PAUSED→RUNNING;并发的取消先到则 409)→ 用存下的原始
派发载荷重派 scheduler,`task.json["resume"]` 带回 `agent_state` + 用户决定;
worker 凭 state 还原并注入确认/答案事件续跑,token 续写**同一条**流(同 run_id)。
暂停时 worker 更新热状态键 `agent:run:{run_id}:state` 的 `status` 字段为 `"PAUSED"`。

工具批准按「工具名 + 规范化参数(解析后各层 key 排序再序列化,所以 key 顺序、空白、
`5.0`/`1e2` 之类的数字写法、字符转义都不影响)」匹配,且只用一次:参数不同或同参数的
第二次调用都会再次暂停。

## 5. 平台 SSE 事件(orchestrator `GET /sessions/{id}/stream` → BFF relay → 前端)

session-scoped、multiplex 该会话所有 run、5 分钟服务端 deadline(客户端自动重连)。
重连带 `?after=<sequence>&after_id=<id>`(客户端最新的持久化行),只补发更新的行;
该行若已不存在,服务端发 `transcript_reset`。一个会话同时只允许一个进行中的 run
(第二个 `POST /runs` 返回 409)。

| 事件 | payload | 备注 |
|---|---|---|
| `run_started` | `{run_id}` | 前端在此清该 run 的临时块+offset(重放幂等锚点) |
| `token` / `thinking_delta` / `thinking` | `{run_id, delta, offset}` / `{run_id, delta, offset}` / `{run_id, text, offset}` | |
| `tool` | `{run_id, kind:"call"\|"result", tool_call_id, name, args?\|result?, state?}` | |
| `plan` | `{run_id, plan_steps}` | |
| `message` | 完整持久化消息行 | 按 id 去重;助手/工具/推理行替换该 run 同类临时块 |
| `transcript_reset` | `{session_id}` | 客户端持有的行已被删除(回退重发),重新拉取整段 transcript |
| `pending_confirmation` / `pending_question` | 同 §2 字段 + `run_id` | 渲染确认/提问卡 |
| `pending_resolved` | `{run_id, tool_call_id, offset}` | 卡片已被回应(可能在其他标签页),移除 |
| `run_paused` | `{run_id}` | PAUSED run 对新连接会一次性重放(找回卡片) |
| `run_stream_end` | `{run_id, paused}` | 本连接对该 run 的 token 跟流结束,**不代表 run 结束** |
| `run_complete` | `{run_id, status, result_summary}` | 助手消息已先落库;失败的 run 另有一条 system 消息说明原因 |
| `done` | `{}` | 仅表示本次连接周期结束,**不代表 run 结束** |
| `error` | `{detail}` | detail 不含内部细节 |

前端纪律:未知事件名 `console.warn`,绝不静默丢弃(踩过 `message` 没认导致"无返回"的坑)。

## 6. 鉴权与身份(机器路径)

- worker→平台:per-run JWT(`foundation_token`);LLM 走 gateway `X-LLM-Virtual-Key`。
  worker 内置适配器(`src/adapter.ts`)在 `localhost` 上监听,SDK 经 `ANTHROPIC_BASE_URL`
  指向它;适配器把 Anthropic Messages 转为网关的 OpenAI 兼容 `chat/completions`,并注入
  `Authorization` / `X-Tenant-Id` / `X-User-Id` / `X-LLM-Virtual-Key`(broker 模式另带 `X-Tool-Broker-Lease`)。
- per-run token 的 scope 不含 `audit.write`:沙箱内不直接写 foundation audit。
- 服务账号(如 cron-runner)调 BFF:`X-Service-Token`(hmac 比对 `BFF_SERVICE_TOKEN`)+
  `X-Tenant-Id` + `X-User-Id`。**user id 必须形如 `usr_[0-9A-Z]{26}`**——下游 RBAC
  守卫第一道是格式正则,不匹配直接 403、不询问 authz。

## 7. 沙盒 egress / tool-broker 契约

调度器按 `SCHEDULER_SANDBOX_EGRESS_MODE` 选择沙盒网络面:

| mode | Docker 行为 | worker 看到的服务 URL |
|---|---|---|
| `foundation` | 兼容模式:加入 `SCHEDULER_DOCKER_NETWORK` | 真实 foundation 服务地址 |
| `broker` | 加入 `SCHEDULER_SANDBOX_BROKER_NETWORK` | `TOOL_BROKER_URL` 下的 façade URL |
| `none` | `network_disabled=True` | 网络服务 URL 置空 |

`broker` 模式下,worker 不应拿到 Redis/MinIO/MCP/LLM/WebFetch 的真实内网地址。
调度器同时注入短期 `TOOL_BROKER_LEASE`,由 worker 快照后从进程 env 清除。当前
façade 约定:

- `LLM_GATEWAY_URL = {TOOL_BROKER_URL}/llm-gateway/v1`
- `MCP_REGISTRY_URL = {TOOL_BROKER_URL}/mcp/v1`
- `SKILL_STORE_URL = {TOOL_BROKER_URL}/skills/v1`
- `WEB_FETCH_GATEWAY_URL = {TOOL_BROKER_URL}/web-fetch/api/web-fetch/v1`
- `DLP_URL = {TOOL_BROKER_URL}/dlp/api/dlp/v1`(`foundation` 模式为真实 DLP 地址,
  `none` 模式为空,即 worker 端 DLP 关闭,见 §8)
- `REDIS_URL = ""`, `S3_ENDPOINT_URL = ""`

worker 启动后必须在工具执行前清理控制面 env。Bash 可以保留,但其子进程不得继承
`FOUNDATION_API_TOKEN`、租户/用户/run id、真实服务地址、`TOOL_BROKER_URL`、
`TOOL_BROKER_LEASE`、`AGENT_TASK_JSON`、`DLP_URL`、`DLP_FAILURE_MODE` 等控制面变量。
完整服务调用由 worker runtime 使用启动时快照的凭据和 broker URL 完成;Bash 只能在
网络策略允许的范围内访问。

tool-broker 必须校验 `X-Tool-Broker-Lease` 中的 tenant/run 绑定、过期时间和 façade
scope:

- `llm-gateway/v1/*` 需要 `llm-gateway.invoke`
- `mcp/v1/*` 需要 `mcp.read_internal`
- `skills/v1/*` 需要 `skill.read_internal`
- `web-fetch/api/web-fetch/v1/*` 需要 `web_fetch.invoke`
- `dlp/api/dlp/v1/*` 需要 `dlp.scan`,且只放行 `core/scan` 一个路径。broker 丢弃
  调用方的 `X-Actor` / `X-User-Id` / `X-Tenant-Id` / `X-Service-Scopes`,只按租约
  重新注入租户与用户,所以沙箱无法冒充 `SERVICE` 改写分类标签(见 §8.4)

## 8. DLP 策略(worker 工具调用 + orchestrator 上传/下载/助手回复)

worker 的 PreToolUse 钩子(`worker-claude-sdk/src/dlp.ts`)与 session-orchestrator 的
`api/dlp_guard.py` 执行同一张决策表。DLP 不可用时怎么办由部署显式配置,不存在
静默放行。

### 8.1 配置

| 组件 | DLP 地址 | 失败策略 |
|---|---|---|
| worker | env `DLP_URL`(scheduler 下发,见 §7) | env `DLP_FAILURE_MODE` |
| session-orchestrator | `SESSION_ORCHESTRATOR_FOUNDATION_DLP_URL` | `SESSION_ORCHESTRATOR_DLP_FAILURE_MODE` |

失败策略取 `closed`(默认)或 `open`,不区分大小写;空值即 `closed`;其他值按
`closed` 处理并记一条告警/错误日志。生效策略只记一次日志:worker 在启动时,
orchestrator 在第一次 DLP 判定时。

### 8.2 决策表

| 情况 | 结果 |
|---|---|
| DLP 地址为空 | DLP 关闭:全部放行,不发请求 |
| DLP 正常应答 | 按 DLP 结论(见 8.3) |
| 无法完成:网络错误、超时(2 s)、非 2xx、响应体不是 JSON 对象、判定字段(`action` / `allowed`)缺失或类型不对;worker 还包括工具入参无法序列化 | `closed`:拒绝。`open`:放行,且每一次都记 ERROR 日志(含资源 id 与原因) |

拒绝时给用户/模型的文字只说明"DLP 检查无法完成、稍后再试",不含 DLP 地址、
令牌或底层错误;技术原因只进日志。

### 8.3 各挂载点

**worker 工具调用(PreToolUse,每次工具调用前)**

- 请求:`POST {DLP_URL}/core/scan`,体 `{content, resource_type: "tool_call",
  resource_id: "<tool>:<tool_use_id>"}`,`content` 为 JSON 序列化的工具入参;头
  `authorization: Bearer <FOUNDATION_API_TOKEN>`、`x-tenant-id`,broker 模式另带
  `x-tool-broker-lease`。worker 不发 `x-actor`。
- 只拦 `QUARANTINE`(L4 及以上):拒绝原因带命中规则名(最多 3 个)、敏感级别和
  action。`ALLOW` 放行;`SCAN_REQUIRED`(L2/L3,如邮箱、手机号)也放行,但记一条
  `console.warn`(资源 id、级别、规则名,不含数据本身)。契约之外的 action 视同
  无法完成,按下一条处理。
- 无法完成:`closed` 拒绝(原因说明扫描无法完成、工具调用不会在未扫描时执行);
  `open` 放行并 `console.error` 记 `UNSCANNED`。
- 只有拒绝时钩子才返回 `permissionDecision: "deny"`;放行时不返回任何决定,
  以免绕过 HITL 的 `canUseTool`。

**附件 / 工作区文件上传**

- 在写 blob 之前调用 `POST {dlp}/core/scan`,头 `X-Tenant-Id`、`X-User-Id`、
  `X-Actor: SERVICE`。只扫文本(含 NUL 字节或非 UTF-8 的二进制文件不扫),最多前
  256 KiB。
- 命中不拒绝上传:DLP 以命中级别写入该文件的分类标签(新标签下限为 DLP 默认级别
  L3,只升不降),下载时由 `check:download` 执行。
- 无法完成:`closed` 返回 HTTP 503 `AGENT.DLP.UNAVAILABLE`,请求事务回滚,不落 blob;
  `open` 照常存储(未扫描、无标签)并记 ERROR `UNCHECKED`。

**附件 / 工作区文件下载**

- 返回字节前调用 `POST {dlp}/core/check:download`,头 `X-Tenant-Id`、`X-User-Id`,
  体 `{resource_type, resource_id, destination}`。用户 clearance 由 DLP 向 identity
  解析,不接受调用方传入(多余字段 422)。无标签的资源按 DLP 默认级别 L3 判定。
- `allowed: false`:HTTP 403,`detail` 为 `DLP: <reason>`;`allowed: true`:放行。
- 无法完成:`closed` 返回 HTTP 503 `AGENT.DLP.UNAVAILABLE`,不返回文件;`open` 返回
  文件并记 ERROR `UNCHECKED`。

**助手回复落库(run 收尾,`resource_type: "transcript"`)**

- `POST {dlp}/core/scan`,`X-Actor: SERVICE`;命中只打标签,从不拦截。
- 无法完成时两种模式行为相同:照常落库、不打标签,记 ERROR `UNLABELLED`(含消息
  id),DLP 恢复后可据此重扫。不拦截、不扣留、不脱敏,原因:
  - 回复在 run 进行中已经按 token 流式推给了用户,收尾落库时拒存或隐藏无法阻止
    任何泄露,只会让用户丢掉刚看到的记录;
  - 脱敏需要的正是这次失败的扫描结果;
  - agent-platform 没有任何访问判定读取 transcript 标签,而 DLP 对无标签资源按
    默认级别 L3 处理。
  真正的出口控制(工具调用、文件下载)在 DLP 不可用时仍按失败策略拒绝。

agent 产出的 artifact 落库时不扫描(只有 s3_key,没有内容),其下载仍经
`check:download`。

### 8.4 foundation-dlp 侧约定

- `/core/scan` 对任何带租户上下文的调用方返回结论。只有 `X-Actor` 为 `SERVICE` /
  `ADMIN` 的调用方会把命中级别写入资源分类标签;其他调用方(如沙箱 worker 扫描
  自己的工具调用)只得到结论,不写标签。每次命中都会记 DLP 事件。
- `check:download` 必须带 `X-User-Id`(否则 401),请求体只允许
  `resource_type` / `resource_id` / `destination`。
