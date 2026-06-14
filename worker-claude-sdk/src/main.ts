/**
 * main.ts — the Claude SDK runtime worker coordinator.
 *
 * Mirrors the AgentScope runtime_worker `main()` exit-code contract:
 *   - missing required infra env  → result.json(FAILED) → exit 1
 *   - missing / malformed task    → result.json(FAILED) → exit 2
 *   - run completed (OK or FAILED status inside result.json) → exit 0
 *   - worker raised / crashed     → result.json(FAILED) → exit 1
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
import { writeResult } from "./result.js";
import { fromEnv as redisFromEnv, type RedisStateClient } from "./redis_state.js";
import { GatewayAdapter } from "./adapter.js";
import {
  buildResumeContext,
  buildSystemPrompt,
  downloadAttachments,
  loadMcpServers,
  runQuery,
  getWorkspaceDir,
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
  "LLM_API_KEY",
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
  /** Build the gateway adapter (defaults to a real GatewayAdapter). */
  makeAdapter?: (env: Env, task: Task) => GatewayAdapter;
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
    return 2;
  }

  const env = readEnv();

  // Empty task text: completed run with FAILED status (exit 0), matching Python.
  if (!task.task || task.task.trim() === "") {
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "task text is empty",
      error: null,
      usage: EMPTY_USAGE,
      plan_steps: [],
    });
    return 0;
  }

  const redis = deps.makeRedis ? deps.makeRedis() : redisFromEnv();
  const adapter = deps.makeAdapter
    ? deps.makeAdapter(env, task)
    : new GatewayAdapter({
        gatewayUrl: env.LLM_GATEWAY_URL || undefined,
        targetModel: task.model_override || env.LLM_MODEL,
        foundationToken: env.FOUNDATION_API_TOKEN,
        tenantId: env.FOUNDATION_TENANT_ID,
        virtualKey: env.LLM_VIRTUAL_KEY,
        userId: env.FOUNDATION_USER_ID,
        brokerLease: env.TOOL_BROKER_LEASE,
        port: 0,
      });

  try {
    // Point the SDK at the in-process adapter BEFORE query() runs.
    const port = await adapter.start();
    process.env.ANTHROPIC_BASE_URL = `http://localhost:${port}`;
    process.env.ANTHROPIC_AUTH_TOKEN = OPAQUE_AUTH_TOKEN;

    // Mark the run RUNNING before dispatch (first checkpoint write).
    await redis.writeStep({ cursor: "start", status: "RUNNING" });

    const resumeNote = await buildResumeContext(redis);
    const downloaded = await downloadAttachments(task, env);
    const mcpServers = await loadMcpServers(env);
    // resumeNote is passed to runQuery (prepended to the user prompt) rather
    // than into the system prompt, so the static system prefix stays stable
    // across runs and remains hittable by DeepSeek's automatic prefix cache.
    const systemPrompt = buildSystemPrompt(task, env, downloaded);
    scrubProcessEnvForTools();

    const result: RunResult = await runQuery(task, env, {
      systemPrompt,
      mcpServers,
      redis,
      resumeNote,
      deps: deps.queryFn ? { queryFn: deps.queryFn } : undefined,
    });

    writeResult(getWorkspaceDir(), {
      status: result.status,
      summary: result.summary,
      error: result.error ?? null,
      usage: result.usage,
      plan_steps: result.plan_steps,
    });

    if (result.status === "OK") {
      await redis.markCompleted();
      return 0;
    }
    // A FAILED run status from the agent loop is an infra/model failure → exit 1.
    await redis.markFailed();
    return 1;
  } catch (err) {
    // Last-resort safety net: any unexpected throw → FAILED + exit 1.
    writeResult(getWorkspaceDir(), {
      status: "FAILED",
      summary: "worker crashed",
      error: `${(err as Error).name}: ${(err as Error).message}`,
      usage: EMPTY_USAGE,
      plan_steps: [],
    });
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
  main()
    .then((code) => {
      process.exit(code);
    })
    .catch((err) => {
      console.error(`[main] fatal: ${(err as Error).message}`);
      process.exit(1);
    });
}
