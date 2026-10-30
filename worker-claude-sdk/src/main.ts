/**
 * main.ts — the Claude SDK runtime worker coordinator.
 *
 * Exit-code contract (what the code below actually returns):
 *   - missing required infra env        → result.json(FAILED)  → exit 1
 *   - missing / malformed task          → result.json(FAILED)  → exit 2
 *   - empty task text                   → result.json(FAILED)  → exit 0
 *   - run completed OK                  → result.json(OK)      → exit 0
 *   - run paused for HITL               → result.json(paused)  → exit 0
 *   - run FAILED (model / agent failure) → result.json(FAILED) → exit 1
 *   - run cancelled (SIGTERM / SIGINT)  → result.json(FAILED)  → exit 1
 *   - worker raised / crashed           → result.json(FAILED)  → exit 1
 *
 * Token stream: every path above that has a RUN_ID ends the run's Redis token
 * stream with exactly one `end` entry carrying the terminal status
 * (completed | failed | paused | cancelled); a failure pushes a short,
 * user-safe `error` entry first. These pushes are best-effort — a Redis outage
 * never masks the original failure or changes the exit code.
 *
 * It also owns lifecycle: start the in-process gateway adapter, point the SDK at
 * it via ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN, drive worker.runQuery, then
 * always stop the adapter and close Redis on the way out.
 */

import { existsSync, readFileSync, unlinkSync } from "node:fs";
import path from "node:path";

import type { query } from "@anthropic-ai/claude-agent-sdk";

import {
  readEnv,
  readTask,
  validateRequiredEnv,
  type Env,
  type Task,
} from "./task.js";
import { logDlpPolicy, resolveDlpPolicy } from "./dlp.js";
import { resumedUsage } from "./hitl.js";
import { writeResult } from "./result.js";
import { fromEnv as redisFromEnv, type RedisStateClient } from "./redis_state.js";
import { GatewayAdapter, type GatewayAdapterOptions } from "./adapter.js";
import {
  buildResumeContext,
  buildSystemPrompt,
  downloadAttachments,
  extractStructuredOutputSchema,
  loadMcpServers,
  mapReasoningEffort,
  runQuery,
  getWorkspaceDir,
  userSafeErrorMessage,
  type RunResult,
} from "./worker.js";

/** Any non-empty token: the adapter ignores its content (F.1). */
const OPAQUE_AUTH_TOKEN = "foundation-runtime-opaque";

const EMPTY_USAGE = { input_tokens: 0, output_tokens: 0, total_tokens: 0 };

const TOOL_ENV_SCRUB_KEYS = [
  "FOUNDATION_API_URL",
  "FOUNDATION_API_TOKEN",
  "FOUNDATION_TENANT_ID",
  "FOUNDATION_USER_ID",
  "FOUNDATION_SERVICE_SCOPES",
  "LLM_GATEWAY_URL",
  "LLM_VIRTUAL_KEY",
  "LLM_MODEL",
  "S3_ENDPOINT",
  "S3_ENDPOINT_URL",
  "S3_BUCKET",
  "S3_ACCESS_KEY_ID",
  "S3_SECRET_ACCESS_KEY",
  "S3_REGION",
  "REDIS_URL",
  "RUN_ID",
  "MCP_REGISTRY_URL",
  "SKILL_STORE_URL",
  "WEB_FETCH_GATEWAY_URL",
  "TOOL_BROKER_URL",
  "TOOL_BROKER_LEASE",
  "SANDBOX_EGRESS_MODE",
  "DLP_URL",
  "DLP_FAILURE_MODE",
  "OTEL_EXPORTER_OTLP_ENDPOINT",
  "AGENT_TASK_JSON",
] as const;

/**
 * Secret keys the scheduler may deliver via the on-disk `<workspace>/.secrets.json`
 * file instead of container env. The scheduler stopped passing these as container
 * env to close a leak: a sandboxed agent could `cat /proc/1/environ` to recover
 * secrets that scrubProcessEnvForTools() (which only mutates process.env, never
 * the kernel environ) cannot remove. Only these keys are trusted from the file;
 * any other key present is ignored.
 */
export const SECRET_FILE_KEYS = [
  "FOUNDATION_API_TOKEN",
  "TOOL_BROKER_LEASE",
  "LLM_VIRTUAL_KEY",
  "S3_ACCESS_KEY_ID",
  "S3_SECRET_ACCESS_KEY",
  "REDIS_URL",
] as const;

/**
 * Load secrets the scheduler dropped at `<workspace>/.secrets.json` into
 * process.env, then unlink the file. Must run BEFORE validateRequiredEnv() /
 * readEnv() because FOUNDATION_API_TOKEN (a required gate var) now arrives this
 * way. Loading into process.env (not just the Env snapshot) is required because
 * redis_state.fromEnv() reads process.env.REDIS_URL directly.
 *
 * The file is unlinked unconditionally — even on malformed JSON — so the secret
 * bytes never survive on the writable /workspace mount. Values are never logged.
 */
export function loadSecretFileIntoEnv(): void {
  const secretsPath = path.join(getWorkspaceDir(), ".secrets.json");
  // Runtime / tests may not provide the file; that's not an error.
  if (!existsSync(secretsPath)) return;

  try {
    let loaded = 0;
    try {
      const raw = readFileSync(secretsPath, "utf8");
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      if (typeof parsed === "object" && parsed !== null) {
        for (const key of SECRET_FILE_KEYS) {
          if (Object.prototype.hasOwnProperty.call(parsed, key)) {
            const value = parsed[key];
            if (value != null) {
              process.env[key] = String(value);
              loaded += 1;
            }
          }
        }
      } else {
        console.warn(
          "[main] .secrets.json did not contain a JSON object; ignoring",
        );
      }
      console.log(`[main] loaded ${loaded} secret(s) from .secrets.json`);
    } catch (err) {
      // Never log values. validateRequiredEnv() fails naturally afterward if
      // FOUNDATION_API_TOKEN was supposed to arrive via this file.
      console.warn(
        `[main] failed to parse .secrets.json: ${(err as Error).message}`,
      );
    }
  } finally {
    // The secret bytes must not survive on the writable mount, even on failure.
    try {
      unlinkSync(secretsPath);
    } catch {
      // best-effort; file may already be gone
    }
  }
}

function scrubProcessEnvForTools(): void {
  let removed = 0;
  for (const key of TOOL_ENV_SCRUB_KEYS) {
    if (process.env[key] !== undefined) {
      delete process.env[key];
      removed += 1;
    }
  }
  if (removed > 0) {
    console.log(
      `[main] scrubbed ${removed} control-plane env vars before tool execution`,
    );
  }
}

/** Injectable dependencies so main() is unit-testable without the real SDK. */
export interface MainDeps {
  /** Injectable SDK query() so main() runs without the real model in tests. */
  queryFn?: typeof query;
  /** Build the Redis client (defaults to redis_state.fromEnv()). */
  makeRedis?: () => RedisStateClient;
  /**
   * Build the gateway adapter (defaults to a real GatewayAdapter). `opts` are
   * the options main() derived from env + task (see buildAdapterOptions).
   */
  makeAdapter?: (env: Env, task: Task, opts: GatewayAdapterOptions) => GatewayAdapter;
  /**
   * Aborting it cancels the run (the direct entrypoint wires SIGTERM / SIGINT
   * to it): the SDK query stops and the run ends as cancelled.
   */
  abortController?: AbortController;
}

/** User-safe stream messages for failures before the agent loop starts. */
const EARLY_FAILURE_MESSAGES = {
  env: "The agent could not start (sandbox configuration error).",
  task: "The agent could not start (invalid task payload).",
  emptyTask: "The task text is empty.",
} as const;

/**
 * The gateway adapter options for a task. A resume leg derives them from its
 * (re-sent) task payload exactly like a fresh run: model, output cap,
 * reasoning effort and structured-output schema.
 */
export function buildAdapterOptions(env: Env, task: Task): GatewayAdapterOptions {
  return {
    gatewayUrl: env.LLM_GATEWAY_URL || undefined,
    targetModel: task.model_override || env.LLM_MODEL,
    foundationToken: env.FOUNDATION_API_TOKEN,
    tenantId: env.FOUNDATION_TENANT_ID,
    virtualKey: env.LLM_VIRTUAL_KEY,
    userId: env.FOUNDATION_USER_ID,
    brokerLease: env.TOOL_BROKER_LEASE,
    // Request-level features the Claude SDK Options surface cannot express,
    // so they are carried into the adapter snapshot (see adapter.ts):
    //   - max_tokens (model OUTPUT cap) from sampling_params,
    //   - reasoning_effort (clamped to DeepSeek's high/max),
    //   - structured-output JSON schema (constrained decoding).
    maxOutputTokens: task.sampling_params?.max_tokens,
    reasoningEffort: mapReasoningEffort(task.reasoning_effort),
    structuredOutputSchema: extractStructuredOutputSchema(task.sampling_params),
    port: 0,
  };
}

/**
 * Best-effort: end the token stream as failed — a user-safe `error` entry,
 * then `end(status="failed")`. Never throws, so a Redis/broker outage cannot
 * mask the original failure or change the exit code.
 */
async function pushFailureToStream(
  redis: RedisStateClient,
  message: string,
): Promise<void> {
  try {
    await redis.pushError(message);
    await redis.pushTokenEnd("failed");
  } catch (err) {
    console.warn(
      `[main] could not report the failure to the token stream: ${(err as Error).message}`,
    );
  }
}

/** {@link pushFailureToStream} for a failure before the run's client exists. */
async function reportEarlyFailure(deps: MainDeps, message: string): Promise<void> {
  let redis: RedisStateClient | null = null;
  try {
    redis = deps.makeRedis ? deps.makeRedis() : redisFromEnv();
    await pushFailureToStream(redis, message);
  } catch (err) {
    console.warn(
      `[main] could not report the failure to the token stream: ${(err as Error).message}`,
    );
  } finally {
    try {
      await redis?.close();
    } catch {
      // best-effort
    }
  }
}

export async function main(deps: MainDeps = {}): Promise<number> {
  // ── secret delivery via file → load into process.env, then unlink ───────
  // Must run before validateRequiredEnv(): FOUNDATION_API_TOKEN (a required gate
  // var) now arrives via <workspace>/.secrets.json rather than container env.
  loadSecretFileIntoEnv();

  // ── infra env gate → exit 1 ─────────────────────────────────────────────
  const missing = validateRequiredEnv();
  if (missing.length > 0) {
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "infra env missing",
      error: `required env var ${missing.join(", ")} is unset`,
      usage: EMPTY_USAGE,
      plan_steps: [],
    });
    await reportEarlyFailure(deps, EARLY_FAILURE_MESSAGES.env);
    return 1;
  }

  // ── task delivery → exit 2 on missing/malformed ─────────────────────────
  let task: Task;
  try {
    task = readTask();
  } catch (err) {
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "invalid task.json",
      error: (err as Error).message,
      usage: EMPTY_USAGE,
      plan_steps: [],
    });
    await reportEarlyFailure(deps, EARLY_FAILURE_MESSAGES.task);
    return 2;
  }

  const env = readEnv();
  // Say once, at startup, whether tool calls are DLP-scanned and what happens
  // when a scan cannot complete (the hook itself never logs "disabled").
  logDlpPolicy(resolveDlpPolicy(env));

  // Empty task text: completed run with FAILED status (exit 0), matching Python.
  if (!task.task || task.task.trim() === "") {
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "task text is empty",
      error: null,
      usage: EMPTY_USAGE,
      plan_steps: [],
    });
    await reportEarlyFailure(deps, EARLY_FAILURE_MESSAGES.emptyTask);
    return 0;
  }

  const redis = deps.makeRedis ? deps.makeRedis() : redisFromEnv();
  const adapterOptions = buildAdapterOptions(env, task);
  const adapter = deps.makeAdapter
    ? deps.makeAdapter(env, task, adapterOptions)
    : new GatewayAdapter(adapterOptions);

  try {
    // Point the SDK at the in-process adapter BEFORE query() runs.
    const port = await adapter.start();
    process.env.ANTHROPIC_BASE_URL = `http://localhost:${port}`;
    process.env.ANTHROPIC_AUTH_TOKEN = OPAQUE_AUTH_TOKEN;

    // Build the resume note BEFORE the first checkpoint write so it reads any
    // checkpoint left behind by a *prior* run on this run-id — never the
    // start-checkpoint this run is about to write below (which would otherwise
    // be misread as "resumed, 1 step done" on every brand-new run).
    const resumeNote = await buildResumeContext(redis);

    // Mark the run RUNNING before dispatch (first checkpoint write).
    await redis.writeStep({ cursor: "start", status: "RUNNING" });
    // From here until the run ends, a quiet stretch (a long tool call) must not
    // let the run's Redis keys expire under it.
    redis.startHeartbeat();
    const downloaded = await downloadAttachments(task, env);
    // task.mcp_server_ids is three-state: undefined => tenant default (every
    // enabled server, the historical behaviour), [] => MCP off for this run,
    // non-empty => only those ids. Forwarded as-is so loadMcpServers can tell
    // "absent" from "empty" — collapsing them here would break the contract.
    const { servers: mcpServers, filters: mcpFilters } = await loadMcpServers(
      env,
      task.mcp_server_ids,
    );
    // resumeNote is passed to runQuery (prepended to the user prompt) rather
    // than into the system prompt, so the static system prefix stays stable
    // across runs and remains hittable by DeepSeek's automatic prefix cache.
    const systemPrompt = buildSystemPrompt(task, env, downloaded);
    scrubProcessEnvForTools();

    const result: RunResult = await runQuery(task, env, {
      systemPrompt,
      mcpServers,
      mcpFilters,
      redis,
      resumeNote,
      abortController: deps.abortController,
      deps: deps.queryFn ? { queryFn: deps.queryFn } : undefined,
    });

    writeResult(getWorkspaceDir(), {
      // result.json has no "cancelled" status; the scheduler sees FAILED.
      status: result.status === "cancelled" ? "FAILED" : result.status,
      summary: result.status === "cancelled" ? "run cancelled" : result.summary,
      error: result.error ?? null,
      usage: result.usage,
      plan_steps: result.plan_steps,
    });

    if (result.status === "OK") {
      await redis.markCompleted();
      return 0;
    }
    if (result.status === "paused") {
      // HITL pause: the worker already wrote the PAUSED state + paused blob to
      // Redis (worker.handlePause). result.json carries status="paused". The run
      // is neither completed nor failed — the orchestrator re-dispatches a fresh
      // sandbox with a resume payload. Exit 0 so the scheduler maps it to PAUSED.
      return 0;
    }
    // A FAILED (or cancelled) run from the agent loop → exit 1. runQuery has
    // already pushed the stream's error/end entries.
    await redis.markFailed();
    return 1;
  } catch (err) {
    // Last-resort safety net: any unexpected throw → FAILED + exit 1.  The
    // usage is at least what earlier legs used; writeResult keeps this leg's
    // checkpointed counts on top of that.
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "worker crashed",
      error: `${(err as Error).name}: ${(err as Error).message}`,
      usage: resumedUsage(task),
      plan_steps: [],
    });
    // A no-op when runQuery already ended the stream (the client pushes `end`
    // only once).
    await pushFailureToStream(redis, userSafeErrorMessage({}));
    try {
      await redis.markFailed();
    } catch {
      // best-effort
    }
    return 1;
  } finally {
    // Always release resources regardless of the path taken above.
    try {
      await adapter.stop();
    } catch {
      // best-effort
    }
    try {
      await redis.close();
    } catch {
      // best-effort
    }
  }
}

// Direct execution entrypoint (node dist/main.js). Skipped under test import.
// import.meta.url vs process.argv[1] check keeps main() importable in vitest.
const isDirectRun =
  typeof process !== "undefined" &&
  process.argv[1] != null &&
  import.meta.url === `file://${process.argv[1]}`;

if (isDirectRun) {
  // Cancellation: `docker stop` / a VM teardown sends SIGTERM (node runs as
  // PID 1, which ignores it without a handler). Abort the run instead so it
  // ends the token stream with end(status="cancelled") before exiting.
  const abortController = new AbortController();
  const onSignal = (signal: NodeJS.Signals): void => {
    if (abortController.signal.aborted) return;
    console.log(`[main] received ${signal}; cancelling the run`);
    abortController.abort();
  };
  process.once("SIGTERM", onSignal);
  process.once("SIGINT", onSignal);

  main({ abortController })
    .then((code) => {
      process.exit(code);
    })
    .catch((err) => {
      console.error(`[main] fatal: ${(err as Error).message}`);
      process.exit(1);
    });
}
