/**
 * worker.ts — the core run loop for the Claude SDK runtime worker.
 *
 * Mirrors the AgentScope runtime_worker's `_run_real` + `_dispatch`:
 *   - download prompt attachments from S3 into /workspace/inputs
 *   - assemble the system prompt (resume note + memory + history + task
 *     sys_prompt + skills index + uploaded-file hint)
 *   - resolve tenant MCP servers from the registry
 *   - run the Claude SDK query(), translating each SDK message into the
 *     agentscope event vocabulary, appending to events.jsonl, pushing hot-state
 *     to Redis, and re-writing result.json (RUNNING) on every tool-call end
 *   - return {status, summary, error?, usage, plan_steps}
 *
 * Every external dependency (S3, Redis, MCP registry) is failure-open: an
 * outage logs a warning and the run continues / degrades gracefully.
 */

import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

import {
  GetObjectCommand,
  S3Client,
  type S3ClientConfig,
} from "@aws-sdk/client-s3";
import {
  query,
  tool,
  createSdkMcpServer,
  AbortError,
  type McpServerConfig,
  type Options,
  type SDKMessage,
  type SDKUserMessage,
  type HookInput,
  type HookJSONOutput,
} from "@anthropic-ai/claude-agent-sdk";
import { z } from "zod";

import { HISTORY_OMITTED, type Env, type Task } from "./task.js";
import type { RedisStateClient } from "./redis_state.js";
import { appendEvent, writeResult, type Usage } from "./result.js";
import { EventTranslator, type EventPayload } from "./event_translator.js";
import { scanToolCall } from "./dlp.js";
import {
  ASK_USER_TOOL,
  buildReplayPrompt,
  makeCanUseTool,
  newHitlContext,
  newTranscriptAccumulator,
  recordTranscriptText,
  recordTranscriptToolCall,
  recordTranscriptToolResult,
  snapshotTranscriptForPause,
  type HitlContext,
  type PendingToolCall,
  type ReplayState,
  type TranscriptAccumulator,
} from "./hitl.js";

/**
 * Workspace root. Defaults to the sandbox mount `/workspace`; overridable via
 * WORKER_WORKSPACE_DIR purely so tests can target a writable tmp dir. Resolved
 * lazily on each access so a test that sets the env after import still sees it;
 * production never sets it, so the contract path is unchanged.
 */
function workspaceDir(): string {
  return process.env.WORKER_WORKSPACE_DIR || "/workspace";
}
function inputsDir(): string {
  return path.join(workspaceDir(), "inputs");
}
function outputsDir(): string {
  return path.join(workspaceDir(), "outputs");
}
function eventsPath(): string {
  return path.join(workspaceDir(), "events.jsonl");
}
const SKILLS_DIR = "/home/skills";

/**
 * Anthropic server-side ("hosted") tools whose execution runs on Anthropic's
 * online infrastructure rather than in the sandbox. This worker talks to an
 * OpenAI-compatible gateway (deepseek), where those tools cannot be executed —
 * `WebFetch` errors outright and `WebSearch` gets hallucinated into fake search
 * results. They are part of the SDK's default tool set, so we explicitly remove
 * them via `disallowedTools`. Client-side tools (Bash/Read/Write/Edit/Glob/Grep/
 * NotebookEdit/Task/TodoWrite/KillShell, the `mcp__*` tools incl. `mcp__hitl__*`,
 * and memory) all run locally and are unaffected.
 *
 * Names are the SDK's canonical tool identifiers (see
 * node_modules/@anthropic-ai/claude-agent-sdk/cli.js and the `WebSearchInput`/
 * `WebFetchInput` entries in sdk-tools.d.ts). This SDK version ships no hosted
 * code-execution tool, so there is nothing else server-side to disable here.
 */
const SERVER_SIDE_TOOLS_UNAVAILABLE_ON_GATEWAY = ["WebSearch", "WebFetch"];

/**
 * Nominal thinking budget used purely to make the Claude Agent SDK EMIT the
 * Anthropic `thinking:{type:"enabled"}` field on the wire (the SDK emits it iff
 * maxThinkingTokens > 0). The actual reasoning depth on the DeepSeek backend is
 * governed by `reasoning_effort`, not by this token budget, so the precise value
 * is irrelevant beyond being positive.
 */
const THINKING_ENABLE_BUDGET = 2048;

/**
 * Map a task-supplied reasoning effort onto a value DeepSeek accepts. DeepSeek
 * only honours `high` / `max`; Anthropic's lower tiers (`low` / `medium`) are
 * floored to `high` (DeepSeek has no lower setting). An unset / unrecognised
 * value yields undefined (no reasoning_effort sent, thinking left off).
 */
export function mapReasoningEffort(effort: string | undefined): string | undefined {
  if (!effort) return undefined;
  const e = effort.trim().toLowerCase();
  switch (e) {
    case "max":
      return "max";
    case "high":
    case "medium":
    case "low":
      // DeepSeek has no tier below `high`, so low/medium are floored to high.
      return "high";
    default:
      return undefined;
  }
}

/**
 * Extract the raw JSON schema for structured outputs from task sampling, or
 * undefined when none is configured. Accepts the OpenAI-style
 * `sampling_params.response_format = {type:"json_schema", schema:{...}}`. The
 * schema is returned verbatim (never rewritten); the adapter wraps it into the
 * gateway's `response_format`. A response_format that carries no `schema` (e.g.
 * a bare `{type:"json_object"}`) yields undefined — we only drive constrained
 * decoding when an actual schema is present.
 */
export function extractStructuredOutputSchema(
  sampling: { response_format?: { type?: string; schema?: unknown } } | undefined,
): unknown {
  const rf = sampling?.response_format;
  if (rf == null || typeof rf !== "object") return undefined;
  if (rf.schema == null) return undefined;
  return rf.schema;
}

/**
 * Result of a completed run, surfaced to main() for result.json + exit code.
 * "cancelled" = the run was aborted from outside (SIGTERM / abortController)
 * before it finished; main() records it as FAILED in result.json.
 */
export interface RunResult {
  status: "OK" | "FAILED" | "paused" | "cancelled";
  summary: string;
  error?: string;
  usage: Usage;
  plan_steps: never[];
}

/**
 * The short, user-safe text pushed as the token stream's `error` entry when a
 * run fails. Deliberately generic: never the raw exception / upstream body
 * (stack traces, paths, hostnames, keys). `apiError` is the SDK's assistant
 * error code; `resultSubtype` the SDK result subtype.
 */
export function userSafeErrorMessage(info: {
  apiError?: string;
  resultSubtype?: string;
}): string {
  switch (info.apiError) {
    case "rate_limit":
      return "The model service is rate-limiting requests. Please try again in a moment.";
    case "authentication_failed":
    case "billing_error":
      return "The model service rejected the request. Please contact your administrator.";
    case "invalid_request":
      return "The model service rejected the request.";
    case "server_error":
      return "The model service is temporarily unavailable. Please try again.";
    default:
      break;
  }
  switch (info.resultSubtype) {
    case "error_max_turns":
      return "The agent stopped after reaching its maximum number of steps.";
    case "error_max_budget_usd":
      return "The agent stopped after reaching its budget limit.";
    case "error_max_structured_output_retries":
      return "The agent could not produce output in the required format.";
    default:
      return "The agent run failed unexpectedly. Please try again.";
  }
}

// ── S3 attachment download ─────────────────────────────────────────────────

/** Build an S3 client from env, or null when storage is not configured. */
function buildS3Client(env: Env): S3Client | null {
  // failure-open: with no bucket configured there is nothing to download.
  if (!env.S3_BUCKET) return null;
  const cfg: S3ClientConfig = {
    region: env.S3_REGION || "us-east-1",
    // MinIO / non-AWS endpoints need path-style addressing.
    forcePathStyle: true,
  };
  if (env.S3_ENDPOINT) cfg.endpoint = env.S3_ENDPOINT;
  if (env.S3_ACCESS_KEY_ID && env.S3_SECRET_ACCESS_KEY) {
    cfg.credentials = {
      accessKeyId: env.S3_ACCESS_KEY_ID,
      secretAccessKey: env.S3_SECRET_ACCESS_KEY,
    };
  }
  try {
    return new S3Client(cfg);
  } catch (err) {
    console.warn(
      `[worker] S3 client init failed; attachments disabled: ${(err as Error).message}`,
    );
    return null;
  }
}

async function streamToBuffer(body: unknown): Promise<Buffer> {
  // The aws-sdk v3 Body is a Node Readable in this runtime; collect its chunks.
  const chunks: Buffer[] = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- Node stream
  for await (const chunk of body as AsyncIterable<any>) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks);
}

/**
 * Download task.files[] into /workspace/inputs/<filename>. Returns the list of
 * filenames successfully downloaded. Failure-open: a failed file logs a warning
 * and is skipped; an unconfigured S3 returns [].
 */
export async function downloadAttachments(
  task: Task,
  env: Env,
): Promise<string[]> {
  const files = task.files ?? [];
  if (files.length === 0) return [];

  const s3 = buildS3Client(env);
  if (s3 === null) {
    console.warn("[worker] S3 not configured; attachments will not be downloaded");
    return [];
  }

  const inputs = inputsDir();
  try {
    await mkdir(inputs, { recursive: true });
  } catch (err) {
    console.warn(`[worker] cannot create ${inputs}: ${(err as Error).message}`);
    return [];
  }

  const downloaded: string[] = [];
  for (const spec of files) {
    const key = spec.key ?? "";
    const filename = spec.filename || key.split("/").pop() || "file";
    const dest = path.join(inputs, filename);
    try {
      const resp = await s3.send(
        new GetObjectCommand({ Bucket: env.S3_BUCKET, Key: key }),
      );
      const buf = await streamToBuffer(resp.Body);
      await writeFile(dest, buf);
      downloaded.push(filename);
      console.log(`[worker] downloaded attachment ${key} -> ${dest}`);
    } catch (err) {
      console.warn(
        `[worker] failed to download attachment ${key}: ${(err as Error).message}`,
      );
    }
  }
  return downloaded;
}

// ── system prompt assembly ──────────────────────────────────────────────────

/** Rough token estimate: 1 char ≈ 0.4 token (matches B.2's budget heuristic). */
function estimateTokens(text: string): number {
  return Math.ceil(text.length * 0.4);
}

/**
 * Serialize task.history[] into plain text, keeping only the most recent
 * ~8000 tokens (B.2). Older turns are dropped from the front (a tool call
 * together with its result), and a leading note says how many earlier turns
 * are not shown — dropped here, or already left out by the orchestrator (its
 * `history_omitted` item) — so the model knows the conversation is incomplete.
 */
export function historyToText(task: Task): string {
  const history = task.history ?? [];
  const turns: string[] = [];
  let omitted = 0;
  for (const item of history) {
    if (item.type === HISTORY_OMITTED) {
      const n = item.omitted;
      if (typeof n === "number" && Number.isInteger(n) && n > 0) omitted += n;
    } else if (item.role === "user") {
      turns.push(`[user]: ${item.content ?? ""}`);
    } else if (item.role === "assistant") {
      turns.push(`[assistant]: ${item.content ?? ""}`);
    } else if (item.role === "tool" && item.tool) {
      const name = item.tool.name ?? "tool";
      const input =
        typeof item.tool.input === "string"
          ? item.tool.input
          : JSON.stringify(item.tool.input ?? {});
      const result =
        typeof item.tool.result === "string"
          ? item.tool.result
          : JSON.stringify(item.tool.result ?? "");
      turns.push(`[tool_call:${name}]: ${input}\n[tool_result:${name}]: ${result}`);
    }
  }

  // Keep the most recent turns under the ~8000-token budget.
  const MAX_TOKENS = 8000;
  const kept: string[] = [];
  let tokens = 0;
  for (let i = turns.length - 1; i >= 0; i--) {
    const t = estimateTokens(turns[i]);
    if (tokens + t > MAX_TOKENS && kept.length > 0) break;
    kept.push(turns[i]);
    tokens += t;
  }
  kept.reverse();
  const notShown = omitted + turns.length - kept.length;
  if (notShown > 0) {
    kept.unshift(`[${notShown} earlier message(s) of this conversation are not shown]`);
  }
  return kept.join("\n");
}

/**
 * Build the full system prompt. Order:
 *   memory → history → task.sys_prompt → skills index → file hint.
 *
 * Cache-friendliness: the system prompt is intentionally kept PURELY STATIC for
 * a given task (no run-dependent values like the resume step count). DeepSeek's
 * automatic prefix cache matches messages token-by-token from the front, so any
 * per-run drift at the head of the prompt would defeat it. The resume note —
 * which differs every run — is therefore injected into the user task prompt
 * instead (see runQuery), not here. This way two consecutive resume runs share
 * an identical system prefix and stay cache-hittable.
 */
export function buildSystemPrompt(
  task: Task,
  _env: Env,
  downloadedFiles: string[],
): string {
  const parts: string[] = [];

  const memory = (task.memory ?? "").trim();
  if (memory) parts.push(`## Relevant memory\n\n${memory}`);

  const historyText = historyToText(task);
  if (historyText) parts.push(`## Conversation history\n\n${historyText}`);

  const sysPrompt = (task.sys_prompt ?? "").trim();
  if (sysPrompt) parts.push(sysPrompt);

  // Skills index (B.3): advertise name + SKILL.md path; the model Reads on demand.
  const skills = task.skills ?? [];
  if (skills.length > 0) {
    const lines = ["## Available skills (use the Read tool to load a skill's instructions when needed)"];
    for (const name of skills) {
      // description left empty in v1 (B.3); only the path is advertised.
      lines.push(`- ${name}: see ${SKILLS_DIR}/${name}/SKILL.md`);
    }
    parts.push(lines.join("\n"));
  }

  if (downloadedFiles.length > 0) {
    const inputs = inputsDir();
    parts.push(
      `The user uploaded the following files to ${inputs}/: ${downloadedFiles.join(", ")}\n` +
        `Use the Read tool to read the files under ${inputs}/ to complete the task.`,
    );
  }

  // Only the workspace dir is mounted back to the scheduler host; files written
  // anywhere else (the image's /home/user, /tmp, …) die with the sandbox and
  // never become run artifacts, so the user never sees them. Static text — the
  // path is a constant, so this stays prefix-cache friendly.
  parts.push(
    `## Output files\n\nAny files you generate must be written under ${workspaceDir()}/` +
      ` (for example ${workspaceDir()}/chart.png). Files written anywhere else are not saved and the user will never see them.`,
  );

  return parts.join("\n\n");
}

// ── MCP registry ────────────────────────────────────────────────────────────

/**
 * Resolve the tenant's MCP servers from the registry. failure-open: when
 * MCP_REGISTRY_URL is unset or the call fails, returns empty servers + filters
 * and logs a warning.
 *
 * B.5/F.5: the registry's server records are mapped onto the SDK's
 * Record<string, McpServerConfig>. The registry is expected to return
 * `{servers: [{name, type, url|command, headers?, args?, env?, auth?,
 * timeout_seconds?, tool_filter?}]}`. If the real registry shape diverges,
 * extend `mapRegistryServer` below.
 *
 * Returns the SDK server map PLUS a per-server `filters` map (from the
 * registry `tool_filter` field) so runQuery can hide blocked tools via
 * `disallowedTools` (exposure) and enforce per-server allow-lists via a
 * PreToolUse gate (the SDK has no per-server allowedTools, so allow is enforced
 * at execution; block is a true exposure filter).
 *
 * `selectedIds` is the run-level selection from `task.json.mcp_server_ids` and
 * is THREE-STATE (see `Task.mcp_server_ids`): `undefined` = tenant default (all
 * enabled servers), `[]` = MCP off for this run, non-empty = only those ids.
 */
export async function loadMcpServers(
  env: Env,
  selectedIds?: readonly string[],
): Promise<{ servers: Record<string, McpServerConfig>; filters: McpToolFilters }> {
  // Explicit empty selection = MCP is off for this run. Short-circuit before the
  // registry call: there is no server we could possibly keep, so the fetch would
  // be pure waste. Note `selectedIds !== undefined` — `[]` is falsy and a
  // truthiness test here would silently mean "tenant default" instead.
  if (selectedIds !== undefined && selectedIds.length === 0) {
    console.log("[worker] MCP disabled for this run (mcp_server_ids=[])");
    return { servers: {}, filters: {} };
  }

  const base = env.MCP_REGISTRY_URL;
  if (!base) {
    console.warn("[worker] MCP_REGISTRY_URL unset; no MCP servers loaded");
    return { servers: {}, filters: {} };
  }

  // The registry's internal-auth guard requires the tenant_id query param to
  // match the x-tenant-id header (and the tool-broker forwards the query string
  // verbatim). Append it whenever we have a tenant; skip it rather than emit
  // `tenant_id=undefined` when the env var is empty.
  const tenantId = env.FOUNDATION_TENANT_ID;
  const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : "";
  const url = `${base.replace(/\/+$/, "")}/internal/mcp-servers${query}`;
  try {
    const resp = await fetch(url, {
      method: "GET",
      headers: {
        authorization: `Bearer ${env.FOUNDATION_API_TOKEN}`,
        "x-tenant-id": env.FOUNDATION_TENANT_ID,
        "x-user-id": env.FOUNDATION_USER_ID,
        "x-service-scopes": env.FOUNDATION_SERVICE_SCOPES,
        ...(env.TOOL_BROKER_LEASE
          ? { "x-tool-broker-lease": env.TOOL_BROKER_LEASE }
          : {}),
      },
    });
    if (!resp.ok) {
      console.warn(
        `[worker] mcp-registry returned ${resp.status}; no MCP servers loaded`,
      );
      return { servers: {}, filters: {} };
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
    const body: any = await resp.json();
    // The mcp-registry /internal/mcp-servers endpoint returns a ListPage
    // envelope `{items: [...], total}`. Older/alternate shapes used a bare
    // array or `{servers: [...]}`; accept all three.
    const servers: any[] = Array.isArray(body)
      ? body
      : Array.isArray(body?.items)
        ? body.items
        : Array.isArray(body?.servers)
          ? body.servers
          : [];

    // Run-level selection is applied HERE — before mapRegistryServer, i.e.
    // BEFORE the transport mapping / broker rewrite / drop logic. The two
    // filters answer different questions and must not be interleaved:
    //   selection  = "did the user pick this server for this run?" (identity)
    //   transport  = "can this runtime actually speak to it?"      (capability)
    // Selecting first means an unpicked server is never mapped, never rewritten
    // to a broker endpoint, and never contacted — a deselected server must not
    // be touched at all. It also keeps the "dropping MCP server X" warnings
    // meaningful: they now only fire for servers the user actually asked for.
    const selected =
      selectedIds === undefined
        ? servers
        : servers.filter((raw) => selectedIds.includes(String(raw?.id ?? "").trim()));
    if (selectedIds !== undefined) {
      console.log(
        `[worker] mcp_server_ids selection: ${selected.length}/${servers.length} server(s) kept`,
      );
    }

    const out: Record<string, McpServerConfig> = {};
    const filters: McpToolFilters = {};
    for (const raw of selected) {
      const mapped = await mapRegistryServer(raw, env);
      if (mapped) {
        out[mapped.name] = mapped.config;
        if (mapped.filter) filters[mapped.name] = mapped.filter;
      }
    }
    console.log(`[worker] loaded ${Object.keys(out).length} MCP server(s) from registry`);
    return { servers: out, filters };
  } catch (err) {
    console.warn(
      `[worker] mcp-registry fetch failed; no MCP servers loaded: ${(err as Error).message}`,
    );
    return { servers: {}, filters: {} };
  }
}

/**
 * Map one registry server record onto an SDK McpServerConfig entry. Returns null
 * for records we cannot represent. Only the serializable transport configs
 * (http/sse/stdio) are produced here (sdk-instance servers are not registry-backed).
 *
 * Broker egress mode (`env.SANDBOX_EGRESS_MODE === "broker"`): the sandbox is NOT
 * on the foundation network, so the raw http/sse endpoint is unreachable. Each
 * http/sse server is rewritten to the tool-broker per-server passthrough
 * `{TOOL_BROKER_URL}/mcp-tenant/v1/{server_id}` and the per-run lease is attached
 * as the `x-tool-broker-lease` header (the SDK re-sends configured headers on
 * every MCP request). Failure-open: if the broker URL or the registry `id` is
 * missing, the server is DROPPED rather than handed an unreachable raw endpoint.
 * stdio servers run locally in the sandbox and are never rewritten or dropped.
 */
async function mapRegistryServer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  raw: any,
  env: Env,
): Promise<{ name: string; config: McpServerConfig; filter?: { allow?: string[]; block?: string[] } } | null> {
  const name = String(raw?.name ?? "").trim();
  if (!name) return null;
  // The mcp-registry returns `id` (e.g. "mcp_<26HEX>") on every record; needed to
  // address the broker passthrough by (tenant, server_id).
  const id = String(raw?.id ?? "").trim();
  // The mcp-registry uses `transport`/`endpoint`; tolerate `type`/`url` too.
  const url = raw?.url ?? raw?.endpoint;
  const type = String(
    raw?.type ?? raw?.transport ?? (url ? "http" : ""),
  ).toLowerCase();
  // tool_filter is transport-agnostic; capture it once for the caller to apply.
  const filter = parseToolFilter(raw?.tool_filter);
  // timeout_seconds applies to every transport (forwarded to the SDK config;
  // the current SDK/CLI ignores an unknown `timeout` field, but strictMcpConfig
  // defaults to false so it passes through harmlessly - see withTimeout).
  const timeoutSeconds = raw?.timeout_seconds;

  if ((type === "http" || type === "sse") && url) {
    if (env.SANDBOX_EGRESS_MODE === "broker") {
      // Broker mode: the raw endpoint is unreachable from the sandbox. Rewrite to
      // the broker passthrough, or drop the server (failure-open) if we cannot.
      const brokerBase = (env.TOOL_BROKER_URL ?? "").replace(/\/+$/, "");
      if (!brokerBase || !id) {
        console.warn(
          `[worker] broker-egress: dropping MCP server ${name} (no broker url / id)`,
        );
        return null;
      }
      // The SDK re-sends these configured headers on EVERY MCP request. The
      // broker's auth runs in two gates: `_require_auth` (needs an
      // Authorization bearer + X-Tenant-Id, exactly like the registry fetch
      // above) THEN `_require_lease` (needs `x-tool-broker-lease` carrying
      // `mcp.tenant_invoke`). Both must be present on every tool call or the
      // broker 401s before the lease is even checked, so we attach all three
      // (mirroring loadMcpServers' registry-fetch header set).
      //
      // Registry-supplied upstream `auth`/`headers` are intentionally NOT
      // attached here: the broker is the single upstream-auth injection point
      // for broker-routed sessions (it reads them from the registry record and
      // injects them toward the real upstream). Sending them here would only
      // hand them to the broker, which already has them, risking duplication.
      return {
        name,
        config: withTimeout(
          {
            type: type as "http" | "sse",
            url: `${brokerBase}/mcp-tenant/v1/${encodeURIComponent(id)}`,
            headers: {
              authorization: `Bearer ${env.FOUNDATION_API_TOKEN}`,
              "x-tenant-id": env.FOUNDATION_TENANT_ID,
              ...(env.TOOL_BROKER_LEASE
                ? { "x-tool-broker-lease": env.TOOL_BROKER_LEASE }
                : {}),
            },
          },
          timeoutSeconds,
        ),
        filter,
      };
    }
    // Non-broker mode (foundation/none/unset): the sandbox reaches the upstream
    // directly, so the worker injects the registry-supplied upstream auth +
    // custom headers itself (mirroring the inline orchestrator). Broker auth
    // headers are NOT attached - there is no broker in this path.
    const directHeaders: Record<string, string> = {
      ...((raw.headers as Record<string, string>) ?? {}),
      ...buildUpstreamAuthHeaders(raw?.auth),
    };
    // oauth: in direct mode the worker performs the client-credentials flow
    // itself (cached + single-flight by server_id) and injects the bearer.
    // Broker mode does NOT fetch a token here - the broker is the single
    // upstream-auth injection point for broker-routed sessions. A fetch failure
    // is fail-open (logged + no Authorization header) so the MCP handshake
    // surfaces the upstream's own 401. The SDK re-sends these configured
    // headers on every MCP request, so the token is baked in at config time;
    // sandbox runs are short-lived, so mid-run expiry is not a concern.
    if (String(raw?.auth?.type ?? "none").toLowerCase() === "oauth") {
      const oauthServerId = String(raw?.id ?? "").trim();
      if (oauthServerId) {
        try {
          directHeaders["Authorization"] = `Bearer ${await getOauthToken(oauthServerId, raw?.auth)}`;
        } catch (err) {
          console.warn(
            `[worker] oauth token fetch failed for ${name}: ${(err as Error).message}`,
          );
        }
      } else {
        console.warn(
          `[worker] oauth auth missing server id for ${name}; skipping token fetch`,
        );
      }
    }
    return {
      name,
      config: withTimeout(
        {
          type: type as "http" | "sse",
          url: String(url),
          ...(Object.keys(directHeaders).length
            ? { headers: directHeaders }
            : {}),
        },
        timeoutSeconds,
      ),
      filter,
    };
  }
  if (type === "stdio" && raw?.command) {
    // stdio: command + args[] + env. args is now a real registry field (it was
    // a phantom read before the registry exposed it). No HTTP auth/headers.
    return {
      name,
      config: withTimeout(
        {
          type: "stdio",
          command: String(raw.command),
          ...(Array.isArray(raw.args) ? { args: raw.args as string[] } : {}),
          ...(raw.env ? { env: raw.env as Record<string, string> } : {}),
        },
        timeoutSeconds,
      ),
      filter,
    };
  }
  console.warn(`[worker] skipping unmappable MCP server record: ${name} (type=${type || "?"})`);
  return null;
}

/**
 * Per-server tool_filter from the registry, keyed by the SDK server name.
 * `allow` non-empty -> only those tools are exposed; `block` non-empty ->
 * those tools are removed. allow is applied first (whitelist) then block.
 */
export type McpToolFilters = Record<string, { allow?: string[]; block?: string[] }>;

/**
 * Build upstream auth headers (bearer / api_key) from a registry `auth` record.
 * Used ONLY in direct (non-broker) HTTP mode: broker-routed sessions have the
 * broker inject upstream auth, so the worker carries only broker-auth headers.
 * `auth` shape: `{type: "none"|"bearer"|"api_key", token?, header_name?, api_key?}`;
 * `header_name` defaults to `X-API-Key` for `api_key`.
 */
function buildUpstreamAuthHeaders(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  auth: any,
): Record<string, string> {
  if (!auth || typeof auth !== "object") return {};
  const out: Record<string, string> = {};
  const type = String(auth.type ?? "none").toLowerCase();
  if (type === "bearer") {
    const token = String(auth.token ?? "").trim();
    if (token) out["Authorization"] = `Bearer ${token}`;
  } else if (type === "api_key") {
    const apiKey = String(auth.api_key ?? "").trim();
    if (apiKey) {
      const headerName = String(auth.header_name ?? "").trim() || "X-API-Key";
      out[headerName] = apiKey;
    }
  }
  return out;
}

// ── OAuth client-credentials token cache (direct-mode MCP servers) ──────────
//
// Direct (non-broker) mode: the sandbox reaches the upstream directly, so the
// worker injects registry-supplied upstream auth itself. For `auth.type ===
// "oauth"` it performs the client-credentials flow (POST to the registry-
// supplied `token_url`), caches the access_token keyed by server_id, and
// injects `Authorization: Bearer {access_token}` into the SDK config headers.
// Broker-routed sessions do NOT fetch tokens here (the broker does it).
//
// Cache: module-level Map<server_id, {access_token, expires_at}>. A token is
// refreshed when within OAUTH_REFRESH_MARGIN_MS of expiry. Single-flight: a
// per-server_id in-flight Promise ensures concurrent calls share one fetch
// (the check-and-set is synchronous, so a concurrent caller always sees the
// stored Promise).
const _oauthTokenCache = new Map<string, { access_token: string; expires_at: number }>();
const _oauthInflight = new Map<string, Promise<string>>();
const OAUTH_REFRESH_MARGIN_MS = 60_000;
const OAUTH_TOKEN_TIMEOUT_MS = 10_000;

/**
 * POST `client_credentials` to `auth.token_url` -> {access_token, expires_at}.
 * `expires_at` is a ms-epoch timestamp. `expires_in` defaults to 3600s when the
 * token endpoint omits it or returns a non-numeric value. Throws on any
 * transport / HTTP / shape error so the caller can fail-open.
 */
export async function fetchOauthToken(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  auth: any,
): Promise<{ access_token: string; expires_at: number }> {
  const tokenUrl = String(auth?.token_url ?? "").trim();
  const clientId = String(auth?.client_id ?? "").trim();
  const clientSecret = String(auth?.client_secret ?? "").trim();
  if (!tokenUrl || !clientId || !clientSecret) {
    throw new Error("oauth auth requires token_url, client_id, client_secret");
  }
  const scopesRaw = Array.isArray(auth?.scopes) ? auth.scopes : [];
  const scopes = scopesRaw.map((s: unknown) => String(s)).filter(Boolean);
  const form = new URLSearchParams({
    grant_type: "client_credentials",
    client_id: clientId,
    client_secret: clientSecret,
    ...(scopes.length ? { scope: scopes.join(" ") } : {}),
  });
  const resp = await fetch(tokenUrl, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: form.toString(),
    signal: AbortSignal.timeout(OAUTH_TOKEN_TIMEOUT_MS),
  });
  if (!resp.ok) {
    throw new Error(`oauth token endpoint returned HTTP ${resp.status}`);
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- token JSON
  const data: any = await resp.json();
  const accessToken = String(data?.access_token ?? "").trim();
  if (!accessToken) {
    throw new Error("oauth token endpoint returned no access_token");
  }
  const expiresIn = typeof data?.expires_in === "number" ? data.expires_in : 3600;
  return { access_token: accessToken, expires_at: Date.now() + expiresIn * 1000 };
}

/**
 * Return a cached oauth access_token for `serverId`, refreshing on expiry.
 * Single-flight: concurrent callers for the same `serverId` share one fetch via
 * a per-`serverId` in-flight Promise (the check-and-set is synchronous, so a
 * concurrent caller always sees the stored Promise).
 */
export async function getOauthToken(
  serverId: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  auth: any,
): Promise<string> {
  const now = Date.now();
  const cached = _oauthTokenCache.get(serverId);
  if (cached && cached.expires_at - now > OAUTH_REFRESH_MARGIN_MS) {
    return cached.access_token;
  }
  let inflight = _oauthInflight.get(serverId);
  if (!inflight) {
    inflight = (async () => {
      const { access_token, expires_at } = await fetchOauthToken(auth);
      _oauthTokenCache.set(serverId, { access_token, expires_at });
      return access_token;
    })().finally(() => {
      _oauthInflight.delete(serverId);
    });
    _oauthInflight.set(serverId, inflight);
  }
  return inflight;
}

/**
 * Test-only: clear the oauth token cache + in-flight promises so tests start
 * from a clean slate. Not part of the runtime contract.
 */
export function _resetOauthTokenCacheForTest(): void {
  _oauthTokenCache.clear();
  _oauthInflight.clear();
}

/**
 * Attach `timeout_seconds` to an SDK MCP config as a `timeout` field. The
 * installed SDK (0.1.77) McpServerConfig type has no `timeout` and the CLI's
 * MCP transport does not consume one, so this is forward-compatible only:
 * `strictMcpConfig` defaults to false so the extra field passes through to the
 * CLI unchanged (never rejected). A future SDK that honours a per-server
 * `timeout` will pick it up with no further change here. Only positive numbers
 * are attached so absent/invalid values leave the config untouched.
 */
function withTimeout(
  config: McpServerConfig,
  timeoutSeconds: unknown,
): McpServerConfig {
  if (typeof timeoutSeconds === "number" && timeoutSeconds > 0) {
    return { ...config, timeout: timeoutSeconds } as unknown as McpServerConfig;
  }
  return config;
}

/**
 * Reduce the per-server `timeout` (seconds, attached by {@link withTimeout})
 * across the MCP server map to a single minimum value. Returns `undefined`
 * when no server carries a positive `timeout` - the caller then leaves the
 * SDK `options.env` untouched so the CLI defaults apply (30s connect, no
 * tool-call cap).
 *
 * MIN-aggregation rationale: the SDK spawns ONE CLI subprocess per run that
 * hosts every MCP server together, so the CLI's process-global `MCP_TIMEOUT`
 * (connect/init) and `MCP_TOOL_TIMEOUT` (per tool-call) cannot be set
 * per-server. Taking the MIN ensures the most conservative (fastest-timeout)
 * server's bound is the one enforced across the whole run - a MAX or average
 * would let a tight server hang past its configured bound. This is a
 * deliberate downgrade from true per-server semantics: a future SDK that
 * honours the per-server `timeout` field (already attached by withTimeout,
 * forward-compatible) would let each value apply independently and this
 * global aggregation could be dropped. In-process SDK servers (hitl /
 * webfetch) never carry a `timeout` and are naturally skipped.
 */
function minMcpTimeoutSeconds(
  servers: Record<string, McpServerConfig>,
): number | undefined {
  let min: number | undefined;
  for (const cfg of Object.values(servers)) {
    // withTimeout attaches `timeout` (seconds) as an extra field; the SDK
    // McpServerConfig union does not declare it, so narrow defensively.
    const t = (cfg as { timeout?: unknown }).timeout;
    if (typeof t === "number" && t > 0 && (min === undefined || t < min)) {
      min = t;
    }
  }
  return min;
}

/**
 * Parse a registry `tool_filter` into the {allow, block} shape. Returns
 * undefined when no non-empty list is present so callers can skip storing it.
 */
function parseToolFilter(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  raw: any,
): { allow?: string[]; block?: string[] } | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const allow = Array.isArray(raw.allow)
    ? raw.allow.map((x: unknown) => String(x)).filter(Boolean)
    : undefined;
  const block = Array.isArray(raw.block)
    ? raw.block.map((x: unknown) => String(x)).filter(Boolean)
    : undefined;
  if (!allow?.length && !block?.length) return undefined;
  const out: { allow?: string[]; block?: string[] } = {};
  if (allow?.length) out.allow = allow;
  if (block?.length) out.block = block;
  return out;
}

// ── resume context ──────────────────────────────────────────────────────────

/**
 * Read the Redis checkpoint; if a *prior* run left real progress behind on this
 * run-id (worker crashed mid-flight and got re-dispatched under the same id),
 * return a resume note (matches the Python L569-589 wording). Otherwise "".
 * Failure-open.
 *
 * A genuine resume requires a checkpoint that (a) exists, (b) is non-terminal
 * (not COMPLETED/FAILED), and (c) reflects real progress past the start marker.
 * The start-checkpoint (`cursor === "start"`) is written by the run loop itself
 * right before dispatch (main.ts) and carries no real progress — it must never
 * be mistaken for a resume, or every brand-new run gets a bogus "N steps already completed"
 * note prepended to its first prompt. This function is also called *before*
 * that write so it only ever sees a prior run's leftover checkpoint, but the
 * cursor guard keeps it correct regardless of call ordering.
 */
export async function buildResumeContext(
  redis: RedisStateClient,
): Promise<string> {
  const checkpoint = await redis.readCheckpoint();
  if (
    checkpoint === null ||
    checkpoint.status === "COMPLETED" ||
    checkpoint.status === "FAILED" ||
    checkpoint.cursor === "start"
  ) {
    return "";
  }
  const step = checkpoint.step ?? 0;
  const plan = checkpoint.plan ?? "";
  return (
    `[System: this run is resuming from a checkpoint; ${step} step(s) were already completed.` +
    (plan ? ` Previous plan: ${plan}` : "") +
    "]"
  );
}

// ── PreToolUse hook (DLP scan) ───────────────────────────────────────────────

/**
 * Build the PreToolUse hook for this run. Each tool call is scanned by
 * Foundation-DLP (see dlp.ts): a non-ALLOW verdict denies the call, everything
 * else allows. The hook is **fail-OPEN at two layers**:
 *   1. dlp.ts fails open on DLP unset / unreachable / timeout / non-2xx / bad JSON.
 *   2. any unexpected throw inside the hook itself is caught here → allow, so a
 *      hook bug can never hard-block the agent.
 *
 * Identity headers mirror the authenticated MCP-registry fetch (x-tenant-id +
 * conditional x-tool-broker-lease) so the scan works through the broker façade.
 */
export function makeDlpPreToolUse(
  env: Env,
): (input: HookInput) => Promise<HookJSONOutput> {
  return async function dlpPreToolUse(
    input: HookInput,
  ): Promise<HookJSONOutput> {
    try {
      // The SDK only registers this under PreToolUse, so the input carries
      // tool_name / tool_input / tool_use_id; narrow defensively all the same.
      const toolName =
        "tool_name" in input ? String(input.tool_name ?? "") : "";
      const toolInput = "tool_input" in input ? input.tool_input : undefined;
      const toolUseId =
        "tool_use_id" in input ? String(input.tool_use_id ?? "") : "";

      const { decision, reason } = await scanToolCall({
        dlpUrl: env.DLP_URL,
        toolName,
        toolInput,
        toolUseId,
        headers: {
          apiToken: env.FOUNDATION_API_TOKEN,
          tenantId: env.FOUNDATION_TENANT_ID,
          ...(env.TOOL_BROKER_LEASE
            ? { brokerLease: env.TOOL_BROKER_LEASE }
            : {}),
        },
      });

      // A PreToolUse hook that returns an explicit `permissionDecision: "allow"`
      // SHORT-CIRCUITS the SDK permission flow and the `canUseTool` callback is
      // never consulted — which would silently disable the HITL gate (every MCP
      // tool / ask_user call would auto-run instead of pausing). So on a clean
      // scan we assert NO permission decision and let `canUseTool` decide. Only
      // a real DLP hit emits a `deny` (which canUseTool can't override anyway).
      if (decision === "deny") {
        return {
          hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: reason,
          },
        };
      }
      return { continue: true };
    } catch (err) {
      // fail-OPEN: a hook bug must never block the agent. Stay silent on the
      // permission decision (same rationale as the clean-scan path above) so a
      // hook error doesn't accidentally bypass the HITL canUseTool gate.
      console.warn(
        `[worker] DLP PreToolUse hook threw (fail-open, allowing): ${(err as Error).message}`,
      );
      return { continue: true };
    }
  };
}

/**
 * Build a PreToolUse hook that enforces per-server MCP `tool_filter` ALLOW lists
 * at execution time. The SDK has no per-server allowedTools, so a registry
 * allow list (only expose these tools from this server) cannot be applied as an
 * exposure filter without enumerating every tool name up front. Instead this
 * hook DENIES any `mcp__<server>__<tool>` call whose `<tool>` is not in that
 * server's allow list. Block lists are handled separately as `disallowedTools`
 * (true exposure), so this hook only needs to reason about allow.
 *
 * Returns `null` when no server has an allow list, so runQuery can skip wiring
 * an extra hook in the common case. Like the DLP hook, it is fail-OPEN: any
 * unexpected throw returns `{continue:true}` so a hook bug can never hard-block
 * the agent, and an allowed/non-MCP tool stays silent (`{continue:true}`) so the
 * HITL canUseTool gate and DLP hook still run (never returns
 * `permissionDecision:"allow"`, which would short-circuit them).
 */
export function makeMcpAllowGate(
  filters: McpToolFilters,
): ((input: HookInput) => Promise<HookJSONOutput>) | null {
  // serverName -> Set(allowed tool names), only for servers with a non-empty allow.
  const allowSets = new Map<string, Set<string>>();
  for (const [serverName, f] of Object.entries(filters)) {
    if (f?.allow?.length) allowSets.set(serverName, new Set(f.allow));
  }
  if (allowSets.size === 0) return null;

  return async function mcpAllowGate(
    input: HookInput,
  ): Promise<HookJSONOutput> {
    try {
      const toolName =
        "tool_name" in input ? String(input.tool_name ?? "") : "";
      // MCP tools are named `mcp__<server>__<tool>` (verified against the CLI).
      // Non-greedy server match: a server name containing `__` would mis-split
      // and fail to find the allow set -> fail-OPEN (no deny), an acceptable
      // edge case since registry server names are simple identifiers.
      const m = toolName.match(/^mcp__(.+?)__(.+)$/);
      if (!m) return { continue: true };
      const [, server, tool] = m;
      const allow = allowSets.get(server);
      if (allow && !allow.has(tool)) {
        return {
          hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "deny",
            permissionDecisionReason: `MCP tool '${tool}' is not in the allow list for server '${server}'`,
          },
        };
      }
      return { continue: true };
    } catch (err) {
      // fail-OPEN: never block the agent on a hook bug.
      console.warn(
        `[worker] MCP allow-gate hook threw (fail-open, allowing): ${(err as Error).message}`,
      );
      return { continue: true };
    }
  };
}

// ── HITL: ask_user tool + system prompt block ────────────────────────────────

/**
 * The HITL system-prompt block (parity with the old worker __main__.py:1268-1277).
 * Without it the model asks clarification questions as plain reply text and keeps
 * going instead of calling ask_user, so the pause/resume machinery never engages.
 */
export const ASK_USER_PROMPT_BLOCK =
  "## Asking the user (ask_user)\n" +
  "When you need the user to clarify the request, choose between options, or provide " +
  "information you cannot proceed without, you must call the ask_user tool and wait for " +
  "the answer. Do not ask in your reply text and then guess and carry on, and do not end " +
  "the task with a question as your final reply. The task resumes automatically once the " +
  "user answers.";

/**
 * Build the in-process ask_user MCP server. The handler is NEVER invoked: the
 * canUseTool gate pauses the run (kind="question") before any tool executes, so
 * the model's ask_user call interrupts before the handler would run. We still
 * register a real handler because the SDK requires one; it returns a benign
 * placeholder in the (unreachable) event it were ever called.
 */
function buildAskUserServer(): McpServerConfig {
  const askUser = tool(
    ASK_USER_TOOL,
    "Ask the user a clarifying question and wait for their answer. Call this when you need the user to provide information, make a choice, or clarify the request.",
    { question: z.string().describe("The question to ask the user") },
    async () => ({
      // Unreachable: canUseTool interrupts before the handler runs.
      content: [{ type: "text", text: "" }],
    }),
  );
  // createSdkMcpServer returns an McpSdkServerConfigWithInstance, which is a
  // member of the McpServerConfig union.
  return createSdkMcpServer({ name: "hitl", tools: [askUser] }) as McpServerConfig;
}

// ── web_fetch: controlled-egress page fetcher ────────────────────────────────

/**
 * The sandbox network is Internal=true isolated: the model cannot `curl` an
 * arbitrary URL and reach the public internet. The ONLY sanctioned egress for
 * web content is the web-fetch gateway (a direct base in foundation mode, or the
 * tool-broker passthrough in broker mode), whose URL the scheduler injects as
 * `WEB_FETCH_GATEWAY_URL`. The AgentScope worker exposed this as a tool; this
 * function ports it to the Claude SDK as an in-process MCP server named
 * `webfetch` with one tool `web_fetch` (the model sees `mcp__webfetch__web_fetch`).
 *
 * This is deliberately NOT named `WebFetch` — that name is the SDK's hosted
 * server-side tool, which we disable via `disallowedTools` (it can't run against
 * the OpenAI-compatible gateway). The MCP-namespaced `mcp__webfetch__web_fetch`
 * is a wholly distinct identifier and does not collide.
 */
const WEB_FETCH_DEFAULT_MAX_BYTES = 1048576;
const WEB_FETCH_TIMEOUT_MS = 30000;

/**
 * Resolve the gateway POST endpoint from the configured base. Mirrors the
 * AgentScope reference (tool.py:200-203): a base already ending in
 * `/api/web-fetch/v1` (the broker-passthrough shape) only needs `/fetch`
 * appended; any other base gets the full `/api/web-fetch/v1/fetch` suffix.
 */
export function webFetchEndpoint(gatewayUrl: string): string {
  if (gatewayUrl.endsWith("/api/web-fetch/v1")) {
    return `${gatewayUrl}/fetch`;
  }
  return `${gatewayUrl}/api/web-fetch/v1/fetch`;
}

/**
 * Perform one gateway fetch and render the agent-readable text. Pure I/O wrapper
 * over the gateway; the error-string vocabulary matches the AgentScope reference
 * (tool.py:217-262) so the model sees identical signals across both workers.
 * Never throws — every failure path returns a `[WebFetch error]`/`[WebFetch
 * blocked]` string.
 */
export async function runWebFetch(
  env: Env,
  url: string,
  maxBytes: number | undefined,
): Promise<string> {
  const gatewayUrl = env.WEB_FETCH_GATEWAY_URL;
  const endpoint = webFetchEndpoint(gatewayUrl);
  const body = JSON.stringify({
    url,
    max_bytes: maxBytes && maxBytes > 0 ? maxBytes : WEB_FETCH_DEFAULT_MAX_BYTES,
  });

  // Identity headers: only attach those with a value (parity with tool.py:205-215).
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (env.FOUNDATION_API_TOKEN) {
    headers["authorization"] = `Bearer ${env.FOUNDATION_API_TOKEN}`;
  }
  if (env.FOUNDATION_TENANT_ID) headers["x-tenant-id"] = env.FOUNDATION_TENANT_ID;
  if (env.FOUNDATION_USER_ID) headers["x-user-id"] = env.FOUNDATION_USER_ID;
  if (env.FOUNDATION_SERVICE_SCOPES) {
    headers["x-service-scopes"] = env.FOUNDATION_SERVICE_SCOPES;
  }
  if (env.TOOL_BROKER_LEASE) headers["x-tool-broker-lease"] = env.TOOL_BROKER_LEASE;

  let resp: Response;
  try {
    resp = await fetch(endpoint, {
      method: "POST",
      headers,
      body,
      signal: AbortSignal.timeout(WEB_FETCH_TIMEOUT_MS),
    });
  } catch (err) {
    const e = err as Error;
    if (e?.name === "TimeoutError" || e?.name === "AbortError") {
      return `[WebFetch error] Request to ${url} timed out (30 s)`;
    }
    return `[WebFetch error] Could not reach gateway at ${gatewayUrl}: ${e?.message ?? String(err)}`;
  }

  // ── gateway error codes (tool.py:226-241) ──
  if (resp.status === 403) {
    let detail: string;
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any -- gateway JSON
      const j: any = await resp.json();
      detail = typeof j?.detail === "string" ? j.detail : JSON.stringify(j).slice(0, 200);
    } catch {
      detail = (await safeText(resp)).slice(0, 200);
    }
    return `[WebFetch blocked] ${url}: ${detail}`;
  }
  if (resp.status === 504) {
    return (
      `[WebFetch error] Gateway timed out while fetching ${url}. ` +
      "The remote server did not respond in time."
    );
  }
  if (resp.status >= 400) {
    return `[WebFetch error] Gateway returned HTTP ${resp.status} for ${url}`;
  }

  // ── success (tool.py:243-262) ──
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- gateway JSON
  let data: any;
  try {
    data = await resp.json();
  } catch (err) {
    return `[WebFetch error] Could not parse gateway response: ${(err as Error).message}`;
  }
  if (data?.error) {
    return `[WebFetch error] ${url}: ${data.error}`;
  }
  const statusCode = data?.status_code ?? "?";
  const contentType = data?.content_type ?? "?";
  const fetched = data?.body ?? "";
  const truncated = data?.truncated === true;
  let result = `[WebFetch] ${url} — HTTP ${statusCode} (${contentType})\n\n${fetched}`;
  if (truncated) result += "\n[... response truncated at limit ...]";
  return result;
}

/** Read a Response body as text without throwing. */
async function safeText(resp: Response): Promise<string> {
  try {
    return await resp.text();
  } catch {
    return "";
  }
}

/**
 * Build the in-process web_fetch MCP server. Unlike ask_user (whose handler is
 * unreachable), this handler REALLY executes: it POSTs to the controlled
 * web-fetch gateway via {@link runWebFetch}. The handler never reaches out to an
 * arbitrary URL itself — the gateway is the sole, controlled egress, preserving
 * the sandbox's Internal=true isolation.
 */
function buildWebFetchServer(env: Env): McpServerConfig {
  const webFetch = tool(
    "web_fetch",
    "Fetch the content of a public HTTP/HTTPS web page, API, or document. This is the " +
      "only way to reach the network from this sandbox: the sandbox network is isolated " +
      "and cannot curl or reach the internet directly, so anything that requires going " +
      "online (page text, public API responses, online docs, etc.) must use this tool. " +
      "Never try to reach the internet with Bash/curl. Returns the page text (may be truncated).",
    {
      url: z
        .string()
        .describe("The full http/https URL to fetch (required)"),
      max_bytes: z
        .number()
        .optional()
        .describe("Maximum response body size in bytes (optional, default 1048576 = 1 MiB)"),
    },
    async (args: { url: string; max_bytes?: number }) => {
      const text = await runWebFetch(env, args.url, args.max_bytes);
      return { content: [{ type: "text", text }] };
    },
  );
  return createSdkMcpServer({ name: "webfetch", tools: [webFetch] }) as McpServerConfig;
}

// ── the run ─────────────────────────────────────────────────────────────────

export interface RunQueryDeps {
  /** Injectable for tests; defaults to the real SDK query(). */
  queryFn?: typeof query;
}

/**
 * Run the Claude SDK query end-to-end. Streams events to events.jsonl + Redis,
 * re-writes result.json (RUNNING) after each tool-call end, and returns the
 * terminal RunResult. Never throws for model/agent failures — those become a
 * FAILED RunResult; only truly unexpected errors propagate to main()'s catch.
 */
export async function runQuery(
  task: Task,
  env: Env,
  opts: {
    systemPrompt: string;
    mcpServers: Record<string, McpServerConfig>;
    /**
     * Per-server tool_filter (allow/block) from the registry, keyed by the SDK
     * server name. block entries become global `disallowedTools` (true exposure
     * filter - the model never sees them); allow lists are enforced by a
     * PreToolUse gate because the SDK has no per-server allowedTools. Optional:
     * tests / headless callers that pass `mcpServers` directly omit it.
     */
    mcpFilters?: McpToolFilters;
    redis: RedisStateClient;
    /**
     * Resume note for a checkpoint-resumed run (see buildResumeContext); "" for
     * a fresh run. Prepended to the user task prompt rather than the system
     * prompt so the (static) system prefix stays cache-hittable across runs.
     */
    resumeNote?: string;
    /**
     * Aborting it (main() does on SIGTERM / SIGINT) stops the SDK query; the
     * run then ends as "cancelled" with `end(status="cancelled")` pushed.
     */
    abortController?: AbortController;
    deps?: RunQueryDeps;
  },
): Promise<RunResult> {
  const queryFn = opts.deps?.queryFn ?? query;
  const translator = new EventTranslator();

  // Ensure workspace I/O dirs + events.jsonl exist before the loop.
  await ensureDirs();

  const model = task.model_override || env.LLM_MODEL || undefined;
  const sampling = task.sampling_params ?? {};

  // HITL gate: interactive runs (hitl !== false) get the canUseTool gate +
  // ask_user tool + the ask_user prompt block; headless runs keep today's bypass.
  const hitl = task.hitl !== false;
  const hitlCtx: HitlContext = newHitlContext(task);

  const evPath = eventsPath();
  const ws = workspaceDir();

  // Build the MCP server map. In HITL mode we add the in-process ask_user tool
  // alongside the tenant servers (its name `mcp__hitl__ask_user` is recognised by
  // the gate as a question-pause).
  const mcpServers: Record<string, McpServerConfig> = { ...opts.mcpServers };

  // Mutable copy of the per-server tool_filter map. In-process SDK tools
  // (hitl / webfetch) are added below and OVERWRITE any same-named tenant
  // server in `mcpServers`; drop their filter entries here so the tenant's
  // tool_filter is never applied to the in-process tools (e.g. a tenant server
  // named "hitl" with a block list would otherwise add `mcp__hitl__ask_user`
  // to disallowedTools and break HITL, or the allow-gate would deny ask_user).
  const mcpFilters: McpToolFilters = { ...(opts.mcpFilters ?? {}) };
  if (hitl) delete mcpFilters.hitl;
  if (env.WEB_FETCH_GATEWAY_URL) delete mcpFilters.webfetch;

  // web_fetch: the sandbox's only sanctioned network egress for page content.
  // Mounted in BOTH headless and HITL modes (the model needs it regardless of a
  // human gate), but only when the scheduler injected a gateway URL — matching
  // the AgentScope behavior of not registering the tool when egress is unset.
  if (env.WEB_FETCH_GATEWAY_URL) {
    mcpServers.webfetch = buildWebFetchServer(env);
  }

  // tool_filter: the SDK has no per-server allowedTools, so we split the filter
  // across the two mechanisms the SDK does expose:
  //   * block -> global `disallowedTools` with `mcp__<server>__<tool>` names.
  //     This is a TRUE EXPOSURE filter: blocked tools are removed from the
  //     model's context and can never be requested (works in both HITL and
  //     headless modes).
  //   * allow -> a PreToolUse gate that DENIES any `mcp__<server>__<tool>` whose
  //     tool is not in that server's allow list. The SDK can't hide non-allowed
  //     tools at exposure time without enumerating every tool name, so allow is
  //     enforced at execution. A deny here is final (canUseTool can't override
  //     it); an allowed tool returns `{continue:true}` so the HITL canUseTool
  //     gate and the DLP hook still run untouched (returning
  //     `permissionDecision:"allow"` would short-circuit them - see
  //     makeDlpPreToolUse).
  const blockedToolNames: string[] = [];
  for (const [serverName, f] of Object.entries(mcpFilters)) {
    if (f?.block?.length) {
      for (const tool of f.block) {
        blockedToolNames.push(`mcp__${serverName}__${tool}`);
      }
    }
  }
  const allowGate = makeMcpAllowGate(mcpFilters);
  const preUseHooks = [makeDlpPreToolUse(env)];
  if (allowGate) preUseHooks.push(allowGate);

  const options: Options = {
    cwd: ws,
    additionalDirectories: [inputsDir(), outputsDir(), SKILLS_DIR],
    systemPrompt: hitl
      ? `${opts.systemPrompt}\n\n${ASK_USER_PROMPT_BLOCK}`
      : opts.systemPrompt,
    mcpServers,
    // Remove Anthropic server-side tools that can't execute against the
    // OpenAI-compatible (deepseek) gateway, PLUS per-server tool_filter block
    // lists (as `mcp__<server>__<tool>` names). Both are exposure filters: the
    // model never sees these tools. This only narrows the available tool set;
    // it is orthogonal to permissionMode / canUseTool (HITL) / the DLP +
    // allow-gate PreToolUse hooks / mcpServers, which all keep operating on the
    // remaining (client-side + MCP) tools.
    disallowedTools: [
      ...SERVER_SIDE_TOOLS_UNAVAILABLE_ON_GATEWAY,
      ...blockedToolNames,
    ],
    // True per-token streaming: emit SDKPartialAssistantMessage (`stream_event`)
    // partials so event_translator can produce incremental TextBlockDeltaEvents
    // (one per text_delta) instead of one whole-text delta. The translator
    // de-duplicates against the final consolidated `assistant` message.
    includePartialMessages: true,
    hooks: {
      PreToolUse: [{ hooks: preUseHooks }],
    },
  };
  if (hitl) {
    // Gate every tool through canUseTool (requires NOT bypassing permissions).
    mcpServers.hitl = buildAskUserServer();
    options.permissionMode = "default";
    options.canUseTool = makeCanUseTool(hitlCtx, task);
  } else {
    // Headless sandbox: no human to confirm tool calls — auto-approve all.
    options.permissionMode = "bypassPermissions";
    options.allowDangerouslySkipPermissions = true;
  }
  if (model) options.model = model;
  if (opts.abortController) options.abortController = opts.abortController;

  // ── sampling / reasoning controls ──
  // The Claude Agent SDK's Options surface exposes ONLY `maxThinkingTokens` (the
  // thinking budget) and `model`/turns/budget — it has NO field for the model's
  // output `max_tokens`, nor for reasoning effort or structured outputs. So:
  //   - sampling.max_tokens (the OUTPUT cap) must NOT be put on maxThinkingTokens
  //     (the old bug: it conflated the output cap with the thinking budget). It
  //     is instead carried to the gateway adapter (main.ts) and applied to the
  //     OpenAI `max_tokens`.
  //   - reasoning_effort, when set, is mapped to high/max (DeepSeek's only
  //     accepted values) and carried to the adapter as `reasoning_effort`.
  // Here in worker.ts we only flip the SDK switch that makes it EMIT the
  // Anthropic `thinking:{type:"enabled"}` field on the wire (which the adapter
  // collapses to DeepSeek's thinking enable): the SDK emits `thinking` iff
  // maxThinkingTokens > 0. So when the task requests reasoning effort we set a
  // nominal thinking budget to turn thinking on; the actual reasoning depth is
  // governed by reasoning_effort at the gateway, not this budget.
  const effort = mapReasoningEffort(task.reasoning_effort);
  if (effort) {
    options.maxThinkingTokens = THINKING_ENABLE_BUDGET;
  }
  if (sampling.temperature != null || sampling.top_p != null) {
    console.log(
      "[worker] sampling temperature/top_p are not exposed by the SDK Options; ignoring",
    );
  }

  // ── MCP timeout (CLI subprocess env) ──
  // The SDK query() has no timeout option and McpServerConfig has no timeout
  // field the CLI consumes (the per-server `timeout` attached by withTimeout
  // is forward-compatible only today). The CLI subprocess DOES honour two env
  // vars, though: MCP_TIMEOUT (connect/init, default 30000ms) and
  // MCP_TOOL_TIMEOUT (per tool-call, default unlimited). The SDK uses
  // `options.env` as the CLI's process.env (sdk.mjs: `options.env ?? process.env`),
  // so we aggregate the per-server `timeout_seconds` (carried on each config as
  // `timeout`, in seconds) down to a global MIN and inject both vars in ms.
  // See minMcpTimeoutSeconds for the MIN-aggregation rationale. When no server
  // carries a positive timeout, options.env is left unset so the CLI defaults
  // apply. This only affects worker-direct http/stdio MCP paths; broker egress
  // is bounded at the broker.
  const mcpTimeoutSeconds = minMcpTimeoutSeconds(mcpServers);
  if (mcpTimeoutSeconds !== undefined) {
    const ms = String(mcpTimeoutSeconds * 1000);
    options.env = {
      ...process.env,
      MCP_TIMEOUT: ms,
      MCP_TOOL_TIMEOUT: ms,
    };
  }

  let summary = "";
  /** block_id of the text block the summary was last extended from. */
  let summaryBlockId: string | null = null;
  let sawError = false;
  let errorMessage: string | undefined;
  /** SDK error code of an API failure the CLI surfaced as an assistant message. */
  let apiError: string | undefined;
  /** SDK result subtype behind a failed run (error_max_turns, …). */
  let resultSubtype: string | undefined;
  /** A result message (success or error) was received. */
  let sawResult = false;
  /** Aborted from outside (SIGTERM / abortController) before finishing. */
  let cancelled = false;

  // Tool-call accumulators, used to drive the Redis token-stream pushes in the
  // exact order the AgentScope worker uses (tool_call after ToolCallEnd;
  // tool_result after ToolResultEnd; delta as text streams; end at the close).
  const toolArgs = new Map<string, string>();
  const toolResultText = new Map<string, string>();
  /** Tool calls already closed: a repeat close is dropped entirely. */
  const closedToolCalls = new Set<string>();

  // HITL transcript accumulator: text and tool calls in stream order, in a form
  // we can serialize into the paused blob's agent_state for history-replay.
  const acc = newTranscriptAccumulator();

  // Prompt selection. A resume leg reconstructs the conversation as a streamed
  // AsyncIterable<SDKUserMessage> (history-replay); a fresh run uses today's
  // single string prompt with the resume note prepended.
  //
  // Cache-friendliness (fresh runs only): the resume note is the only
  // per-run-varying piece of context, so it lives on the user turn (here)
  // instead of the system prompt, keeping the static system prefix cacheable.
  const resumeNote = opts.resumeNote ?? "";
  const prompt: string | AsyncIterable<SDKUserMessage> = task.resume
    ? buildReplayPrompt(task)
    : resumeNote
      ? `${resumeNote}\n\n${task.task}`
      : task.task;

  try {
    const response = queryFn({ prompt, options });

    for await (const msg of response as AsyncIterable<SDKMessage>) {
      const events = translator.translateMessage(msg);
      for (const ev of events) {
        // Defensive: the translator closes each tool call exactly once. Should a
        // second close for the same id ever slip through, drop it entirely — no
        // second tool_call entry, transcript record or checkpoint.
        if (ev.event_type === "ToolCallEndEvent") {
          const id = String(ev.tool_call_id ?? "");
          if (id && closedToolCalls.has(id)) continue;
          if (id) closedToolCalls.add(id);
        }

        appendEvent(evPath, ev);

        // HITL pause guard: when canUseTool denies+interrupts a tool call, the
        // SDK emits a synthetic tool_result (is_error, "paused for human
        // approval") for that call. We MUST NOT push that result to the token
        // stream — the frontend treats any tool_result as "interaction
        // consumed" and would acknowledge/remove the very pending card we're
        // about to raise, so the ConfirmCard/QuestionCard never renders (and
        // can't be rehydrated). The pending tool_call_id(s) are recorded by
        // canUseTool before this result arrives, so skip the matching result.
        if (
          ev.event_type === "ToolResultEndEvent" &&
          hitlCtx.pending.some((p) => p.id === String(ev.tool_call_id ?? ""))
        ) {
          continue;
        }

        await pushToRedis(opts.redis, ev, toolArgs, toolResultText, translator);

        if (ev.event_type === "TextBlockDeltaEvent") {
          const delta = String(ev.delta ?? "");
          const blockId = String(ev.block_id ?? "");
          if (delta) {
            // Separate text blocks (e.g. before and after a tool call) by a
            // paragraph break instead of running them together.
            if (summary && blockId !== summaryBlockId) {
              summary = withParagraphBreak(summary);
            }
            summaryBlockId = blockId;
            summary += delta;
          }
          recordTranscriptText(acc, blockId, delta);
        } else if (ev.event_type === "ToolCallEndEvent") {
          // Record the closed tool call (at its position) for the replay snapshot.
          const id = String(ev.tool_call_id ?? "");
          recordTranscriptToolCall(
            acc,
            id,
            translator.toolNameFor(id),
            toolCallArgs(ev, toolArgs),
          );
          // RUNNING checkpoint + result.json refresh after each tool call.
          await opts.redis.writeStep({
            cursor: "ToolCallEndEvent",
            status: "RUNNING",
          });
          writeResult(ws, {
            status: "RUNNING",
            summary,
            usage: translator.getCumulativeUsage(),
            plan_steps: [],
          });
        } else if (ev.event_type === "ToolResultEndEvent") {
          const id = String(ev.tool_call_id ?? "");
          recordTranscriptToolResult(acc, id, toolResultText.get(id) ?? "");
        } else if (ev.event_type === "ApiErrorEvent") {
          // Not terminal by itself (the CLI may recover); the result decides.
          apiError = String(ev.error ?? "unknown");
        } else if (ev.event_type === "ErrorEvent") {
          sawResult = true;
          sawError = true;
          errorMessage = String(ev.message ?? "result error");
          resultSubtype = typeof ev.subtype === "string" ? ev.subtype : undefined;
        } else if (ev.event_type === "ReplyEndEvent") {
          sawResult = true;
        }
      }

      // The pause is signalled inside canUseTool; once observed we stop draining
      // the iterator (the SDK is interrupting the run anyway). Breaking here also
      // covers SDKs that keep the iterator open after a deny+interrupt.
      if (hitlCtx.paused) break;
    }
  } catch (err) {
    if (hitlCtx.paused) {
      // canUseTool recorded a pending confirmation/question and interrupted the
      // run on purpose; the SDK may surface that as a throw (AbortError).
      console.log(
        `[worker] query() interrupted for HITL pause (${(err as Error)?.name ?? "?"})`,
      );
    } else if (opts.abortController?.signal.aborted) {
      cancelled = true;
      console.log("[worker] query() aborted from outside: run cancelled");
    } else {
      // A real failure — including an AbortError with nothing pending (never
      // mistaken for a pause: there is nothing for a human to answer).
      sawError = true;
      errorMessage =
        err instanceof AbortError
          ? `query aborted unexpectedly: ${err.message}`
          : (err as Error)?.message ?? String(err);
      appendEvent(evPath, {
        event_type: "ErrorEvent",
        message: errorMessage,
      });
    }
  }

  // Aborted from outside without the iterator throwing, before any result.
  if (
    !hitlCtx.paused &&
    !cancelled &&
    !sawResult &&
    opts.abortController?.signal.aborted
  ) {
    cancelled = true;
  }
  // An API error with no result after it: the run did not recover.
  if (!hitlCtx.paused && !cancelled && !sawResult && apiError && !sawError) {
    sawError = true;
    errorMessage = `model API error (${apiError})`;
  }

  const usage = translator.getCumulativeUsage();

  // ── HITL pause: paused blob + pending entries + end(status="paused") ──
  if (hitlCtx.paused) {
    return await handlePause(task, opts.redis, hitlCtx, acc, summary, usage);
  }

  // Every other exit closes the token stream with exactly one terminal entry
  // (the client drops duplicates); a failure first pushes a user-safe `error`.
  if (cancelled) {
    await opts.redis.pushTokenEnd("cancelled");
    return {
      status: "cancelled",
      summary: summary.trim(),
      error: "run cancelled",
      usage,
      plan_steps: [],
    };
  }

  summary = summary.trim();
  if (sawError) {
    await opts.redis.pushError(userSafeErrorMessage({ apiError, resultSubtype }));
    await opts.redis.pushTokenEnd("failed");
    return {
      status: "FAILED",
      summary: summary || "worker run failed",
      error: errorMessage ?? "see events.jsonl",
      usage,
      plan_steps: [],
    };
  }
  if (!summary) summary = "(agent returned an empty reply)";
  await opts.redis.pushTokenEnd("completed");
  return { status: "OK", summary, usage, plan_steps: [] };
}

/** Append a paragraph break unless `s` already ends with one. */
function withParagraphBreak(s: string): string {
  if (s.endsWith("\n\n")) return s;
  return s.endsWith("\n") ? `${s}\n` : `${s}\n\n`;
}

/**
 * The complete JSON args of a closed tool call: the ToolCallEndEvent's `args`
 * (consolidated, CLI-normalised input) when present, else the streamed deltas.
 */
function toolCallArgs(ev: EventPayload, toolArgs: Map<string, string>): string {
  if (typeof ev.args === "string") return ev.args;
  return toolArgs.get(String(ev.tool_call_id ?? "")) ?? "";
}

/**
 * Persist a HITL pause, in an order that is safe for a consumer reacting to
 * the stream: write the paused blob (our replay transcript under agent_state),
 * mark the run PAUSED (also extending the token stream to the paused TTL),
 * then push the pending confirmation/question entries and finally
 * `end(status="paused")` — the resume leg (a new process) appends to the same
 * stream. Returns a paused RunResult. Failure-open: the client swallows Redis
 * errors.
 */
async function handlePause(
  task: Task,
  redis: RedisStateClient,
  ctx: HitlContext,
  acc: TranscriptAccumulator,
  summary: string,
  usage: Usage,
): Promise<RunResult> {
  const pending: PendingToolCall[] = ctx.pending;
  const kind = ctx.kind === "question" ? "question" : "confirm";

  // Our replay transcript → agent_state (opaque to the orchestrator).
  const replayState: ReplayState = snapshotTranscriptForPause(task, acc, pending);

  await redis.writePaused({
    kind,
    reply_id: "",
    tool_calls: pending.map((p) => ({ id: p.id, name: p.name, args: p.args })),
    question: kind === "question" ? ctx.question ?? "" : null,
    agent_state: replayState,
    paused_at: new Date().toISOString(),
  });
  await redis.markPaused();

  // Token-stream pending entries (one per pending call), then the terminal end.
  if (kind === "question") {
    const first = pending[0];
    await redis.pushPendingQuestion(
      first?.id ?? "",
      ctx.question ?? "",
    );
  } else {
    for (const p of pending) {
      await redis.pushPendingConfirmation(p.id, p.name, p.args);
    }
  }
  await redis.pushTokenEnd("paused");

  console.log(
    `[worker] run paused for HITL (${kind}); ${pending.length} pending tool call(s)`,
  );

  return {
    status: "paused",
    summary: summary.trim(),
    usage,
    plan_steps: [],
  };
}

/**
 * Push one translated event into the Redis token stream, mirroring the
 * AgentScope worker's push order: text deltas → delta; tool call args coalesced
 * and pushed (once per call, complete JSON) on ToolCallEnd; tool result text
 * coalesced and pushed on ToolResultEnd.
 *
 * Thinking: live reasoning chunks stream as `thinking_delta` entries (the
 * client coalesces adjacent chunks) so reasoning shows while it happens, before
 * the answer; a whole block from a non-streamed (consolidated-only) message is
 * pushed as one `thinking` entry.
 */
async function pushToRedis(
  redis: RedisStateClient,
  ev: EventPayload,
  toolArgs: Map<string, string>,
  toolResultText: Map<string, string>,
  translator: EventTranslator,
): Promise<void> {
  switch (ev.event_type) {
    case "TextBlockDeltaEvent": {
      const delta = String(ev.delta ?? "");
      if (delta) await redis.pushToken(delta);
      break;
    }
    case "ToolCallDeltaEvent": {
      const id = String(ev.tool_call_id ?? "");
      toolArgs.set(id, (toolArgs.get(id) ?? "") + String(ev.delta ?? ""));
      break;
    }
    case "ToolCallEndEvent": {
      const id = String(ev.tool_call_id ?? "");
      await redis.pushToolCall(
        id,
        translator.toolNameFor(id),
        toolCallArgs(ev, toolArgs),
      );
      break;
    }
    case "ToolResultTextDeltaEvent": {
      const id = String(ev.tool_call_id ?? "");
      toolResultText.set(
        id,
        (toolResultText.get(id) ?? "") + String(ev.delta ?? ""),
      );
      break;
    }
    case "ToolResultEndEvent": {
      const id = String(ev.tool_call_id ?? "");
      await redis.pushToolResult(
        id,
        translator.toolNameFor(id),
        toolResultText.get(id) ?? "",
        String(ev.state ?? "success"),
      );
      break;
    }
    case "ThinkingBlockDeltaEvent": {
      const text = String(ev.delta ?? "");
      if (!text) break;
      if (ev.whole_block === true) {
        await redis.pushThinking(text);
      } else {
        await redis.pushThinkingDelta(text);
      }
      break;
    }
    default:
      break;
  }
}

async function ensureDirs(): Promise<void> {
  for (const dir of [inputsDir(), outputsDir()]) {
    try {
      await mkdir(dir, { recursive: true });
    } catch (err) {
      console.warn(`[worker] cannot create ${dir}: ${(err as Error).message}`);
    }
  }
  // Make sure events.jsonl exists even if the agent emits nothing.
  const evPath = eventsPath();
  try {
    await writeFile(evPath, "", { flag: "a" });
  } catch (err) {
    console.warn(`[worker] cannot touch ${evPath}: ${(err as Error).message}`);
  }
}

/**
 * The resolved workspace path. A getter (not a frozen const) so main() and tests
 * always read the current value even if WORKER_WORKSPACE_DIR changes per test.
 */
export function getWorkspaceDir(): string {
  return workspaceDir();
}
