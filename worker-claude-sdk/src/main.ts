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
