# claude-sdk-spike

A **1-day, self-contained spike**. It is fully decoupled from the rest of
`agent-platform` — nothing here imports or modifies `agentscope-ext/`,
`services/`, or any existing config.

## What this spike proves (the three things)

1. **LLM traffic can route through the existing llm-gateway.** An agent started
   with the TypeScript **Claude Agent SDK** sends Anthropic-protocol requests to
   a thin adapter, which translates to OpenAI protocol and forwards to the
   self-hosted `llm-gateway`, which routes to **DeepSeek**.
2. **Tenant identity rides the token.** The adapter resolves `tenant_id` and
   `virtual_key` out of the inbound bearer token and injects the gateway's three
   required headers.
3. **Tool execution is hook-gated.** A `PreToolUse` hook `await`s a (mocked) DLP
   check and **denies** the `Bash` tool before it runs — so the agent never
   actually executes `echo`.

## Architecture (text diagram)

```
  ┌────────────────────────┐   Anthropic Messages         ┌──────────────────┐
  │ Claude Agent SDK        │   POST /v1/messages          │ adapter.ts        │
  │ (src/runner.ts)         │   Authorization: Bearer <b64>│ (Node http)       │
  │  + PreToolUse hook      │ ───────────────────────────▶ │  :8788            │
  │    (DLP gate on Bash)   │                              │                   │
  └────────────────────────┘                              │  decode token →   │
            ▲                                              │  tenant/vkey/upst │
            │ ANTHROPIC_BASE_URL=http://localhost:8788     │  Anthropic→OpenAI │
            │ ANTHROPIC_AUTH_TOKEN=<b64 token>             └─────────┬─────────┘
            │                                                        │ OpenAI /chat/completions
            │                                                        │ Authorization: Bearer <upstream>
            │                                                        │ X-Tenant-Id / X-LLM-Virtual-Key
            │                                                        ▼
            │                                              ┌──────────────────┐
            │                                              │ llm-gateway       │
            │                                              │  :8011            │
            │                                              │  /api/llm-gateway │
            │                                              │  /v1              │
            │                                              └─────────┬─────────┘
            │                                                        │ routes by model
            │                                                        ▼
            │                                                  ┌───────────┐
            └──── final text + usage ◀───────────────────────│ DeepSeek   │
                                                              └───────────┘
```

`Claude Agent SDK → adapter(:8788) → llm-gateway(:8011) → DeepSeek`

## Token scheme (spike-only)

The single Anthropic-style bearer is `base64( JSON )` of:

```json
{ "tenant_id": "...", "virtual_key": "...", "upstream_token": "..." }
```

- `runner.ts` **encodes** it from `RUNNER_TENANT_ID` / `RUNNER_VIRTUAL_KEY` /
  `RUNNER_UPSTREAM_TOKEN` and sets it as `ANTHROPIC_AUTH_TOKEN`.
- `adapter.ts` **decodes** it and injects the gateway's three headers
  (`Authorization: Bearer <upstream_token>`, `X-Tenant-Id`, `X-LLM-Virtual-Key`).

> This is a spike shortcut. Production must NOT carry upstream creds inside the
> client token — see "spike → 生产的下一步".

---

## Prerequisites

1. **Node 18+** (the SDK spawns a `claude-code` CLI child process that requires
   it). This repo was scaffolded against Node 22; anything ≥18 should work.
2. **Install deps** (this spike intentionally ships no `node_modules`):
   ```bash
   cd spikes/claude-sdk-spike
   npm install
   ```
3. **Start the llm-gateway.** Bring it up via the saas-foundation compose stack
   (out of scope here — use that repo's compose). It must be reachable at the
   `LLM_GATEWAY_URL` you set (default `http://localhost:8011/api/llm-gateway/v1`).
   Sanity check:
   ```bash
   curl -s http://localhost:8011/api/llm-gateway/v1/healthz   # -> {"status":"ok"}
   ```
4. **Register a DeepSeek provider on the gateway** (one-time human step; this
   spike does NOT do it). Example shape:
   ```bash
   curl -X POST http://localhost:8011/api/llm-gateway/v1/providers \
     -H "Authorization: Bearer <admin-or-upstream-token>" \
     -H "X-Tenant-Id: <tenant>" \
     -H "Content-Type: application/json" \
     -d '{
       "name": "deepseek",
       "api_base": "https://api.deepseek.com/v1",
       "api_key": "<your-deepseek-api-key>",
       "provider_type": "openai_compatible",
       "models": ["deepseek-chat"]
     }'
   ```
   > Exact field names / route may differ in your gateway version — adjust to
   > match. The point is: `deepseek-chat` must be a model the gateway can route.
5. **Obtain the tenant's `virtual_key`** (and the upstream bearer the gateway
   expects). Put them in your env (next section).

## Configure env

Copy the example and fill it in. This spike does **not** auto-load `.env` —
either export the vars in your shell, or prefix the commands with a loader:

```bash
cp .env.example .env
# then either: export each var,  OR  use a dotenv shim, e.g.:
#   npx dotenv-cli -e .env -- npm run start:adapter
```

Key vars (see `.env.example` for the full list + comments):

| var | used by | meaning |
|-----|---------|---------|
| `ADAPTER_PORT` | both | adapter port (default 8788); runner points at it |
| `LLM_GATEWAY_URL` | adapter | gateway base, e.g. `http://localhost:8011/api/llm-gateway/v1` |
| `ADAPTER_TARGET_MODEL` | adapter | model the gateway routes to DeepSeek, e.g. `deepseek-chat` |
| `RUNNER_TENANT_ID` | runner | tenant encoded into the token |
| `RUNNER_VIRTUAL_KEY` | runner | virtual key encoded into the token |
| `RUNNER_UPSTREAM_TOKEN` | runner | the real Bearer the gateway expects |

## How to run

Two terminals (env exported/loaded in both):

```bash
# Terminal 1 — start the adapter
npm run start:adapter
# -> [adapter] listening on http://localhost:8788

# Terminal 2 — run the SDK agent
npm run start:runner
```

(Optional) typecheck / build:

```bash
npm run typecheck   # tsc --noEmit
npm run build       # emits dist/
```

## How to see the evidence for each of the three claims

After a run, look at the two terminals and `out/`:

1. **Traffic goes through the gateway** —
   - adapter log: `[adapter] forwarding to gateway model=deepseek-chat ... -> http://localhost:8011/...`
   - cross-check the **llm-gateway's own logs** showing the inbound
     `/chat/completions` request for that tenant.
2. **Tenant resolved from token** —
   - adapter log: `[adapter] resolved tenant=<x> virtual_key=<y> from token`
     (printed on every inbound request, before forwarding).
3. **Tool blocked by the hook** —
   - runner log: `[runner] PreToolUse Bash -> DENY (spike DLP: Bash blocked ...)`
   - the agent's final answer does **not** contain real `echo` output (it was
     never executed); `out/events.jsonl` contains a `pre_tool_use.deny` line.

`out/` artifacts (field shapes mirror the existing `runtime_worker`):

- `out/result.json` — `{ status, summary, error, usage:{input_tokens,output_tokens,total_tokens}, plan_steps:[] }`
- `out/events.jsonl` — one JSON object per line, **each with an `event_type`**
  field (e.g. `assistant`, `tool_use`, `pre_tool_use.deny`, `result.success`).
  > These `event_type` values are a **stub vocabulary** (`message.type[.subtype]`)
  > to prove the event pipe works. They are **not** aligned to agentscope's event
  > class names — mapping is future work.

---

## 实测待确认 (verify after installing / running)

These were assumed from docs but not yet exercised in this environment:

- **`options.env`**: whether the SDK lets you pass `env` in `options` to override
  the base URL / token. This spike avoids it and sets
  `process.env.ANTHROPIC_BASE_URL` / `process.env.ANTHROPIC_AUTH_TOKEN` **before**
  `query()`. Confirm the child process actually inherits them.
- **CLI binary in container**: the SDK spawns a `claude-code` CLI subprocess.
  Confirm it ships with the npm package (or is otherwise on PATH) in your
  runtime/container image — not just on a dev laptop.
- **Node version**: confirm the runtime is ≥18 (subprocess requirement).
- **SDK type exports**: `HookCallback` / `PreToolUseHookInput` are referenced
  conceptually but the code uses a minimal local `PreToolUseHookInputLike` +
  `as any` so it compiles pre-install. After `npm install`, swap in the real
  exported types and drop the casts.
- **Hook return contract**: confirm `permissionDecision: "deny"` actually
  prevents execution (vs. just annotating). The evidence is the agent never
  producing real `echo` output.
- **Streaming**: NOT implemented. The adapter forces `stream:false` both inbound
  and to the gateway. SSE translation is a TODO.
- **Provider registration shape**: the `/providers` curl above is illustrative;
  confirm against your gateway's actual API.

## spike → 生产的下一步 (placeholder)

- **Where the adapter lives.** Two options to weigh:
  (a) fold this translation into a Claude-backend `runtime_worker` (keep the
  gateway untouched), or (b) add a native `/v1/messages` endpoint to
  `llm-gateway` itself (no sidecar, but couples Anthropic protocol into the
  gateway). Decide based on how many SDK-based agents you expect.
- **Events format mapping.** Map the stub `event_type` vocabulary here to
  agentscope's real event classes so the SDK runner's stream is consumable by
  the existing pipeline.
- **Real DLP integration.** Replace the `dlpCheck` mock in `runner.ts` with a
  `fetch()` to the real DLP service (wire the `AbortSignal` for cancellation;
  define fail-open vs fail-closed policy).
- **Scheduler backend routing.** Teach the scheduler to dispatch certain jobs to
  this Claude-SDK backend (vs the existing agentscope backend), including how
  tenant/virtual_key are resolved server-side rather than stuffed in the token.
- **Token model.** Drop the base64-creds shortcut: resolve tenant / virtual_key /
  upstream_token from an authenticated session on the adapter side.
- **Streaming.** Implement OpenAI-delta → Anthropic-event SSE translation in the
  adapter for token-by-token UX.
