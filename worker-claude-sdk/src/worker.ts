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
  type McpServerConfig,
  type Options,
  type SDKMessage,
  type HookJSONOutput,
} from "@anthropic-ai/claude-agent-sdk";

import type { Env, Task } from "./task.js";
import type { RedisStateClient } from "./redis_state.js";
import { appendEvent, writeResult, type Usage } from "./result.js";
import { EventTranslator, type EventPayload } from "./event_translator.js";

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

/** Result of a completed run, surfaced to main() for result.json + exit code. */
export interface RunResult {
  status: "OK" | "FAILED";
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

  const url = `${base.replace(/\/+$/, "")}/internal/mcp-servers`;
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
    const servers: any[] = Array.isArray(body)
      ? body
      : Array.isArray(body?.servers)
        ? body.servers
        : [];

    const out: Record<string, McpServerConfig> = {};
    for (const raw of servers) {
      const mapped = mapRegistryServer(raw);
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
 */
function mapRegistryServer(
  // eslint-disable-next-line @typescript-eslint/no-explicit-any -- registry JSON
  raw: any,
): { name: string; config: McpServerConfig } | null {
  const name = String(raw?.name ?? "").trim();
  if (!name) return null;
  const type = String(raw?.type ?? (raw?.url ? "http" : "")).toLowerCase();

  if ((type === "http" || type === "sse") && raw?.url) {
    return {
      name,
      config: {
        type: type as "http" | "sse",
        url: String(raw.url),
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
 * Read the Redis checkpoint; if a prior non-terminal run exists, return a resume
 * note (matches the Python L569-589 wording). Otherwise return "". Failure-open.
 */
export async function buildResumeContext(
  redis: RedisStateClient,
): Promise<string> {
  const checkpoint = await redis.readCheckpoint();
  if (
    checkpoint === null ||
    checkpoint.status === "COMPLETED" ||
    checkpoint.status === "FAILED"
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

// ── PreToolUse hook (placeholder allow-all; DLP integration is a TODO) ───────

/**
 * v1 PreToolUse hook: allow every tool call. The hook mechanism is wired so a
 * real DLP check can be dropped in later (see spike runner.ts for the deny path).
 * TODO(DLP): replace with a `fetch()` to the DLP gateway and honour its verdict.
 */
async function allowAllPreToolUse(): Promise<HookJSONOutput> {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason: "v1 allow-all (DLP integration pending)",
    },
  };
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

  const evPath = eventsPath();
  const ws = workspaceDir();
  const options: Options = {
    cwd: ws,
    additionalDirectories: [inputsDir(), outputsDir(), SKILLS_DIR],
    systemPrompt: opts.systemPrompt,
    mcpServers: opts.mcpServers,
    // Headless sandbox: no human to confirm tool calls.
    permissionMode: "bypassPermissions",
    allowDangerouslySkipPermissions: true,
    hooks: {
      PreToolUse: [{ hooks: [allowAllPreToolUse] }],
    },
  };
  if (model) options.model = model;
  // SDK sampling controls: only max_tokens has a direct Options analogue
  // (maxThinkingTokens). temperature / top_p are not exposed by the SDK Options
  // surface in this version, so they are intentionally dropped (logged once).
  if (sampling.max_tokens != null) {
    options.maxThinkingTokens = sampling.max_tokens;
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

  // Cache-friendliness: the resume note is the only per-run-varying piece of
  // context, so it lives on the user turn (here) instead of the system prompt.
  // The model still learns it is resuming from a checkpoint; the static system
  // prefix is left untouched so DeepSeek prefix-caching can hit across runs.
  const resumeNote = opts.resumeNote ?? "";
  const prompt = resumeNote ? `${resumeNote}\n\n${task.task}` : task.task;

  try {
    const response = queryFn({ prompt, options });

    for await (const msg of response as AsyncIterable<SDKMessage>) {
      const events = translator.translateMessage(msg);
      for (const ev of events) {
        appendEvent(evPath, ev);
        await pushToRedis(opts.redis, ev, toolArgs, toolResultText, translator);

        if (ev.event_type === "TextBlockDeltaEvent") {
          summary += String(ev.delta ?? "");
        } else if (ev.event_type === "ToolCallEndEvent") {
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
        } else if (ev.event_type === "ErrorEvent") {
          sawError = true;
          errorMessage = String(ev.message ?? "result error");
        }
      }
    }
  } catch (err) {
    sawError = true;
    errorMessage = (err as Error)?.message ?? String(err);
    appendEvent(evPath, {
      event_type: "ErrorEvent",
      message: errorMessage,
    });
  }

  // Close the Redis token stream regardless of outcome.
  await opts.redis.pushTokenEnd();

  const usage = translator.getCumulativeUsage();
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
 * Push one translated event into the Redis token stream, mirroring the
 * AgentScope worker's push order: text deltas → delta; tool call args coalesced
 * and pushed on ToolCallEnd; tool result text coalesced and pushed on
 * ToolResultEnd; thinking pushed on ThinkingBlockEnd.
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
    case "ThinkingBlockDeltaEvent": {
      const delta = String(ev.delta ?? "");
      if (delta) await redis.pushThinking(delta);
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
