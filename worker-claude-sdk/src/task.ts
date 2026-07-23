/**
 * task.ts — task.json typing + delivery + env validation/reader.
 *
 * The scheduler hands a worker its job two ways (env wins over file):
 *   1. AGENT_TASK_JSON — the complete task.json as a single JSON string.
 *   2. /workspace/task.json — a file mounted into the sandbox.
 *
 * Field shapes mirror the existing Python runtime_worker contract so a Claude
 * SDK run and an AgentScope run are interchangeable upstream.
 */

import { readFileSync } from "node:fs";

// ── task.json shape (aligned with the existing runtime_worker contract) ──────

/** A prompt-time file reference (uploaded blob in S3). */
export interface FileSpec {
  key: string;
  filename: string;
  mime: string;
}

/** One prior conversation turn. `tool` is present on tool turns only. */
export interface HistoryItem {
  role: "user" | "assistant" | "tool";
  content?: string;
  tool?: {
    id: string;
    name: string;
    input: unknown;
    result: unknown;
  };
}

/**
 * A structured-output request. Mirrors the Anthropic `output_config.format`
 * shape `{type:"json_schema", schema:{...}}`. The adapter maps it onto the
 * OpenAI `response_format:{type:"json_schema", json_schema:{...}}` constrained
 * decoding supported by the self-hosted vLLM/SGLang backend. `schema` is the
 * raw JSON Schema and is forwarded verbatim (never rewritten).
 */
export interface ResponseFormat {
  type?: string;
  schema?: unknown;
}

/** Sampling overrides; all optional, passed through to the model when set. */
export interface SamplingParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  /**
   * Structured-output schema. Accepted under `response_format` (the OpenAI-style
   * key the orchestrator/app layer uses). The adapter emits a json_schema
   * response_format only when a schema is present; absent => no constraint.
   */
  response_format?: ResponseFormat;
}

/**
 * The resume payload the orchestrator forwards on a HITL continuation leg. It is
 * written by the scheduler into `task.json["resume"]` (docker_runtime.py:927-928
 * / cube_runtime.py:304-305) and originates from the orchestrator
 * (runs.py:795-801).
 *
 * Spelling note: the paused blob uses `kind:"question"` for the ask_user leg,
 * while the orchestrator forwards `kind:"answer"` on resume. The worker accepts
 * BOTH spellings (the old worker did: __main__.py:1470). `agent_state` is the
 * blob we put under `agent_state` on pause, round-tripped back untouched — it
 * carries our own replay transcript (opaque to the orchestrator).
 */
export interface ResumePayload {
  kind: "confirm" | "answer" | "question";
  tool_call_id: string;
  confirmed?: boolean | null;
  answer?: string | null;
  agent_state?: unknown;
}

/** The full task payload the worker executes. */
export interface Task {
  task: string;
  /**
   * LEGACY / UNUSED. The scheduler hardcodes `[]` here (docker_runtime.py:929)
   * because MCP servers are fetched live from the registry at run time. Do NOT
   * read this to decide which servers to enable — use `mcp_server_ids`.
   */
  mcp_servers: unknown[];
  /**
   * Run-level MCP server selection, by registry server id. THREE-STATE, and the
   * distinction is load-bearing — always test with `=== undefined`, never for
   * truthiness (`[]` is falsy but means something entirely different):
   *
   * * `undefined` (key absent)  → use the tenant's default enabled set, i.e. the
   *   historical "register every enabled server" behaviour.
   * * `[]` (explicit empty)     → MCP is disabled for this run; register nothing.
   * * non-empty list            → register ONLY the servers with these ids.
   */
  mcp_server_ids?: string[];
  skills: string[];
  files: FileSpec[];
  memory: string;
  history: HistoryItem[];
  agent_name?: string;
  sys_prompt?: string;
  model_override?: string;
  reasoning_effort?: string;
  sampling_params: SamplingParams;
  /**
   * Whether human-in-the-loop gating is active. Defaults to `true` (interactive
   * sessions). Scheduled / headless runs set `false`, which bypasses every tool
   * gate (matches the old worker's BYPASS path, __main__.py:1143).
   */
  hitl: boolean;
  /**
   * Present only on a resume leg. `null`/absent on a fresh run. When set, the
   * worker reconstructs the conversation via history-replay instead of a fresh
   * string prompt.
   */
  resume?: ResumePayload | null;
}

// ── task delivery ────────────────────────────────────────────────────────────

const WORKSPACE_TASK_PATH = "/workspace/task.json";

/**
 * Resolve the task payload. AGENT_TASK_JSON (env) takes precedence over the
 * /workspace/task.json file. Throws when neither source is present, or when
 * the chosen source contains malformed JSON — the caller maps a throw to
 * exit code 2 ("invalid task.json").
 */
export function readTask(): Task {
  const fromEnv = process.env.AGENT_TASK_JSON;
  if (fromEnv != null && fromEnv.trim() !== "") {
    return parseTask(fromEnv, "AGENT_TASK_JSON");
  }

  let raw: string;
  try {
    raw = readFileSync(WORKSPACE_TASK_PATH, "utf8");
  } catch {
    throw new Error(
      `no task: AGENT_TASK_JSON unset and ${WORKSPACE_TASK_PATH} not readable`,
    );
  }
  return parseTask(raw, WORKSPACE_TASK_PATH);
}

function parseTask(raw: string, source: string): Task {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new Error(
      `invalid task.json from ${source}: ${(err as Error).message}`,
    );
  }
  if (typeof parsed !== "object" || parsed === null) {
    throw new Error(`invalid task.json from ${source}: not an object`);
  }
  return normalizeTask(parsed as Record<string, unknown>);
}

/** Fill defaults so downstream phases can treat every field as present. */
function normalizeTask(o: Record<string, unknown>): Task {
  return {
    task: typeof o.task === "string" ? o.task : "",
    mcp_servers: Array.isArray(o.mcp_servers) ? o.mcp_servers : [],
    // NOT the `?? []` default the sibling fields use: for mcp_server_ids the
    // absent key and `[]` are DIFFERENT instructions (tenant-default vs
    // disable-all), so absence must survive normalization as `undefined`.
    mcp_server_ids: normalizeMcpServerIds(o.mcp_server_ids),
    skills: Array.isArray(o.skills) ? (o.skills as string[]) : [],
    files: Array.isArray(o.files) ? (o.files as FileSpec[]) : [],
    memory: typeof o.memory === "string" ? o.memory : "",
    history: Array.isArray(o.history) ? (o.history as HistoryItem[]) : [],
    agent_name: typeof o.agent_name === "string" ? o.agent_name : undefined,
    sys_prompt: typeof o.sys_prompt === "string" ? o.sys_prompt : undefined,
    model_override:
      typeof o.model_override === "string" ? o.model_override : undefined,
    reasoning_effort:
      typeof o.reasoning_effort === "string" ? o.reasoning_effort : undefined,
    sampling_params:
      typeof o.sampling_params === "object" && o.sampling_params !== null
        ? (o.sampling_params as SamplingParams)
        : {},
    // hitl defaults to true (interactive); only an explicit `false` disables the
    // tool gate. A missing/non-boolean field keeps the safe interactive default.
    hitl: o.hitl === false ? false : true,
    resume: normalizeResume(o.resume),
  };
}

/**
 * Normalize the run-level MCP selection while PRESERVING the absent/`[]`
 * distinction (see `Task.mcp_server_ids`).
 *
 * * absent / `null` / not an array → `undefined` (fall back to tenant default).
 *   A malformed value is deliberately treated as "absent" rather than as
 *   "disable everything": a scheduler that ships a bad shape degrades to the
 *   pre-existing behaviour instead of silently stripping the user's tools.
 * * an array → the string ids in it (non-strings and blanks dropped). An empty
 *   array stays an empty array, which downstream means "register nothing".
 */
function normalizeMcpServerIds(raw: unknown): string[] | undefined {
  if (!Array.isArray(raw)) return undefined;
  return raw
    .filter((v): v is string => typeof v === "string")
    .map((v) => v.trim())
    .filter((v) => v !== "");
}

/**
 * Normalize the resume payload. Returns null for a fresh run (no resume) or a
 * malformed value. The three accepted `kind` spellings mirror the
 * orchestrator/blob contract (see ResumePayload doc).
 */
function normalizeResume(raw: unknown): ResumePayload | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const kind = o.kind;
  if (kind !== "confirm" && kind !== "answer" && kind !== "question") {
    return null;
  }
  return {
    kind,
    tool_call_id: typeof o.tool_call_id === "string" ? o.tool_call_id : "",
    confirmed:
      typeof o.confirmed === "boolean" ? o.confirmed : o.confirmed === null ? null : undefined,
    answer:
      typeof o.answer === "string" ? o.answer : o.answer === null ? null : undefined,
    agent_state: o.agent_state,
  };
}

// ── env validation ───────────────────────────────────────────────────────────

/** The four infra env vars a run cannot proceed without. */
const REQUIRED_ENV_KEYS = [
  "FOUNDATION_API_URL",
  "FOUNDATION_API_TOKEN",
  "FOUNDATION_TENANT_ID",
  "FOUNDATION_USER_ID",
] as const;

/**
 * Return the list of required infra env vars that are missing/empty. An empty
 * list means all are present. The caller maps a non-empty list to exit code 1
 * ("infra env missing").
 */
export function validateRequiredEnv(): string[] {
  const missing: string[] = [];
  for (const key of REQUIRED_ENV_KEYS) {
    const v = process.env[key];
    if (v == null || v.trim() === "") missing.push(key);
  }
  return missing;
}

// ── Env reader ───────────────────────────────────────────────────────────────

/** All env the worker reads, snapshotted once. Missing values are "". */
export interface Env {
  // Foundation control-plane identity/credentials.
  FOUNDATION_API_URL: string;
  FOUNDATION_API_TOKEN: string;
  FOUNDATION_TENANT_ID: string;
  FOUNDATION_USER_ID: string;
  FOUNDATION_SERVICE_SCOPES: string;
  // LLM gateway.
  LLM_GATEWAY_URL: string;
  LLM_VIRTUAL_KEY: string;
  LLM_MODEL: string;
  // Object storage (prompt files / artifacts).
  S3_ENDPOINT: string;
  S3_BUCKET: string;
  S3_ACCESS_KEY_ID: string;
  S3_SECRET_ACCESS_KEY: string;
  S3_REGION: string;
  // Hot-state.
  REDIS_URL: string;
  RUN_ID: string;
  // Capability gateways the SDK toolchain may dial out to.
  MCP_REGISTRY_URL: string;
  SKILL_STORE_URL: string;
  WEB_FETCH_GATEWAY_URL: string;
  // Tool-broker passthrough base. In broker egress mode this is the only path to
  // internal services (e.g. tenant MCP servers); equals
  // `http://tool-broker:8024/api/tool-broker/v1`. Read from process.env only —
  // main.ts runs loadMcpServers() BEFORE scrubProcessEnvForTools() deletes it.
  TOOL_BROKER_URL: string;
  TOOL_BROKER_LEASE: string;
  // Sandbox egress mode injected by the scheduler: "broker" | "foundation" |
  // "none". In broker mode the sandbox is NOT on the foundation network and can
  // only reach internal services via the tool-broker passthrough. Distinct from
  // the scheduler's own SCHEDULER_SANDBOX_EGRESS_MODE.
  SANDBOX_EGRESS_MODE: string;
  // Data-loss-prevention scan endpoint (PreToolUse hook). Wired by the backend
  // to a direct DLP base (foundation mode) or a broker façade base (broker mode);
  // empty when DLP is not configured (the hook then fails open).
  DLP_URL: string;
  // Observability.
  OTEL_EXPORTER_OTLP_ENDPOINT: string;
}

function get(key: string): string {
  return process.env[key] ?? "";
}

/** Snapshot all worker env vars into a typed object. */
export function readEnv(): Env {
  return {
    FOUNDATION_API_URL: get("FOUNDATION_API_URL"),
    FOUNDATION_API_TOKEN: get("FOUNDATION_API_TOKEN"),
    FOUNDATION_TENANT_ID: get("FOUNDATION_TENANT_ID"),
    FOUNDATION_USER_ID: get("FOUNDATION_USER_ID"),
    FOUNDATION_SERVICE_SCOPES: get("FOUNDATION_SERVICE_SCOPES"),
    LLM_GATEWAY_URL: get("LLM_GATEWAY_URL"),
    LLM_VIRTUAL_KEY: get("LLM_VIRTUAL_KEY"),
    LLM_MODEL: get("LLM_MODEL"),
    S3_ENDPOINT: get("S3_ENDPOINT"),
    S3_BUCKET: get("S3_BUCKET"),
    S3_ACCESS_KEY_ID: get("S3_ACCESS_KEY_ID"),
    S3_SECRET_ACCESS_KEY: get("S3_SECRET_ACCESS_KEY"),
    S3_REGION: get("S3_REGION"),
    REDIS_URL: get("REDIS_URL"),
    RUN_ID: get("RUN_ID"),
    MCP_REGISTRY_URL: get("MCP_REGISTRY_URL"),
    SKILL_STORE_URL: get("SKILL_STORE_URL"),
    WEB_FETCH_GATEWAY_URL: get("WEB_FETCH_GATEWAY_URL"),
    TOOL_BROKER_URL: get("TOOL_BROKER_URL"),
    TOOL_BROKER_LEASE: get("TOOL_BROKER_LEASE"),
    SANDBOX_EGRESS_MODE: get("SANDBOX_EGRESS_MODE"),
    DLP_URL: get("DLP_URL"),
    OTEL_EXPORTER_OTLP_ENDPOINT: get("OTEL_EXPORTER_OTLP_ENDPOINT"),
  };
}
