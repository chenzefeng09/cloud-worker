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
  "history": [{"role","type",...}, ...],  // 多轮历史(有损 dict);resume 时忽略
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

| 条目 `type` | 其余字段 | 语义 |
|---|---|---|
| `delta` | `text` | 助手正文增量 |
| `thinking` | `text` | 推理文本(整块) |
| `tool_call` | `tool_call_id`, `name`, `args`(JSON 字符串) | 工具调用(参数已完整) |
| `tool_result` | `tool_call_id`, `name`, `result`, `state` | 工具结果 |
| `pending_confirmation` | `tool_call_id`, `name`, `args` | HITL:等用户批准(一个待批工具一条) |
| `pending_question` | `tool_call_id`, `question` | HITL:ask_user 等用户回答 |
| `end` | — | 回复正常结束。**暂停时不发 end** |

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

## 4. 输出③:暂停状态 `agent:run:{run_id}:paused`(SET,TTL 7 天)

```jsonc
{
  "kind": "confirm" | "question",
  "reply_id": "string",                  // 续跑时 reply_id 延续,不再发 ReplyStart
  "tool_calls": [{"id","name","args"}],
  "question": "string|null",
  "agent_state": {...},                  // worker 自定义的完整可恢复状态
                                         // (Claude SDK 会话恢复载荷),格式由 worker 定,
                                         //   平台只负责原样回传,不解析
  "paused_at": "iso8601"
}
```

续跑:orchestrator `POST /sessions/{sid}/runs/{run_id}/respond` 读此 key →
同一 Run 行 PAUSED→RUNNING → 重派 scheduler,`task.json["resume"]` 带回
`agent_state` + 用户决定;worker 凭 state 还原并注入确认/答案事件续跑,
token 续写**同一条**流(同 run_id)。同时 worker 更新热状态键
`agent:run:{run_id}:state` 的 `status` 字段为 `"PAUSED"`。

## 5. 平台 SSE 事件(orchestrator `GET /sessions/{id}/stream` → BFF relay → 前端)

session-scoped、multiplex 该会话所有 run、5 分钟服务端 deadline(客户端自动重连)。

| 事件 | payload | 备注 |
|---|---|---|
| `run_started` | `{run_id}` | 前端在此清该 run 的临时块+offset(重放幂等锚点) |
| `token` / `thinking` | `{run_id, delta}` / `{run_id, text}` | |
| `tool` | `{run_id, kind:"call"\|"result", tool_call_id, name, args?\|result?, state?}` | |
| `plan` | `{run_id, plan_steps}` | |
| `message` | 完整持久化消息行 | `addMessage` 按 id 去重 |
| `pending_confirmation` / `pending_question` | 同 §2 字段 + `run_id` | 渲染确认/提问卡 |
| `run_paused` | `{run_id}` | PAUSED run 对新连接会一次性重放(找回卡片) |
| `run_complete` | `{run_id, status, result_summary}` | 助手消息已先落库 |
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
- `REDIS_URL = ""`, `S3_ENDPOINT_URL = ""`

worker 启动后必须在工具执行前清理控制面 env。Bash 可以保留,但其子进程不得继承
`FOUNDATION_API_TOKEN`、租户/用户/run id、真实服务地址、`TOOL_BROKER_URL`、
`TOOL_BROKER_LEASE`、`AGENT_TASK_JSON` 等控制面变量。完整服务调用由 worker
runtime 使用启动时快照的凭据和 broker URL 完成;Bash 只能在网络策略允许的范围内访问。

tool-broker 必须校验 `X-Tool-Broker-Lease` 中的 tenant/run 绑定、过期时间和 façade
scope:

- `llm-gateway/v1/*` 需要 `llm-gateway.invoke`
- `mcp/v1/*` 需要 `mcp.read_internal`
- `skills/v1/*` 需要 `skill.read_internal`
- `web-fetch/api/web-fetch/v1/*` 需要 `web_fetch.invoke`
