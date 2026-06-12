/**
 * main.test.ts — coordinator exit-code contract.
 *
 *   - missing required infra env → exit 1 + result.json(FAILED)
 *   - malformed task.json        → exit 2 + result.json(FAILED)
 *   - happy path (stub query + mock adapter + no-op redis) → exit 0 + result.json(OK)
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { RedisStateClient } from "../src/redis_state.js";
import { main } from "../src/main.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMsg = any;

const REQUIRED_ENV = {
  FOUNDATION_API_URL: "https://foundation.local",
  FOUNDATION_API_TOKEN: "tok-123",
  FOUNDATION_TENANT_ID: "tnt-abc",
  FOUNDATION_USER_ID: "usr-xyz",
  LLM_MODEL: "claude-test",
};

/** A fake GatewayAdapter that binds no socket and reports a fixed port. */
class FakeAdapter {
  async start(): Promise<number> {
    return 12345;
  }
  async stop(): Promise<void> {
    /* no-op */
  }
  get port(): number {
    return 12345;
  }
}

function fakeQuerySuccess(text: string) {
  return () =>
    (async function* () {
      yield {
        type: "assistant",
        message: {
          content: [{ type: "text", text }],
          usage: { input_tokens: 90, output_tokens: 30 },
        },
      } as AnyMsg;
      yield { type: "result", subtype: "success" } as AnyMsg;
    })() as AnyMsg;
}

describe("main", () => {
  let ws: string;
  const saved: Record<string, string | undefined> = {};

  function setEnv(o: Record<string, string>): void {
    for (const [k, v] of Object.entries(o)) {
      saved[k] = process.env[k];
      process.env[k] = v;
    }
  }

  beforeEach(() => {
    ws = mkdtempSync(path.join(tmpdir(), "wmain-"));
    process.env.WORKER_WORKSPACE_DIR = ws;
    // Start each test from a clean infra-env + task slate.
    for (const k of [
      "FOUNDATION_API_URL",
      "FOUNDATION_API_TOKEN",
      "FOUNDATION_TENANT_ID",
      "FOUNDATION_USER_ID",
      "AGENT_TASK_JSON",
      "REDIS_URL",
    ]) {
      saved[k] = process.env[k];
      delete process.env[k];
    }
  });

  afterEach(() => {
    delete process.env.WORKER_WORKSPACE_DIR;
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  });

  it("exits 1 and writes FAILED when a required env var is missing", async () => {
    setEnv(REQUIRED_ENV);
    delete process.env.FOUNDATION_TENANT_ID; // drop one required var
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "x" }) });

    const code = await main({
      makeRedis: () => new RedisStateClient("run", undefined),
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: fakeQuerySuccess("never") as never,
    });

    expect(code).toBe(1);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("FAILED");
    expect(result.summary).toBe("infra env missing");
    expect(result.error).toContain("FOUNDATION_TENANT_ID");
  });

  it("exits 2 and writes FAILED on malformed task.json file", async () => {
    setEnv(REQUIRED_ENV);
    // No AGENT_TASK_JSON; place a malformed file at <ws>/task.json. readTask()
    // reads /workspace/task.json, so point the worker (not readTask) here — but
    // readTask uses a hard-coded path. Instead drive the malformed case via env.
    setEnv({ AGENT_TASK_JSON: "{not valid json" });

    const code = await main({
      makeRedis: () => new RedisStateClient("run", undefined),
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: fakeQuerySuccess("never") as never,
    });

    expect(code).toBe(2);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("FAILED");
    expect(result.summary).toBe("invalid task.json");
  });

  it("happy path: exit 0, result.json OK with summary + usage", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }) });

    const code = await main({
      makeRedis: () => new RedisStateClient("run", undefined),
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: fakeQuerySuccess("hi back") as never,
    });

    expect(code).toBe(0);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("OK");
    expect(result.summary).toBe("hi back");
    expect(result.usage.input_tokens).toBe(90);
    expect(result.usage.total_tokens).toBe(120);
  });

  it("empty task text → exit 0 with FAILED status (matches Python)", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "   " }) });

    const code = await main({
      makeRedis: () => new RedisStateClient("run", undefined),
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: fakeQuerySuccess("never") as never,
    });

    expect(code).toBe(0);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("FAILED");
    expect(result.summary).toContain("empty");
  });
});
