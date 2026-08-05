# HITL Feasibility Spike — Claude Agent SDK Worker

Scope: can the `worker-claude-sdk/` runtime (using `@anthropic-ai/claude-agent-sdk`
**v0.1.77**) match the OLD agentscope worker's HITL (pause/resume + CONFIRM
tool-approval + ask_user question) behavior, and if so, exactly how?

Investigation date: 2026-06-15.

---

## VERDICT

**Feasible on v0.1.77 — yes, with caveats.**

The SDK gives us a clean BEFORE-tool-execution callback (`canUseTool`) that
exposes tool name, input, and tool_use_id, and lets us deny-and-interrupt the
run. That is enough to *pause*. The caveats are:

1. **SDK-native session resume is NOT usable in the headless-exit model.** The
   SDK persists session transcripts only to `~/.claude/projects/` on the
   sandbox's local disk (`Options.persistSession`, default true). A HITL pause
   exits the worker and the sandbox is destroyed, so a fresh worker on a fresh
   sandbox has no transcript to `resume:` from. **We must reconstruct history
   ourselves on resume** (history-replay), not use `Options.resume`.

2. **The old worker serialized `agent_state` (AgentScope's `AgentState.model_dump`)
   into the paused blob and restored it verbatim.** There is no equivalent SDK
   object we can serialize/restore. We replace `agent_state` with **our own
   replayable conversation transcript** (assistant text + tool_call/tool_result
   pairs + the pending tool_call), stored in the paused blob, and feed it back
   as the SDK prompt on resume. The Redis paused-blob *shape* stays
   wire-compatible with the orchestrator (it never inspects `agent_state` — it
   passes it through opaquely; see below), so the orchestrator/scheduler need
   **zero changes**.

3. **`canUseTool` requires we drop `permissionMode: 'bypassPermissions'`.** The
   current worker hard-codes bypass (worker.ts:448-449). Bypass skips the
   permission callback entirely. We switch to `permissionMode: 'default'` and
   gate via `canUseTool`. (Confirmed from the type docs: bypass "Bypass all
   permission checks"; `canUseTool` is "Called before each tool execution".)

4. **No `interrupt()` available** — the worker uses a single-string `prompt`
   (worker.ts:485), not streaming-input mode, and `Query.interrupt()` /
   control requests are "only supported when streaming input/output is used".
   We do NOT need `interrupt()`: returning `{behavior:'deny', interrupt:true}`
   from `canUseTool` terminates the run cleanly. (See Approach A.)

Net: behavior parity is achievable. The implementation is moderate (one new
module + edits to worker.ts/main.ts/task.ts/redis_state.ts), with the main risk
being faithful history-replay across resume legs.

---

## Decisive SDK signatures (v0.1.77)

From `node_modules/@anthropic-ai/claude-agent-sdk/entrypoints/sdk/runtimeTypes.d.ts`:

```ts
// The BEFORE-tool callback — fires before each tool executes, with name+input+id.
export type CanUseTool = (toolName: string, input: Record<string, unknown>, options: {
    signal: AbortSignal;
    suggestions?: PermissionUpdate[];
    blockedPath?: string;
    decisionReason?: string;
    toolUseID: string;          // <-- the tool_use_id we persist + match on resume
    agentID?: string;
}) => Promise<PermissionResult>;
```

```ts
// Options surface (relevant fields)
export type Options = {
    canUseTool?: CanUseTool;            // our pause hook
    permissionMode?: PermissionMode;    // must be 'default' (NOT 'bypassPermissions')
    resume?: string;                    // session id — NOT usable headless (see caveat 1)
    continue?: boolean;                 // continue most-recent — also disk-bound
    persistSession?: boolean;           // default true → writes ~/.claude/projects/ (ephemeral)
    forkSession?: boolean; resumeSessionAt?: string;   // resume sub-options, all disk-bound
    abortController?: AbortController;
    hooks?: Partial<Record<HookEvent, HookCallbackMatcher[]>>;
    ...
};
```

From `entrypoints/sdk/coreTypes.d.ts` — `PermissionResult` deny variant carries
`interrupt`:

```ts
export type PermissionResult = {
    behavior: 'allow';
    updatedInput: Record<string, unknown>;
    updatedPermissions?: PermissionUpdate[];
    toolUseID?: string;
} | {
    behavior: 'deny';
    message: string;        // "guidance of what the model should do instead"
    interrupt?: boolean;    // "If true, interrupt execution and do not continue."
    toolUseID?: string;
};
```

`PreToolUse` hook input (alternative surface, also viable but weaker — see
Approach C):

```ts
export type PreToolUseHookInput = BaseHookInput & {
    hook_event_name: 'PreToolUse';
    tool_name: string;
    tool_input: unknown;
    tool_use_id: string;
};
// Hook output PreToolUse: { permissionDecision?: 'allow'|'deny'|'ask'; permissionDecisionReason?: string }
```

`Query.interrupt()` (NOT usable — needs streaming input):

```ts
export interface Query extends AsyncGenerator<SDKMessage, void> {
    interrupt(): Promise<void>;   // "control requests ... only supported when streaming input/output is used"
    ...
}
```

The SDK can accept a **structured multi-turn transcript** as the prompt — this
is what makes history-replay cheap:

```ts
export declare function query(_params: {
    prompt: string | AsyncIterable<SDKUserMessage>;   // <-- async iterable of user messages
    options?: Options;
}): Query;

export type SDKUserMessage = {
    type: 'user';
    message: APIUserMessage;             // full Anthropic MessageParam (can carry tool_result blocks)
    parent_tool_use_id: string | null;
    tool_use_result?: unknown;
    uuid?: UUID; session_id: string;
} & ...;
```

---

## Chosen approach: **(A) deny+interrupt in `canUseTool`, exit, resume via history-replay**

### Why A over B and C

- **(A) Pause inside `canUseTool` → persist blob → `{behavior:'deny', interrupt:true}` → run ends → worker exits 0 (status="paused").**
  - *Matches the old headless-exit model exactly.* The old worker did NOT keep
    the process alive; it returned `status="paused"`, wrote the Redis paused
    blob, exited 0, and the orchestrator re-launched a fresh sandbox with
    `task.resume`. (Confirmed: `__main__.py:1562-1563` returns "paused";
    `main()` `_exit(0,...)`; scheduler maps `status:"paused"` → `PAUSED` at
    `docker_runtime.py:60` / `cube_runtime.py:85`.)
  - Zero idle resource cost — no sandbox sitting blocked for up to 7 days.
  - The orchestrator already expects exactly this (re-dispatch a *new* run via
    `POST /runs` with a `resume` payload; `runs.py:766-802`).

- **(B) Keep the worker alive, block inside `canUseTool` on a Redis poll/subscribe.**
  - Rejected. It breaks the headless contract: a paused run would hold a live
    sandbox + microVM RAM for the entire human-confirmation window (up to 7
    days per the `PAUSED_TTL_SECONDS` design). The scheduler/orchestrator are
    built around re-dispatch, not long-poll. Also the sandbox token
    (`FOUNDATION_API_TOKEN`) is short-lived (<= sandbox_timeout), so a
    long-blocked worker would lose its credentials mid-wait.

- **(C) `PreToolUse` hook returning `permissionDecision:'deny'`.**
  - Viable to *detect* + decide, but the hook output has **no `interrupt`
    field** (only `'allow'|'deny'|'ask'`); a plain `deny` feeds the model a
    denial and the model keeps going (it does not cleanly terminate the run the
    way `canUseTool`'s `interrupt:true` does). `canUseTool` is the
    purpose-built surface and gives us `interrupt` + `toolUseID` directly.
    Use `canUseTool` for HITL.

> **CORRECTION (e2e-discovered, 2026-06-15):** the PreToolUse DLP hook and the
> `canUseTool` HITL gate are NOT independent. The SDK permission resolver
> (`node_modules/@anthropic-ai/claude-agent-sdk/cli.js`) SHORT-CIRCUITS
> `canUseTool` whenever a PreToolUse hook returns
> `hookSpecificOutput.permissionDecision: "allow"` (debug string: "bypassing
> permission check"). An allow-all DLP hook therefore DISABLES HITL entirely —
> every `mcp__*`/`ask_user` call auto-runs with no pause. The DLP hook MUST stay
> **silent** (`{continue: true}`, no `permissionDecision`) on a clean scan and on
> fail-open, and return `permissionDecision: "deny"` ONLY on a real DLP hit. Only
> a hook with no `permissionDecision` lets `canUseTool` run. Additionally, after a
> canUseTool deny+interrupt the SDK emits an `is_error` tool_result for the gated
> call ("paused for human approval"); that `ToolResultEndEvent` must be suppressed
> from the Redis token stream by `tool_use_id`, or the frontend treats the
> interaction as consumed and removes the ConfirmCard (also breaks reconnect
> rehydration). See memory `claude-sdk-dlp-hitl-precedence`.

### How "approved / rejected" is fed back to the model on resume

On resume we do NOT call the SDK's session-resume. We start a **fresh `query()`**
whose prompt is an `AsyncIterable<SDKUserMessage>` we build from the saved
transcript:

- **CONFIRM approved**: replay prior turns, then re-issue the original user
  intent with an explicit instruction that the previously-blocked tool call is
  now approved — the model re-emits the tool call and it runs (canUseTool now
  allows that specific tool_use, see "approved-tool registry" below).
- **CONFIRM rejected**: replay prior turns + a synthetic user/tool message
  telling the model the tool call `<name>(<args>)` was denied by the user (carry
  the orchestrator's optional guidance text) and to continue without it.
- **QUESTION (ask_user) answered**: replay prior turns + a user message that
  delivers the human's answer as the result of the `ask_user` question, then let
  the model continue.

Because we replay our own transcript, the model resumes coherently regardless of
the sandbox being brand-new.

---

## Current new-worker state vs. what's needed (file:line)

| Concern | Current new worker | Needed |
|---|---|---|
| Permission mode | `permissionMode:'bypassPermissions'` + `allowDangerouslySkipPermissions:true` (worker.ts:448-449) | `'default'` (HITL) / keep bypass when `hitl=false` |
| Tool gate | no `canUseTool`; no-op `allowAllPreToolUse` hook (worker.ts:387-395, 451) | add `canUseTool` that pauses on MCP tools |
| Pause blob write | none — `RedisStateClient` has no `writePaused`/`markPaused` (redis_state.ts), though `paused` kind IS already routable (redis_state.ts:172, BrokerTransport SET_KINDS) | add `writePaused()` + `markPaused()` + `pushPendingConfirmation()` + `pushPendingQuestion()` |
| Resume input | `Task` has no `resume`/`hitl` field (task.ts:42-55); prompt is a single string (worker.ts:482-485) | add `resume`/`hitl` to Task; build `AsyncIterable<SDKUserMessage>` replay on resume |
| History | `historyToText()` → plain text into **system prompt** (worker.ts:165-201, 225-226) | keep for fresh runs; resume uses structured replay instead |
| Status mapping | main.ts maps only OK→0 / FAILED→1 (main.ts:264-270); no "paused" | add: result.status "paused" → write result.json status="paused", exit 0, do NOT markFailed |
| ask_user tool | none | register an in-process SDK MCP tool (or a documented tool name) that `canUseTool` treats as a question-pause |

Note: `redis_state.ts` already declares `paused` as a valid broker SET kind
(line 172) and the TTL plumbing exists — so the broker `/state/v1` façade
already accepts the paused write. We only need the client-side methods.

---

## Redis contract to honor (unchanged — this is the parity surface)

Keys (stable; scheduler + orchestrator read these):

- `agent:run:{run_id}:state` — JSON; set `status:"PAUSED"` with TTL 7d on pause.
- `agent:run:{run_id}:paused` — JSON blob, TTL **7 days** (`PAUSED_TTL_SECONDS`):
  ```json
  {
    "kind": "confirm" | "question",
    "reply_id": "<string>",
    "tool_calls": [{"id": "<tool_use_id>", "name": "<tool>", "args": "<json string>"}],
    "question": "<string|null>",
    "agent_state": { /* opaque to orchestrator — we put our replay transcript here */ },
    "paused_at": "<ISO-8601>"
  }
  ```
- `agent:run:{run_id}:tokens` — Redis Stream; on pause push (no `type=end`):
  - `type=pending_confirmation` `tool_call_id` `name` `args`  (one per pending MCP call)
  - `type=pending_question` `tool_call_id` `question`         (ask_user)

result.json on pause: `{"status":"paused", "summary":"...", "usage":{...}, "plan_steps":[]}`,
exit code **0**.

Orchestrator/scheduler proof of drop-in compatibility:

- Orchestrator reads `agent:run:{id}:paused`, never inspects `agent_state`
  beyond passing it through: `agent_state = paused_data.get("agent_state") or {}`
  then forwards it verbatim in the resume payload
  (`session-orchestrator/.../api/v1/runs.py:764, 795-801`). So whatever we store
  as `agent_state` round-trips back to us untouched.
- Resume payload the worker receives (built by orchestrator runs.py:795-801,
  forwarded by scheduler runs.py:476-479 → runtime → task.json):
  ```json
  {"kind":"confirm"|"answer","tool_call_id":"<id>","confirmed":<bool|null>,
   "answer":"<str|null>","agent_state":{...}}
  ```
  scheduler writes it to `task.json["resume"]` (docker_runtime.py:927-928,
  cube_runtime.py:304-305). Note orchestrator uses `kind:"answer"` for the
  question-resume leg while the paused blob uses `kind:"question"` — the worker
  must accept both spellings on the resume leg (old worker did:
  `__main__.py:1470` checks `resume_kind == "answer"`).

---

## Step-by-step implementation plan

1. **`src/task.ts`** — extend the `Task` interface + `normalizeTask`:
   - `hitl?: boolean` (default `true`).
   - `resume?: ResumePayload | null` where
     `ResumePayload = {kind:"confirm"|"answer"|"question"; tool_call_id:string;
     confirmed?:boolean|null; answer?:string|null; agent_state?:unknown}`.

2. **`src/redis_state.ts`** — add to `RedisStateClient`:
   - `writePaused(blob)` → `transport.set(":paused", json, PAUSED_TTL_SECONDS=7d)`.
   - `markPaused()` → read `:state`, set `status:"PAUSED"`, re-set with 7d TTL
     (mirror `_write_terminal` but long TTL).
   - `pushPendingConfirmation(id,name,args)` and `pushPendingQuestion(id,question)`
     → `xadd` with the field shapes above (do NOT push `type=end`).
   - Add `PAUSED_TTL_SECONDS = 7*24*3600`.

3. **New `src/hitl.ts`** — the pause/resume brains:
   - `requiresConfirmation(toolName, task): boolean` — policy (see "which tools"
     below). Default: native tools allow; `mcp__*` tools pause; the synthetic
     `ask_user` tool → question-pause.
   - `makeCanUseTool(ctx)` returning a `CanUseTool`:
     - On a tool that needs confirm/question: capture `{toolUseID, toolName, input}`
       into `ctx.pending`, set `ctx.paused = ctx.kind = ...`, return
       `{behavior:'deny', message:'paused for human approval', interrupt:true}`.
     - On the approved-tool-use registry hit (resume-approved id), return
       `{behavior:'allow', updatedInput: input}`.
     - Otherwise `{behavior:'allow', updatedInput: input}`.
   - `buildReplayPrompt(task): AsyncIterable<SDKUserMessage>` — for resume legs,
     yields the saved transcript turns + the decision turn (approved/rejected/
     answer). Stored transcript lives in `task.resume.agent_state` (our own
     shape, e.g. `{transcript: SDKUserMessage[]-ish, original_task: string,
     pending: {...}}`).
   - `snapshotTranscriptForPause(...)` — assemble the replay transcript at pause
     time from what we streamed (assistant text so far + completed
     tool_call/tool_result pairs + the pending tool_call) and put it in the blob.

4. **`src/worker.ts`** — `runQuery` changes:
   - Compute `hitl = task.hitl ?? true`. If `hitl`, set
     `permissionMode:'default'`, attach `canUseTool` from `makeCanUseTool`, and
     register the `ask_user` SDK tool (via `createSdkMcpServer`/`tool` from the
     SDK, or document a reserved tool name). If `!hitl`, keep today's bypass.
   - Build the prompt: fresh run → today's string prompt; resume leg
     (`task.resume`) → `buildReplayPrompt(task)` (an `AsyncIterable<SDKUserMessage>`).
   - Accumulate the streamed transcript (assistant text + tool_call args +
     tool_result text, keyed by tool_use_id) so a pause can snapshot it. The
     translator already tracks tool names (`event_translator.ts:97,172-173`) and
     the run loop already coalesces tool args/results (worker.ts:474-475,
     560-589) — reuse those maps.
   - After the SDK loop, if `ctx.paused`: call `pushPending*`, `writePaused`,
     `markPaused`, return a new `RunResult` variant `{status:"paused", ...}`.
   - Widen `RunResult.status` to `"OK" | "FAILED" | "paused"`.

5. **`src/main.ts`** — handle the paused result:
   - After `runQuery`, if `result.status === "paused"`: `writeResult(...,
     status:"paused")`, do NOT `markFailed()`/`markCompleted()` (the worker
     already wrote PAUSED state), `return 0`.
   - Keep OK→markCompleted/exit0, FAILED→markFailed/exit1.

6. **ask_user tool registration** — use the SDK in-process server:
   `createSdkMcpServer({name:'hitl', tools:[ tool('ask_user', '...ask the user...',
   {question: z.string()}, async()=>...) ]})` added to `mcpServers`. Its handler
   is never actually run because `canUseTool` pauses before execution (kind=
   "question"). The system prompt must instruct the model to call `ask_user`
   for clarifications (port the old worker's prompt block, `__main__.py:1268-1277`).

7. **System prompt (HITL mode)** — when `hitl`, append the ask_user instruction
   block so the model uses the tool instead of asking in plain text (parity with
   `__main__.py:1268-1277`). Keep it out of the static prefix only if cache
   matters; simplest is to append it (HITL runs are a distinct cache class).

8. **Approved-tool registry on resume** — when `task.resume.kind==="confirm"` &&
   `confirmed===true`, seed `ctx.approvedToolUseIds`/an approved-tool-name+args
   matcher so the re-emitted tool call passes `canUseTool` (allow once), then the
   run proceeds. Rejected → inject the denial guidance turn and do NOT allow.

No changes to: scheduler, orchestrator, tool-broker, BFF, frontend. The Redis
keys, blob shape, token-stream entry types, result.json `status:"paused"`, and
the `task.json["resume"]` contract are all already implemented on those sides for
the agentscope worker and are honored byte-for-byte here.

---

## Which MCP tools require confirmation (how the gate decides)

The old worker did **not** read a per-tool flag or a tool-broker policy. It used a
**name-based allowlist by construction**:

- Native sandbox tools are explicitly ALLOW-ruled: `_NATIVE_TOOL_NAMES =
  ("Read","Grep","Glob","Write","Edit","Bash")` (`hooks`/`__main__.py:829`), added
  as `PermissionBehavior.ALLOW` rules (`__main__.py:860-868`).
- Everything else falls through to the DEFAULT `ASK` path. Since tenant MCP tools
  are named `mcp__{server}__{tool}` (confirmed in the pending_confirmation doc,
  `redis_state.py:556` example `mcp__github__search_code`), they never match an
  allow rule and therefore pause. (`__main__.py:832-876`.)
- `ask_user` is a special external tool that pauses as a *question*, not a
  confirm (`is_external_tool=True` → `RequireExternalExecutionEvent`,
  `__main__.py:897-961`).
- The whole gate is switched off when `task.hitl=false` → BYPASS, all
  auto-approved (`__main__.py:1143, 879-894`).

**Replicate in the new worker** in `requiresConfirmation()`:
- allow built-in tools by name (`Read|Grep|Glob|Write|Edit|Bash` plus any other
  SDK built-ins the run enables);
- treat any tool whose name starts with `mcp__` as confirm-pausing;
- treat the `ask_user` tool name as question-pausing;
- if `task.hitl === false`, bypass (return false for everything) and use
  `permissionMode:'bypassPermissions'` as today.

(Optional future hardening: have the gate consult the tool-broker / a per-tool
policy. Not required for parity — the old behavior is purely name-based.)

---

## Risks / open questions

1. **History-replay fidelity (top risk).** The old worker restored AgentScope's
   exact `AgentState`. We approximate with a replayed transcript. Edge cases:
   multiple tool calls in one assistant turn where only one needs confirm;
   interleaved thinking blocks; very long transcripts hitting context limits.
   Mitigation: snapshot the *actual* SDK messages we observed
   (`SDKAssistantMessage`/tool blocks) rather than re-deriving, and store them in
   the blob; on replay, feed them as `SDKUserMessage`/assistant turns.
2. **Re-emission non-determinism on approve.** After a confirm-approve, the model
   must re-issue the same tool call for it to run. A model could phrase it
   differently. Mitigation: on approve, the replay's final turn explicitly
   states the approved tool + args and asks the model to execute it now; the
   approved-tool registry matches by name (and optionally args) for a single
   allow. If the model deviates, it simply pauses again (safe).
3. **`canUseTool` + in-process gateway adapter.** The worker points the SDK at a
   localhost gateway adapter (main.ts:232-234). `canUseTool` runs in the SDK
   client process, independent of the model endpoint, so the adapter does not
   interfere — but verify the deny+interrupt path actually terminates the
   `query()` async-iterator (it should yield a `result` message with
   `permission_denials` then end).
4. **`interrupt:true` semantics.** Need to confirm empirically that
   `{behavior:'deny', interrupt:true}` ends the whole `query()` (not just that
   one tool) and surfaces cleanly (no thrown `AbortError` we fail to catch). If
   it throws, catch it and treat as the pause path, not a failure.
5. **One assistant turn, multiple MCP calls.** Old worker pushed one
   `pending_confirmation` per pending call and stored all in `tool_calls[]`.
   `canUseTool` fires per tool; we must decide whether to pause on the first MCP
   call (simplest, matches "deny+interrupt") or collect siblings. Recommend
   pause-on-first; the others re-appear on resume.
6. **ask_user via SDK MCP tool vs reserved name.** If we register a real SDK MCP
   tool whose handler never runs (we pause first), confirm the SDK doesn't error
   on a never-resolving handler. Alternative: a plain disallowed/known tool name
   the gate recognizes. Needs a quick spike.
7. **`reply_id`.** Old blob carries `reply_id`; the SDK has `session_id`/`uuid`
   per message but no AgentScope `reply_id`. We can store the SDK `session_id`
   (or empty string) — orchestrator only echoes it back, doesn't require a
   specific value.
8. **HITL + DeepSeek prefix cache.** Appending the ask_user block changes the
   system prefix for HITL runs (cache note at worker.ts:203-213). Acceptable;
   HITL runs are a separate cache class.

---

## Test plan

### Unit (vitest, mock `query` via `RunQueryDeps.queryFn`)
- `requiresConfirmation`: native names → false; `mcp__x__y` → confirm;
  `ask_user` → question; `hitl=false` → all false.
- `canUseTool`: returns `{behavior:'deny', interrupt:true}` and records pending
  `{toolUseID,name,input}` for an `mcp__` tool; `{behavior:'allow'}` for `Read`;
  allow-once for an id in the approved registry.
- `runQuery` pause path: a mocked `query` that yields an assistant turn calling
  `mcp__github__search` → assert (a) `writePaused` called with correct blob
  shape (kind/tool_calls/agent_state/paused_at), (b) `markPaused` called,
  (c) `pushPendingConfirmation` pushed, (d) NO `pushTokenEnd`, (e) RunResult
  `status:"paused"`.
- ask_user pause path: mocked `query` calls `ask_user(question)` → blob
  `kind:"question"`, `pushPendingQuestion`, `question` field set.
- `buildReplayPrompt`:
  - confirm-approve → final turn instructs execution; approved registry seeded.
  - confirm-reject → denial/guidance turn injected; registry NOT seeded.
  - answer → ask_user answer delivered as the tool's result turn.
  - accepts resume `kind` both `"answer"` and `"question"`.
- `main` paused branch: result.status "paused" → result.json `status:"paused"`,
  exit code 0, neither markCompleted nor markFailed called.
- redis_state: `writePaused`/`markPaused` use 7d TTL; pending-* xadd field shapes
  match the documented contract; broker transport routes `:paused` set
  (already in SET_KINDS).

### Integration / e2e (real stack — parity proof)
The proof of parity is: **swap the agentscope worker image for the claude-sdk
worker image and run the existing HITL e2e unchanged.** Specifically (mirrors the
memory notes `hitl-confirm-path-e2e-verified` / `hitl-sse-delivery-two-bugs-fixed`):
1. CONFIRM approve: prompt the agent to call a tenant MCP tool (the echo-mcp test
   server). Assert the run goes PAUSED, a `pending_confirmation` reaches the
   frontend via SSE, `POST /sessions/{id}/runs/{run}/respond {confirmed:true}`
   re-dispatches a fresh sandbox with `task.json["resume"]`, the tool executes,
   and the run COMPLETEs.
2. CONFIRM reject: same but `{confirmed:false}` → model continues without the
   tool, run COMPLETEs, no tool side-effect.
3. QUESTION: prompt that forces `ask_user` → run PAUSED with `pending_question`
   → `respond {answer:"..."}` → run resumes using the answer → COMPLETEs.
4. Re-pause: a resume leg that triggers a *second* MCP call must PAUSE again
   (the pause path is reachable on every leg).
5. Rehydration: refresh/reconnect while PAUSED → `GET /sessions/{id}/pending`
   still returns the pending card (orchestrator reads the Redis paused blob;
   `runs.py:511-569`).
6. `hitl=false` (scheduled run): the same MCP tool auto-runs, never PAUSEs.

Parity is established when all six pass against the claude-sdk worker with **no
changes** to orchestrator/scheduler/BFF/frontend.
