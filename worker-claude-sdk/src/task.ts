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

/** Sampling overrides; all optional, passed through to the model when set. */
export interface SamplingParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
}

/** The full task payload the worker executes. */
export interface Task {
  task: string;
  mcp_servers: unknown[];
  skills: string[];
  files: FileSpec[];
  memory: string;
  history: HistoryItem[];
  agent_name?: string;
  sys_prompt?: string;
  model_override?: string;
  reasoning_effort?: string;
  sampling_params: SamplingParams;
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
  TOOL_BROKER_LEASE: string;
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
    TOOL_BROKER_LEASE: get("TOOL_BROKER_LEASE"),
    OTEL_EXPORTER_OTLP_ENDPOINT: get("OTEL_EXPORTER_OTLP_ENDPOINT"),
  };
}
