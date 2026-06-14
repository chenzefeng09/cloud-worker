# Worker Wire Contract — task.json / Redis 流 / 平台 SSE 事件

> **这是谁的契约**:沙箱 worker(今天是 AgentScope `agentscope_ext.runtime_worker`,将来可能是
> Claude Agent SDK worker——scheduler 已有 `claude_sdk_image` + `enable_claude_sdk_worker`
> 灰度旗标)与平台(agent-scheduler / session-orchestrator / 前端)之间的全部线上格式。
> **换 worker 框架时,只要新 worker 遵守本文件,orchestrator 与前端零改动。**
> 框架内部事件名(AgentScope 的 `TextBlockDeltaEvent`、Claude SDK 的 `content_block_delta`…)
> 一律不得外漏:worker 在自己内部把它们翻译成本契约的 Redis 条目。
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
  "mcp_servers": [],             // 恒空;mcp_tenant_injector 沙箱内实时拉取
  "skills": ["name", ...],       // skills_loader 据此解析 manifest
  "files": [{"key","filename","mime"}, ...],   // worker 下载到 /workspace/inputs/
  "memory": "string",            // 预取记忆片段,worker 前置进 system prompt
  "history": [{"role","type",...}, ...],  // 多轮历史(有损 dict);resume 时忽略
  "model_override": "string|null",
  "reasoning_effort": "string|null",
  "sys_prompt": "string",        // App harness 系统提示(聊天 run 为空串)
  "sampling_params": {},         // App harness 采样覆盖
  "hitl": true,                  // false=一刀切 BYPASS(定时/后台 run)
  "resume": null                 // 或 {"kind":"confirm"|"answer", "tool_call_id":str,
                                 //     "confirmed":bool|null, "answer":str|null,
                                 //     "agent_state":{...}}   ← 见 §4
}
```

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

**可能出现的键(至多这 7 个)**:`FOUNDATION_API_TOKEN`、`TOOL_BROKER_LEASE`、
`LLM_VIRTUAL_KEY`、`LLM_API_KEY`、`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`、
`REDIS_URL`。`TOOL_BROKER_LEASE` / `LLM_VIRTUAL_KEY` 仅在非空时出现;`LLM_API_KEY`
恒在(gateway enforcer 启用时为 `""`)。键缺失即语义上"未配置",worker 不得猜测。

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
  "agent_state": {...},                  // 框架自定义的完整可恢复状态。
                                         // AgentScope: AgentState.model_dump(mode="json")
                                         //   (mode=json 必须,否则枚举写成 repr 还原崩)
                                         // Claude SDK: 等价的会话恢复载荷,格式自定,
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
