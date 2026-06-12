# Claude Agent SDK Spike — 结论

**日期**: 2026-06-12
**目标**: 验证能否以"第二档(配置期可切换 + 灰度 + 回滚)"为目标,把 TypeScript Claude Agent SDK 接入 agent-platform 作为可选 agent 后端。

本 spike 实际跑通,**三件核心事现已全部经真实 llm-gateway 端到端验证通过**(不再有"直连 DeepSeek 隔离掉网关"的取巧),并沉淀了若干对生产决策有价值的发现。

---

## 三件核心事:验证结果

| # | 要验证的 | 结果 | 证据 |
|---|---|---|---|
| 1 | Claude SDK 的 LLM 流量能强制走自定义网关 | ✅ | SDK 设 `ANTHROPIC_BASE_URL` 后,流量经 adapter 转发到**真实 llm-gateway**;adapter 日志 `forwarding to gateway model=deepseek-v4-pro (inbound model=claude-sonnet-4-5-...) -> http://localhost:8021/api/llm-gateway/v1/chat/completions`;真网关路由到 v4-pro 并真实返回 |
| 2 | 租户身份通过单个 token 传递、网关侧解析 | ✅ | `ANTHROPIC_AUTH_TOKEN` 携带 base64(JSON) per-run token;adapter 解析后注入真网关要的三个 header(`Authorization`/`X-Tenant-Id`/`X-LLM-Virtual-Key`),真 llm-gateway 接受并按 `X-Tenant-Id`+`X-LLM-Virtual-Key` 完成租户/配额路由 |
| 3 | 工具执行前可同步阻断 + hook 内异步远程调用 | ✅ | 经真网关路由的 **v4-pro 发起真实 `tool_use`**;`events.jsonl`:`tool_use Bash` → `pre_tool_use.deny`(来自 `await` 的 mock DLP)→ echo/ls/git/pwd 全程被拦,`echo hello` 始终未执行,agent 如实收尾 |

### 额外确认的事实(spike 前是不确定项)

- **ESM env 风险不存在**:`sdk.mjs` 自身不读 `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN`,而是 `{...process.env}` 整个透传给它 `spawn` 出来的 `cli.js` 子进程,且子进程在 `query()` 调用时才起。→ runner 现状(import 后、调用前设 env)正确,无需改成 `await import()`。
- **CLI 二进制自带**:`node_modules/@anthropic-ai/claude-agent-sdk/cli.js` 随包提供,容器化无需单独装 claude-code。
- **SDK 类型核对一致**:`HookCallback` 签名 `(input, toolUseID, {signal})`、`PreToolUseHookInput.{tool_name, tool_input, tool_use_id}`、返回 `hookSpecificOutput.{hookEventName, permissionDecision:'allow'|'deny'|'ask', permissionDecisionReason}` —— 与 runner 写法完全对上。类型定义在 `entrypoints/sdk/coreTypes.d.ts` 和 `runtimeTypes.d.ts`(不在顶层 `sdk.d.ts`)。
- **环境**:Node v22.14、npm 10.9 跑通;`npm install` 仅 10 个包。

---

## 重要发现:后端模型的 tool-calling 能力是选后端时要纳入的因素(v4-pro 已验证过关)

这是本轮一个被推翻又被重新定性的结论,教训价值保留:

- 早先用 `deepseek-chat` 时,它**不发起真正的 `tool_use`**——把"用 Bash"理解成输出字面文本 `<bash>echo hello</bash>`,而不是按 Claude Agent SDK 期望的工具调用协议走。当时第 3 件事只能靠 adapter 注入一个**合成的 Bash tool_use**(`ADAPTER_FORCE_BASH=1`)隔离验证。
- **本轮换成 `deepseek-v4-pro` 后,这个约束消解了**:v4-pro **原生支持真实 function calling**。curl DeepSeek `/v1/chat/completions` 带 `tools` 会返回 `finish_reason: tool_calls` + 结构化 `tool_calls:[{function:{name:"run_bash", arguments:"{\"command\":\"echo hello\"}"}}]`,与 deepseek-chat 吐字面文本是天壤之别。所以**第 3 件事现在是用 v4-pro 的真实工具调用、经真网关验证的,不再需要 `ADAPTER_FORCE_BASH` 合成**。

结论分两层:

- **hook 拦截机制本身完全可用**(✅,已证,且现在是被真实 tool_use 触发)。
- **但它能不能拦到东西,取决于后端模型会不会按 SDK 协议发起 tool_use**——deepseek-chat 不行、v4-pro 行,就是正反两个例证。

**生产含义**:后端工具调用能力**确实是选后端时必须纳入评估的一项**。本 spike 用的 v4-pro 这个具体后端已经过关(真实发起 tool_use、经 adapter 双向转换、被 hook 拦截),但换其它后端时仍需逐一评估其工具调用能力——对工具调用弱的后端,中间层契约里"工具执行前拦截"这条会形同虚设(模型根本不发起 tool_use)。

---

## adapter:已做真正的双向 tool 协议转换

本轮 adapter 不再只转文本,做了完整的双向工具协议转换:

- **请求方向**:Anthropic `tools` → OpenAI `functions`;assistant 的 `tool_use` blocks → OpenAI `tool_calls`;user 的 `tool_result` blocks → OpenAI `{role:"tool"}` 消息(按 `tool_call_id` 配对)。
- **响应方向**:OpenAI `tool_calls` → Anthropic `tool_use` blocks,并据此设 `stop_reason:"tool_use"`。
- 加了 tool-loop 一致性清洗(去重 + 丢弃孤立 tool 消息)。

## 实测发现 / 修的 bug

(以下 2、3 都是真做多轮工具循环才暴露的)

1. **SDK 给 `/v1/messages` 加了 `?beta=true` 查询串** —— adapter 原来精确匹配 `req.url === "/v1/messages"` 导致 404。已修:改为 strip 查询串后匹配 `pathname`。
2. **v4-pro/DeepSeek 有时在一次响应里返回两个相同 id 的 `tool_call`** —— 原样转成两个 `tool_use` 会让下一轮 round-trip 产生重复 `tool_call_id`,被 DeepSeek 以 `Duplicate value for 'tool_call_id'` 400 拒绝。已修:在**响应转换端**按 id 去重 `tool_calls`。
3. **hook deny 之后,SDK 会在历史里同一条 assistant 消息内重复同一个 `tool_use`**(同 id 出现两次)—— 同样导致重复 `tool_call_id` 400。已修:在**请求转换端**,对每条 assistant 消息内的 `tool_use` blocks 按 id 去重。

## 未验证 / spike 范围外(刻意不做)

- **流式(SSE)**:adapter 强制 `stream:false`,未实现 Anthropic SSE 透传。
- **usage 透传不完整(小保真度问题,诚实记录)**:端到端经真网关跑 `status=OK`,但 runner 日志里 `usage in=0 out=0 cost_usd=0`。推测是 adapter 响应转换里 usage 字段映射没完全对上 SDK 读取 Anthropic usage 的路径(SDK 期望的 usage 字段位置/命名与 adapter 产出的不一致)。不影响功能(工具循环、拦截、路由都正常),但生产化时要修好 usage 透传,否则计费/配额统计在 SDK 侧会显示 0。
- **events.jsonl 事件名**:是打桩词表(`message.type[.subtype]`),未对齐 agentscope 事件类名。
- **Redis 检查点 / MCP 注入 / skills / S3 / 审计 / scheduler 路由**:全部未做。
- **`options.env` 传 base_url**:未用,靠 `process.env` 改写(已证可行,见上)。

---

## 本轮最大成果:真实 llm-gateway 端到端联调已补完

上一版唯一的留白(真实 llm-gateway 那一跳)本轮已打通:

- 起了真实 llm-gateway 全栈(docker compose,容器名 `foundation-llm-gateway`,宿主机 `localhost:8021`,内部前缀 `/api/llm-gateway/v1`)。
- **网关 API 契约(实测)**:
  - 健康检查 `GET /api/llm-gateway/v1/healthz`(不是 `/healthz`)。
  - 推理入站 `POST /api/llm-gateway/v1/chat/completions`(**OpenAI 格式**,不是 Anthropic 格式——正好和 adapter 的输出对上)。
  - 三个 header:`X-Tenant-Id`(必须,正则 `tnt_[0-9A-Z]{26}`)、`X-LLM-Virtual-Key`(必须,必须是真实建出来的 `sk-foundation-*`,dev 下不放宽、不能用假值)、`Authorization: Bearer`(dev 下是 opaque,不校验 JWT)。
- 注册 DeepSeek provider:`POST /providers`,`provider_id=llmp_01KTWGG7KXATBZQCEKFE7S96Y3`,`api_base=https://api.deepseek.com/v1`,`models=[deepseek-v4-pro, deepseek-v4-flash]`。
- 建真 virtual_key:`POST /tenant-keys`,得到 `sk-foundation-01KTWGH3CFFC8KY4VTH153C0WZ`。
- 直连网关验证路由:返回 `model:deepseek-v4-pro`、`usage` 有真实 token 计数(prompt 15 / completion 30,含 reasoning_tokens),证明真路由到 v4-pro 且配额/计费生效。
- 端到端跑:SDK → adapter(注入三 header,`LLM_GATEWAY_URL=http://localhost:8021/api/llm-gateway/v1`)→ **真 llm-gateway** → v4-pro,`result status=OK`。v4-pro 发起真实 `tool_use`,PreToolUse hook 全程拦截(echo/ls/git/pwd 全 deny,`echo hello` 始终没执行),agent 如实收尾。

至此**三件核心事全部经真网关验证**,不再有"直连 DeepSeek 隔离掉网关"的取巧。

### 运维备忘(磁盘满后遗症)

之前本机磁盘写满,导致 `foundation-postgres` 的 `postmaster.pid` 和 `/var/run/postgresql` 的 socket lock 文件被写成空文件,postgres 起来就 crash loop(报 `bogus data in lock file`)。修法:清磁盘后 `docker rm -f foundation-postgres` + `docker compose up -d postgres` 重建容器(数据卷 `saas-foundation-dev_pg_data` 保留,数据不丢)。

---

## 怎么复现(本 spike)

```bash
cd spikes/claude-sdk-spike
npm install

# 阶段一:adapter 直连 DeepSeek,验证文本链路 + token 解租户
ADAPTER_PORT=8788 LLM_GATEWAY_URL=https://api.deepseek.com/v1 \
  ADAPTER_TARGET_MODEL=deepseek-v4-pro npm run start:adapter &
ADAPTER_PORT=8788 RUNNER_TENANT_ID=tnt_01J5DEVXX00000000000000001 \
  RUNNER_VIRTUAL_KEY=vk-spike RUNNER_UPSTREAM_TOKEN=<DEEPSEEK_KEY> npm run start:runner

# 阶段二:验证 PreToolUse hook 拦截
# v4-pro 原生发起真实 tool_use,不再需要 ADAPTER_FORCE_BASH 合成
# 直接跑 runner，看 events.jsonl 出现 tool_use -> pre_tool_use.deny
# (历史:deepseek-chat 不发起真实 tool_use 时,才需 ADAPTER_FORCE_BASH=1 合成隔离验证)

# 阶段三:经真实 llm-gateway 端到端(本轮补完)
# 1) 起网关全栈后,注册 DeepSeek provider:
#    POST /api/llm-gateway/v1/providers  api_base=https://api.deepseek.com/v1  models=[deepseek-v4-pro, deepseek-v4-flash]
# 2) 建真 virtual_key:POST /api/llm-gateway/v1/tenant-keys  -> 得到 sk-foundation-*
# 3) adapter 指向真网关并跑 runner:
ADAPTER_PORT=8788 ADAPTER_TARGET_MODEL=deepseek-v4-pro \
  LLM_GATEWAY_URL=http://localhost:8021/api/llm-gateway/v1 npm run start:adapter &
# runner 的 RUNNER_VIRTUAL_KEY 用上一步建出的 sk-foundation-* 真值(dev 下不放宽)
```

DeepSeek key 来源:`agent-platform/deploy/docker-compose/.env` 的 `LLM_API_KEY`。

---

## spike → 生产 的下一步(第二档落地)

按优先级:

1. ✅ **已完成 — 端到端补验真实 llm-gateway 那一跳**:已起 llm-gateway 全栈、注册 DeepSeek provider(`POST /providers`)、建真 virtual_key(`POST /tenant-keys`),adapter 的 `LLM_GATEWAY_URL` 指向 `http://localhost:8021/api/llm-gateway/v1`,三 header 被真网关接受并路由到 v4-pro(`status=OK`,详见上文"本轮最大成果"节)。dev tenant 已预置 `tnt_01J5DEVXX00000000000000001`,Authorization 在 dev 下为 opaque、不校验 JWT。

2. **adapter 的 tool_calls ↔ tool_use 双向转换:已做基础版,生产需加固**——基础双向转换 + tool-loop 去重清洗已在本 spike 里落地并验证(v4-pro 真实工具调用经此跑通)。生产化还需:SSE 流式转换、usage 透传(修上文记录的 `usage in=0 out=0`)、错误映射。后端工具调用能力仍需逐后端评估(v4-pro 已过关)。

3. **adapter 的归属决策**(关键架构选择,本 spike 给了数据支撑):
   - **方案 A**:adapter 作为"Claude 后端"runtime_worker 的一部分,和 TS runtime_worker 一起打成独立 sandbox 镜像。改动隔离在 Claude 后端,不碰 saas-foundation 网关。
   - **方案 B**:在 llm-gateway 原生加 `/v1/messages`(Anthropic 入站)端点(~250 行,改 saas-foundation),复用网关现成的配额/审计/计费,架构更干净但要动公共组件。
   - 倾向:**先 A 后 B**。A 让 Claude 后端自包含、可灰度;若 Claude 后端长期化、Anthropic 入站成为常态,再把 adapter 的协议转换上沉到网关(B)。

4. **TS runtime_worker 补齐 runtime_worker 完整契约**:events.jsonl 事件格式映射(SDK 消息 → 现有事件 schema)、result.json 的 plan_steps、退出码、Redis 检查点(可选)、MCP/skills/S3(按需)。

5. **真 DLP/authz 对接**:把 runner 里的 mock `dlpCheck` 换成 `fetch` 你的 `foundation_dlp` 服务,signal 接 AbortSignal。

6. **scheduler 后端路由**:用现有 feature-flag 在 agent-scheduler 按 tenant/flag 决定 spawn 哪个 runtime_worker 镜像(AgentScope vs Claude SDK)——这是第二档"配置期切换/灰度/回滚"的最后一环。
