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
import { AbortError } from "@anthropic-ai/claude-agent-sdk";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  HEARTBEAT_INTERVAL_MS,
  RedisStateClient,
  type StateTransport,
} from "../src/redis_state.js";
import { main } from "../src/main.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMsg = any;

/**
 * A StateTransport that records every token-stream entry. Used under a REAL
 * RedisStateClient so the tests assert the exact wire entries main() produces.
 */
class WireTransport implements StateTransport {
  entries: Array<Record<string, string>> = [];
  sets: Array<{ key: string; value: string; ttlSec: number }> = [];
  async get(): Promise<string | null> {
    return null;
  }
  async set(key: string, value: string, ttlSec: number): Promise<void> {
    this.sets.push({ key, value, ttlSec });
  }
  async xadd(fields: Record<string, string>): Promise<void> {
    this.entries.push({ ...fields });
  }
  async close(): Promise<void> {}
}

/** A real (coalescing, exactly-once-end) client over a WireTransport. */
function wireRedis(): { redis: RedisStateClient; wire: WireTransport } {
  const wire = new WireTransport();
  return { redis: new RedisStateClient("run", undefined, wire), wire };
}

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
      "LLM_GATEWAY_URL",
      "LLM_VIRTUAL_KEY",
      "MCP_REGISTRY_URL",
      "TOOL_BROKER_URL",
      "TOOL_BROKER_LEASE",
      "SANDBOX_EGRESS_MODE",
      "S3_SECRET_ACCESS_KEY",
      "REDIS_URL",
      "DLP_URL",
      "DLP_FAILURE_MODE",
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

  it("logs the DLP policy exactly once at startup (disabled when DLP_URL is unset)", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }) });
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
    try {
      const code = await main({
        makeRedis: () => new RedisStateClient("run", undefined),
        makeAdapter: () => new FakeAdapter() as never,
        queryFn: fakeQuerySuccess("hi back") as never,
      });
      expect(code).toBe(0);
      const dlpLines = logSpy.mock.calls
        .map((c) => String(c[0]))
        .filter((l) => l.includes("DLP"));
      expect(dlpLines).toHaveLength(1);
      expect(dlpLines[0]).toContain("DLP disabled");
    } finally {
      logSpy.mockRestore();
    }
  });

  it("logs the DLP failure mode at startup when DLP is configured", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({
      AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }),
      DLP_URL: "http://dlp:8007/api/dlp/v1",
    });
    const logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
    try {
      await main({
        makeRedis: () => new RedisStateClient("run", undefined),
        makeAdapter: () => new FakeAdapter() as never,
        queryFn: fakeQuerySuccess("hi back") as never,
      });
      const dlpLines = logSpy.mock.calls
        .map((c) => String(c[0]))
        .filter((l) => l.includes("DLP"));
      expect(dlpLines).toHaveLength(1);
      expect(dlpLines[0]).toContain("DLP_FAILURE_MODE=closed");
    } finally {
      logSpy.mockRestore();
    }
  });

  it("scrubs control-plane env before SDK query can run tools", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({
      AGENT_TASK_JSON: JSON.stringify({ task: "inspect env" }),
      LLM_GATEWAY_URL: "http://llm-gateway:8011/api/llm-gateway/v1",
      LLM_VIRTUAL_KEY: "vk-secret",
      MCP_REGISTRY_URL: "http://mcp-registry:8010/api/mcp/v1",
      TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
      TOOL_BROKER_LEASE: "v1.payload.signature",
      SANDBOX_EGRESS_MODE: "broker",
      REDIS_URL: "redis://:secret@foundation-redis:6379/0",
      S3_SECRET_ACCESS_KEY: "minio-secret",
      DLP_URL: "http://dlp:8007/api/dlp/v1",
      DLP_FAILURE_MODE: "closed",
    });

    const keys = [
      "FOUNDATION_API_TOKEN",
      "FOUNDATION_TENANT_ID",
      "FOUNDATION_USER_ID",
      "AGENT_TASK_JSON",
      "LLM_GATEWAY_URL",
      "LLM_VIRTUAL_KEY",
      "MCP_REGISTRY_URL",
      "TOOL_BROKER_URL",
      "TOOL_BROKER_LEASE",
      "SANDBOX_EGRESS_MODE",
      "REDIS_URL",
      "S3_SECRET_ACCESS_KEY",
      "DLP_URL",
      "DLP_FAILURE_MODE",
    ];
    const inspected: Record<string, string | undefined> = {};
    const queryFn = () =>
      (async function* () {
        for (const key of keys) inspected[key] = process.env[key];
        yield {
          type: "assistant",
          message: {
            content: [{ type: "text", text: "scrubbed" }],
            usage: { input_tokens: 1, output_tokens: 1 },
          },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;

    const code = await main({
      makeRedis: () => new RedisStateClient("run", undefined),
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: queryFn as never,
    });

    expect(code).toBe(0);
    expect(Object.keys(inspected)).toHaveLength(keys.length);
    for (const key of keys) {
      expect(inspected[key]).toBeUndefined();
    }
  });

  it("paused run → result.json status=paused, exit 0, neither markCompleted nor markFailed", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "search the repo", hitl: true }) });

    // A redis spy that records the terminal marks; the worker writes PAUSED state
    // itself (markPaused / writePaused), so main must NOT call mark*.
    let completed = false;
    let failed = false;
    const redis = new RedisStateClient("run", undefined);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (redis as any).markCompleted = async () => {
      completed = true;
    };
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (redis as any).markFailed = async () => {
      failed = true;
    };

    // A fake query that drives canUseTool to pause on an mcp tool call.
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        const gate = args.options.canUseTool as (
          n: string,
          i: Record<string, unknown>,
          o: { toolUseID: string },
        ) => Promise<unknown>;
        await gate("mcp__github__search_code", { q: "x" }, { toolUseID: "tu_1" });
      })() as AnyMsg;

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: queryFn as never,
    });

    expect(code).toBe(0);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("paused");
    expect(completed).toBe(false);
    expect(failed).toBe(false);
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

  // ── the token stream always ends, with a status ─────────────────────────

  it("happy path wire: delta entries, then exactly one end(completed)", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }) });
    const { redis, wire } = wireRedis();

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: fakeQuerySuccess("hi back") as never,
    });

    expect(code).toBe(0);
    expect(wire.entries).toEqual([
      { type: "delta", text: "hi back" },
      { type: "end", text: "", status: "completed" },
    ]);
  });

  it("a tool call that stays silent past the keep-alive interval leaves a heartbeat", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "run the import", hitl: false }) });
    const { redis, wire } = wireRedis();
    let announced: () => void = () => {};
    const toolCallWritten = new Promise<void>((resolve) => {
      announced = resolve;
    });
    const record = wire.xadd.bind(wire);
    wire.xadd = async (fields: Record<string, string>) => {
      await record(fields);
      if (fields.type === "tool_call") announced();
    };
    let finishTool: () => void = () => {};
    const toolDone = new Promise<void>((resolve) => {
      finishTool = resolve;
    });
    const usage = { input_tokens: 10, output_tokens: 5 };
    const queryFn = () =>
      (async function* () {
        yield {
          type: "assistant",
          message: {
            id: "m1",
            content: [{ type: "tool_use", id: "tu_1", name: "mcp__etl__run", input: {} }],
            usage,
          },
        } as AnyMsg;
        await toolDone; // the tool runs for a long time, writing nothing
        yield {
          type: "user",
          message: { content: [{ type: "tool_result", tool_use_id: "tu_1", content: "imported" }] },
        } as AnyMsg;
        yield {
          type: "assistant",
          message: { id: "m2", content: [{ type: "text", text: "Done." }], usage },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;

    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    try {
      const running = main({
        makeRedis: () => redis,
        makeAdapter: () => new FakeAdapter() as never,
        queryFn: queryFn as never,
      });
      await toolCallWritten;
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL_MS);
      finishTool();
      expect(await running).toBe(0);
    } finally {
      vi.useRealTimers();
    }

    expect(wire.entries.map((e) => e.type)).toEqual([
      "tool_call",
      "heartbeat",
      "tool_result",
      "delta",
      "end",
    ]);
    // The state blob written at the tool call was re-set by the heartbeat.
    const states = wire.sets.filter((s) => s.key === "agent:run:run:state").map((s) => s.value);
    expect(states[2]).toBe(states[1]);
    expect(JSON.parse(states[1]).status).toBe("RUNNING");
  });

  describe("early failures reach the token stream", () => {
    const cases = [
      {
        name: "missing infra env",
        env: { ...REQUIRED_ENV, FOUNDATION_TENANT_ID: "" },
        taskJson: JSON.stringify({ task: "x" }),
        code: 1,
        message: "The agent could not start (sandbox configuration error).",
      },
      {
        name: "malformed task payload",
        env: REQUIRED_ENV,
        taskJson: "{not valid json",
        code: 2,
        message: "The agent could not start (invalid task payload).",
      },
      {
        name: "empty task text",
        env: REQUIRED_ENV,
        taskJson: JSON.stringify({ task: "   " }),
        code: 0,
        message: "The task text is empty.",
      },
    ];

    for (const c of cases) {
      it(`${c.name}: error + end(failed), exit ${c.code}`, async () => {
        setEnv(c.env);
        setEnv({ AGENT_TASK_JSON: c.taskJson });
        const { redis, wire } = wireRedis();

        const code = await main({
          makeRedis: () => redis,
          makeAdapter: () => new FakeAdapter() as never,
          queryFn: fakeQuerySuccess("never") as never,
        });

        expect(code).toBe(c.code);
        expect(wire.entries).toEqual([
          { type: "error", message: c.message },
          { type: "end", text: "", status: "failed" },
        ]);
      });

      it(`${c.name}: a Redis outage while reporting never changes the exit code`, async () => {
        setEnv(c.env);
        setEnv({ AGENT_TASK_JSON: c.taskJson });
        const redis = new RedisStateClient("run", undefined);
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        (redis as any).pushError = async () => {
          throw new Error("redis down");
        };

        const code = await main({
          makeRedis: () => redis,
          makeAdapter: () => new FakeAdapter() as never,
          queryFn: fakeQuerySuccess("never") as never,
        });
        expect(code).toBe(c.code);
      });
    }

    it("a client that cannot even be built never changes the exit code", async () => {
      setEnv({ ...REQUIRED_ENV, FOUNDATION_TENANT_ID: "" });
      setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "x" }) });
      const code = await main({
        makeRedis: () => {
          throw new Error("bad REDIS_URL");
        },
        makeAdapter: () => new FakeAdapter() as never,
        queryFn: fakeQuerySuccess("never") as never,
      });
      expect(code).toBe(1);
    });
  });

  it("worker crash (adapter fails to start): user-safe error + end(failed), exit 1", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }) });
    const { redis, wire } = wireRedis();
    const crashingAdapter = {
      async start(): Promise<number> {
        throw new Error("listen EADDRINUSE 127.0.0.1:41000");
      },
      async stop(): Promise<void> {},
    };

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => crashingAdapter as never,
      queryFn: fakeQuerySuccess("never") as never,
    });

    expect(code).toBe(1);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.summary).toBe("worker crashed");
    expect(result.error).toContain("EADDRINUSE"); // detail stays in result.json
    expect(wire.entries).toEqual([
      { type: "error", message: "The agent run failed unexpectedly. Please try again." },
      { type: "end", text: "", status: "failed" },
    ]);
    // The state key ends FAILED.
    const lastState = JSON.parse(wire.sets[wire.sets.length - 1].value);
    expect(lastState.status).toBe("FAILED");
  });

  it("a failed agent run: one error + one end(failed) (main adds no second end)", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "say hi" }) });
    const { redis, wire } = wireRedis();
    const queryFn = () =>
      (async function* () {
        yield { type: "result", subtype: "error_during_execution", errors: ["boom"] } as AnyMsg;
      })() as AnyMsg;

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: queryFn as never,
    });

    expect(code).toBe(1);
    expect(wire.entries).toEqual([
      { type: "error", message: "The agent run failed unexpectedly. Please try again." },
      { type: "end", text: "", status: "failed" },
    ]);
  });

  it("cancellation (abortController): end(cancelled), result.json FAILED, exit 1", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "long job" }) });
    const { redis, wire } = wireRedis();
    const abortController = new AbortController();
    let sawAbortController = false;
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        sawAbortController = args.options.abortController === abortController;
        yield { type: "stream_event", event: { type: "message_start" } } as AnyMsg;
        yield {
          type: "stream_event",
          event: { type: "content_block_start", index: 0, content_block: { type: "text" } },
        } as AnyMsg;
        yield {
          type: "stream_event",
          event: { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Working" } },
        } as AnyMsg;
        abortController.abort(); // what the SIGTERM handler does
        throw new AbortError("Operation aborted");
      })() as AnyMsg;

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: queryFn as never,
      abortController,
    });

    expect(sawAbortController).toBe(true);
    expect(code).toBe(1);
    const result = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(result.status).toBe("FAILED");
    expect(result.summary).toBe("run cancelled");
    expect(wire.entries).toEqual([
      { type: "delta", text: "Working" },
      { type: "end", text: "", status: "cancelled" },
    ]);
  });

  it("paused run wire: pending entry, then end(paused); stream + state get the paused TTL", async () => {
    setEnv(REQUIRED_ENV);
    setEnv({ AGENT_TASK_JSON: JSON.stringify({ task: "search the repo", hitl: true }) });
    const { redis, wire } = wireRedis();
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        await args.options.canUseTool("mcp__github__search_code", { q: "x" }, { toolUseID: "tu_1" });
      })() as AnyMsg;

    const code = await main({
      makeRedis: () => redis,
      makeAdapter: () => new FakeAdapter() as never,
      queryFn: queryFn as never,
    });

    expect(code).toBe(0);
    expect(wire.entries).toEqual([
      {
        type: "pending_confirmation",
        tool_call_id: "tu_1",
        name: "mcp__github__search_code",
        args: '{"q":"x"}',
      },
      { type: "end", text: "", status: "paused" },
    ]);
    const paused = wire.sets.find((s) => s.key.endsWith(":paused"));
    expect(paused?.ttlSec).toBe(7 * 24 * 3600);
    const state = wire.sets.filter((s) => s.key.endsWith(":state")).pop();
    expect(JSON.parse(state!.value).status).toBe("PAUSED");
    expect(state!.ttlSec).toBe(7 * 24 * 3600);
  });

  // ── a resume leg is configured exactly like a fresh run ─────────────────

  it("fresh and resume legs get identical adapter + query options (only the prompt differs)", async () => {
    const baseTaskJson = {
      task: "post the weekly report",
      model_override: "claude-override",
      reasoning_effort: "high",
      sampling_params: { max_tokens: 2048 },
      sys_prompt: "You are the reporting agent.",
      memory: "The user prefers short reports.",
      history: [
        { role: "user", content: "what did we ship?" },
        { role: "assistant", content: "Three features." },
      ],
      hitl: true,
    };
    const resumeTaskJson = {
      ...baseTaskJson,
      resume: {
        kind: "confirm",
        tool_call_id: "tu_9",
        confirmed: true,
        agent_state: {
          version: 2,
          original_task: "post the weekly report",
          transcript: [{ role: "assistant", text: "Posting it now." }],
          pending: [{ id: "tu_9", name: "mcp__slack__post", args: '{"text":"report"}' }],
        },
      },
    };

    async function runLeg(taskJson: unknown) {
      // main() scrubs control-plane env before the query; re-set it per leg.
      Object.assign(process.env, REQUIRED_ENV, { AGENT_TASK_JSON: JSON.stringify(taskJson) });
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let adapterOpts: any;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      let queryArgs: any;
      const code = await main({
        makeRedis: () => new RedisStateClient("run", undefined),
        makeAdapter: (_env, _task, opts) => {
          adapterOpts = opts;
          return new FakeAdapter() as never;
        },
        queryFn: ((args: AnyMsg) => {
          queryArgs = args;
          return fakeQuerySuccess("done")();
        }) as never,
      });
      expect(code).toBe(0);
      return { adapterOpts, queryArgs };
    }

    setEnv(REQUIRED_ENV); // records the pre-test values for afterEach
    const fresh = await runLeg(baseTaskJson);
    const resumed = await runLeg(resumeTaskJson);

    expect(resumed.adapterOpts).toEqual(fresh.adapterOpts);
    expect(fresh.adapterOpts).toMatchObject({
      targetModel: "claude-override",
      maxOutputTokens: 2048,
      reasoningEffort: "high",
    });

    const pick = (o: AnyMsg) => ({
      systemPrompt: o.systemPrompt,
      model: o.model,
      maxThinkingTokens: o.maxThinkingTokens,
      disallowedTools: o.disallowedTools,
      mcpServers: Object.keys(o.mcpServers).sort(),
      permissionMode: o.permissionMode,
      includePartialMessages: o.includePartialMessages,
      hasCanUseTool: typeof o.canUseTool === "function",
    });
    expect(pick(resumed.queryArgs.options)).toEqual(pick(fresh.queryArgs.options));
    // History, memory and the agent's own system prompt reach BOTH legs.
    const sys = String(fresh.queryArgs.options.systemPrompt);
    expect(sys).toContain("You are the reporting agent.");
    expect(sys).toContain("The user prefers short reports.");
    expect(sys).toContain("Three features.");

    // The prompt is the only difference: a string vs the replayed transcript.
    expect(typeof fresh.queryArgs.prompt).toBe("string");
    expect(typeof resumed.queryArgs.prompt).not.toBe("string");
    const replayed: AnyMsg[] = [];
    for await (const m of resumed.queryArgs.prompt) replayed.push(m);
    expect(replayed).toHaveLength(1);
    const text = String(replayed[0].message.content);
    expect(text).toContain("[Original task] post the weekly report");
    expect(text).toContain("Assistant: Posting it now.");
  });
});
