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

import type { Env, Task } from "./task.js";
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
  snapshotTranscriptForPause,
  type HitlContext,
  type PendingToolCall,
  type ReplayState,
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

/** Result of a completed run, surfaced to main() for result.json + exit code. */
export interface RunResult {
  status: "OK" | "FAILED" | "paused";
  summary: string;
  error?: string;
  usage: Usage;
  plan_steps: never[];
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
 * ~8000 tokens (B.2). Older turns are dropped from the front.
 */
export function historyToText(task: Task): string {
  const history = task.history ?? [];
  const lines: string[] = [];
  for (const item of history) {
    if (item.role === "user") {
      lines.push(`[user]: ${item.content ?? ""}`);
    } else if (item.role === "assistant") {
      lines.push(`[assistant]: ${item.content ?? ""}`);
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
      lines.push(`[tool_call:${name}]: ${input}`);
      lines.push(`[tool_result:${name}]: ${result}`);
    }
  }
  if (lines.length === 0) return "";

  // Keep the most recent lines under the ~8000-token budget.
  const MAX_TOKENS = 8000;
  const kept: string[] = [];
  let tokens = 0;
  for (let i = lines.length - 1; i >= 0; i--) {
    const t = estimateTokens(lines[i]);
    if (tokens + t > MAX_TOKENS && kept.length > 0) break;
    kept.push(lines[i]);
    tokens += t;
  }
  kept.reverse();
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
  if (memory) parts.push(`## 相关历史记忆\n\n${memory}`);

  const historyText = historyToText(task);
  if (historyText) parts.push(`## 历史对话\n\n${historyText}`);

  const sysPrompt = (task.sys_prompt ?? "").trim();
  if (sysPrompt) parts.push(sysPrompt);

  // Skills index (B.3): advertise name + SKILL.md path; the model Reads on demand.
  const skills = task.skills ?? [];
  if (skills.length > 0) {
    const lines = ["## 可用技能（需要时用 Read 工具读取指令文件）"];
    for (const name of skills) {
      // description left empty in v1 (B.3); only the path is advertised.
      lines.push(`- ${name}: 见 ${SKILLS_DIR}/${name}/SKILL.md`);
    }
    parts.push(lines.join("\n"));
  }

  if (downloadedFiles.length > 0) {
    const inputs = inputsDir();
    parts.push(
      `用户上传了以下文件，位于 ${inputs}/：${downloadedFiles.join(", ")}\n` +
        `请使用 Read 工具读取 ${inputs}/ 下的文件来完成任务。`,
    );
  }

  return parts.join("\n\n");
}

// ── MCP registry ────────────────────────────────────────────────────────────

/**
 * Resolve the tenant's MCP servers from the registry. failure-open: when
 * MCP_REGISTRY_URL is unset or the call fails, returns {} and logs a warning.
 *
 * B.5/F.5: the registry's server records are mapped onto the SDK's
 * Record<string, McpServerConfig>. The registry is expected to return
 * `{servers: [{name, type, url|command, headers?, args?, env?}]}`. If the real
 * registry shape diverges, extend `mapRegistryServer` below.
 */
export async function loadMcpServers(
  env: Env,
): Promise<Record<string, McpServerConfig>> {
  const base = env.MCP_REGISTRY_URL;
  if (!base) {
    console.warn("[worker] MCP_REGISTRY_URL unset; no MCP servers loaded");
    return {};
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
      return {};
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

    const out: Record<string, McpServerConfig> = {};
    for (const raw of servers) {
      const mapped = mapRegistryServer(raw, env);
      if (mapped) out[mapped.name] = mapped.config;
    }
    console.log(`[worker] loaded ${Object.keys(out).length} MCP server(s) from registry`);
    return out;
  } catch (err) {
    console.warn(
      `[worker] mcp-registry fetch failed; no MCP servers loaded: ${(err as Error).message}`,
    );
    return {};
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
function mapRegistryServer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  raw: any,
  env: Env,
): { name: string; config: McpServerConfig } | null {
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
      return {
        name,
        config: {
          type: type as "http" | "sse",
          url: `${brokerBase}/mcp-tenant/v1/${encodeURIComponent(id)}`,
          // The SDK re-sends these configured headers on EVERY MCP request. The
          // broker's auth runs in two gates: `_require_auth` (needs an
          // Authorization bearer + X-Tenant-Id, exactly like the registry fetch
          // above) THEN `_require_lease` (needs `x-tool-broker-lease` carrying
          // `mcp.tenant_invoke`). Both must be present on every tool call or the
          // broker 401s before the lease is even checked, so we attach all three
          // (mirroring loadMcpServers' registry-fetch header set). Any
          // manifest-supplied headers are preserved and take lowest precedence.
          headers: {
            ...((raw.headers as Record<string, string>) ?? {}),
            authorization: `Bearer ${env.FOUNDATION_API_TOKEN}`,
            "x-tenant-id": env.FOUNDATION_TENANT_ID,
            ...(env.TOOL_BROKER_LEASE
              ? { "x-tool-broker-lease": env.TOOL_BROKER_LEASE }
              : {}),
          },
        },
      };
    }
    // Non-broker mode (foundation/none/unset): keep current behavior exactly.
    return {
      name,
      config: {
        type: type as "http" | "sse",
        url: String(url),
        ...(raw.headers ? { headers: raw.headers as Record<string, string> } : {}),
      },
    };
  }
  if (type === "stdio" && raw?.command) {
    return {
      name,
      config: {
        type: "stdio",
        command: String(raw.command),
        ...(Array.isArray(raw.args) ? { args: raw.args as string[] } : {}),
        ...(raw.env ? { env: raw.env as Record<string, string> } : {}),
      },
    };
  }
  console.warn(`[worker] skipping unmappable MCP server record: ${name} (type=${type || "?"})`);
  return null;
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
 * be mistaken for a resume, or every brand-new run gets a bogus "已完成 N 步"
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
    `[系统：本次运行从断点恢复，上次已完成 ${step} 步。` +
    (plan ? `上次计划：${plan}` : "") +
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

// ── HITL: ask_user tool + system prompt block ────────────────────────────────

/**
 * The HITL system-prompt block (parity with the old worker __main__.py:1268-1277).
 * Without it the model asks clarification questions as plain reply text and keeps
 * going instead of calling ask_user, so the pause/resume machinery never engages.
 */
export const ASK_USER_PROMPT_BLOCK =
  "## 向用户提问（ask_user）\n" +
  "当你需要用户澄清需求、在多个方案中做选择，或缺少继续执行所必需的信息时，" +
  "必须调用 ask_user 工具提问并等待用户回答；不要在正文里提问后自行猜测继续，" +
  "也不要把问题作为最终回复结束任务。用户回答后会自动继续本次任务。";

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
    "向用户提出一个澄清问题并等待用户回答。当你需要用户提供信息、做选择或澄清需求时调用。",
    { question: z.string().describe("要向用户提出的问题") },
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
    "抓取公开 HTTP/HTTPS 网页、API 或文档的内容。这是本沙盒唯一的联网方式：" +
      "沙盒网络被隔离，无法直接 curl/访问外网，任何需要联网获取的信息（网页正文、" +
      "公开 API 响应、在线文档等）都必须调用本工具，绝不要尝试用 Bash/curl 直连外网。" +
      "返回网页文本内容（可能被截断）。",
    {
      url: z
        .string()
        .describe("要抓取的完整 http/https URL（必填）"),
      max_bytes: z
        .number()
        .optional()
        .describe("响应正文最大字节数（可选，默认 1048576 = 1 MiB）"),
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
    redis: RedisStateClient;
    /**
     * Resume note for a checkpoint-resumed run (see buildResumeContext); "" for
     * a fresh run. Prepended to the user task prompt rather than the system
     * prompt so the (static) system prefix stays cache-hittable across runs.
     */
    resumeNote?: string;
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

  // web_fetch: the sandbox's only sanctioned network egress for page content.
  // Mounted in BOTH headless and HITL modes (the model needs it regardless of a
  // human gate), but only when the scheduler injected a gateway URL — matching
  // the AgentScope behavior of not registering the tool when egress is unset.
  if (env.WEB_FETCH_GATEWAY_URL) {
    mcpServers.webfetch = buildWebFetchServer(env);
  }

  const options: Options = {
    cwd: ws,
    additionalDirectories: [inputsDir(), outputsDir(), SKILLS_DIR],
    systemPrompt: hitl
      ? `${opts.systemPrompt}\n\n${ASK_USER_PROMPT_BLOCK}`
      : opts.systemPrompt,
    mcpServers,
    // Remove Anthropic server-side tools that can't execute against the
    // OpenAI-compatible (deepseek) gateway. This only narrows the available tool
    // set; it is orthogonal to permissionMode / canUseTool (HITL) / the DLP
    // PreToolUse hook / mcpServers, which all keep operating on the remaining
    // (client-side + MCP) tools. Applied in both HITL and headless modes since
    // it sits on the shared `options` built before the per-mode branch below.
    disallowedTools: SERVER_SIDE_TOOLS_UNAVAILABLE_ON_GATEWAY,
    // True per-token streaming: emit SDKPartialAssistantMessage (`stream_event`)
    // partials so event_translator can produce incremental TextBlockDeltaEvents
    // (one per text_delta) instead of one whole-text delta. The translator
    // de-duplicates against the final consolidated `assistant` message.
    includePartialMessages: true,
    hooks: {
      PreToolUse: [{ hooks: [makeDlpPreToolUse(env)] }],
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

  let summary = "";
  let sawError = false;
  let errorMessage: string | undefined;

  // Tool-call accumulators, used to drive the Redis token-stream pushes in the
  // exact order the AgentScope worker uses (tool_call after ToolCallEnd;
  // tool_result after ToolResultEnd; delta as text streams; end at the close).
  const toolArgs = new Map<string, string>();
  const toolResultText = new Map<string, string>();
  // Thinking accumulator: buffer reasoning deltas per block_id and push the whole
  // block as ONE Redis type=thinking entry on its End (see pushToRedis doc).
  const thinkingBuffers = new Map<string, string>();

  // HITL transcript accumulator: mirrors the token-stream maps but in a form we
  // can serialize into the paused blob's agent_state for history-replay.
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

        await pushToRedis(
          opts.redis,
          ev,
          toolArgs,
          toolResultText,
          thinkingBuffers,
          translator,
        );

        if (ev.event_type === "TextBlockDeltaEvent") {
          summary += String(ev.delta ?? "");
          acc.assistantText += String(ev.delta ?? "");
        } else if (ev.event_type === "ToolCallEndEvent") {
          // Record the completed tool call for the replay snapshot.
          recordToolCall(acc, ev, toolArgs, translator);
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
          recordToolResult(acc, ev, toolResultText);
        } else if (ev.event_type === "ErrorEvent") {
          sawError = true;
          errorMessage = String(ev.message ?? "result error");
        }
      }

      // The pause is signalled inside canUseTool; once observed we stop draining
      // the iterator (the SDK is interrupting the run anyway). Breaking here also
      // covers SDKs that keep the iterator open after a deny+interrupt.
      if (hitlCtx.paused) break;
    }
  } catch (err) {
    // A deny+interrupt may surface as a thrown AbortError. When we have already
    // flagged a pause, treat ANY throw as the pause path (the run was
    // intentionally interrupted), not a failure. Otherwise it is a real error.
    if (hitlCtx.paused || err instanceof AbortError) {
      hitlCtx.paused = true;
      console.log(
        `[worker] query() interrupted for HITL pause (${(err as Error)?.name ?? "?"})`,
      );
    } else {
      sawError = true;
      errorMessage = (err as Error)?.message ?? String(err);
      appendEvent(evPath, {
        event_type: "ErrorEvent",
        message: errorMessage,
      });
    }
  }

  // Wind-down flush: any thinking block that never saw an End (aborted/errored/
  // paused mid-reasoning) is pushed now so its content is not dropped. A clean
  // run leaves no residue here (every block flushed at its End).
  await flushThinkingBuffers(opts.redis, thinkingBuffers);

  const usage = translator.getCumulativeUsage();

  // ── HITL pause: persist the paused blob + pending entries, do NOT end stream ──
  if (hitlCtx.paused) {
    return await handlePause(task, opts.redis, hitlCtx, acc, summary, usage);
  }

  // Close the Redis token stream on a non-paused outcome (a pause leaves the
  // stream open so the resume leg can continue it).
  await opts.redis.pushTokenEnd();

  summary = summary.trim();
  if (!sawError && !summary) summary = "(agent returned an empty reply)";

  if (sawError) {
    return {
      status: "FAILED",
      summary: summary || "worker run failed",
      error: errorMessage ?? "see events.jsonl",
      usage,
      plan_steps: [],
    };
  }
  return { status: "OK", summary, usage, plan_steps: [] };
}

/**
 * Record a completed tool call into the HITL transcript accumulator (name + args
 * keyed by tool_use_id, in first-seen order) so a pause can snapshot it.
 */
function recordToolCall(
  acc: ReturnType<typeof newTranscriptAccumulator>,
  ev: EventPayload,
  toolArgs: Map<string, string>,
  translator: EventTranslator,
): void {
  const id = String(ev.tool_call_id ?? "");
  if (!id) return;
  if (!acc.toolName.has(id)) acc.toolOrder.push(id);
  acc.toolName.set(id, translator.toolNameFor(id));
  acc.toolArgs.set(id, toolArgs.get(id) ?? "");
}

/** Record a tool result text into the accumulator, keyed by tool_use_id. */
function recordToolResult(
  acc: ReturnType<typeof newTranscriptAccumulator>,
  ev: EventPayload,
  toolResultText: Map<string, string>,
): void {
  const id = String(ev.tool_call_id ?? "");
  if (!id) return;
  acc.toolResult.set(id, toolResultText.get(id) ?? "");
}

/**
 * Persist a HITL pause: push the pending confirmation/question entries (NO
 * type=end — the stream stays open for the resume leg), write the paused blob
 * with our replay transcript under agent_state, mark the run PAUSED, and return a
 * paused RunResult. Failure-open: Redis errors are swallowed by the client.
 */
async function handlePause(
  task: Task,
  redis: RedisStateClient,
  ctx: HitlContext,
  acc: ReturnType<typeof newTranscriptAccumulator>,
  summary: string,
  usage: Usage,
): Promise<RunResult> {
  const pending: PendingToolCall[] = ctx.pending;
  const kind = ctx.kind === "question" ? "question" : "confirm";

  // Token-stream pending entries (one per pending call).
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
 * and pushed on ToolCallEnd; tool result text coalesced and pushed on
 * ToolResultEnd; thinking accumulated per block_id and pushed as ONE entry on
 * ThinkingBlockEnd.
 *
 * Thinking contract (matches the AgentScope worker): the downstream pipeline
 * treats "one Redis type=thinking entry = one complete thought block", so a
 * thinking block is buffered by block_id across its deltas and flushed exactly
 * once at its End. Pushing per-delta would explode one thought into hundreds of
 * standalone collapsible cards in the UI. Text, by contrast, stays per-token
 * streamed (pushToken on each delta) for the live typing experience.
 */
async function pushToRedis(
  redis: RedisStateClient,
  ev: EventPayload,
  toolArgs: Map<string, string>,
  toolResultText: Map<string, string>,
  thinkingBuffers: Map<string, string>,
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
        toolArgs.get(id) ?? "",
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
    case "ThinkingBlockStartEvent": {
      const id = String(ev.block_id ?? "");
      thinkingBuffers.set(id, "");
      break;
    }
    case "ThinkingBlockDeltaEvent": {
      // Accumulate by block_id only; the whole block is pushed once at its End.
      const id = String(ev.block_id ?? "");
      thinkingBuffers.set(id, (thinkingBuffers.get(id) ?? "") + String(ev.delta ?? ""));
      break;
    }
    case "ThinkingBlockEndEvent": {
      const id = String(ev.block_id ?? "");
      const text = thinkingBuffers.get(id) ?? "";
      thinkingBuffers.delete(id);
      if (text) await redis.pushThinking(text);
      break;
    }
    default:
      break;
  }
}

/**
 * Flush any thinking blocks that never received a ThinkingBlockEnd (e.g. the run
 * was aborted/errored mid-block) so their content is not silently dropped. Each
 * residual buffer is pushed as one Redis thinking entry — same "one entry = one
 * block" contract as the normal End path. Buffers are consumed (cleared) so this
 * is safe to call once at run wind-down.
 */
async function flushThinkingBuffers(
  redis: RedisStateClient,
  thinkingBuffers: Map<string, string>,
): Promise<void> {
  if (thinkingBuffers.size === 0) return;
  for (const text of thinkingBuffers.values()) {
    if (text) await redis.pushThinking(text);
  }
  console.log(
    `[worker] flushed ${thinkingBuffers.size} unterminated thinking block(s) at wind-down`,
  );
  thinkingBuffers.clear();
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
