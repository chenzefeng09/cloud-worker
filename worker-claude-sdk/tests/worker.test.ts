/**
 * worker.test.ts — core run loop.
 *
 * Stubs the SDK query() with a fixed async SDKMessage sequence and verifies:
 *   - result.json fields (status / summary / non-zero usage / plan_steps=[])
 *   - events.jsonl line count + event_type vocabulary
 *   - buildSystemPrompt includes the skills index + history text
 * S3 / Redis / MCP are all failure-open: unconfigured, they must not crash.
 */

import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Env, Task } from "../src/task.js";
import { RedisStateClient } from "../src/redis_state.js";
import { writeResult } from "../src/result.js";
import * as worker from "../src/worker.js";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMsg = any;

// A no-op Redis client (REDIS_URL unset => every method is a no-op, no network).
function noopRedis(): RedisStateClient {
  return new RedisStateClient("run-test", undefined);
}

/**
 * A fake RedisStateClient that records the HITL pause calls (and the token-end /
 * checkpoint writes) so the pause path can be asserted without a real Redis. Only
 * the methods the run loop touches are implemented; everything else is a no-op.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
class RecordingRedis {
  pendingConfirmations: Array<{ id: string; name: string; args: string }> = [];
  pendingQuestions: Array<{ id: string; question: string }> = [];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  pausedBlob: any = null;
  markedPaused = false;
  endPushed = false;
  /** status of every pushTokenEnd call, in order. */
  endStatuses: string[] = [];
  errors: string[] = [];
  toolCalls: Array<{ id: string; name: string; args: string }> = [];
  toolResults: Array<{ id: string; name: string; result: string; state: string }> = [];
  tokens: string[] = [];
  thinkings: string[] = [];
  thinkingDeltas: string[] = [];
  /** Every call, in order (method name only), to assert sequencing. */
  calls: string[] = [];

  async writeStep(): Promise<void> {}
  async pushToken(delta: string): Promise<void> {
    this.calls.push("pushToken");
    this.tokens.push(delta);
  }
  async pushToolCall(id: string, name: string, args: string): Promise<void> {
    this.calls.push("pushToolCall");
    this.toolCalls.push({ id, name, args });
  }
  async pushToolResult(id: string, name: string, result: string, state: string): Promise<void> {
    this.calls.push("pushToolResult");
    this.toolResults.push({ id, name, result, state });
  }
  async pushThinking(text: string): Promise<void> {
    this.calls.push("pushThinking");
    this.thinkings.push(text);
  }
  async pushThinkingDelta(text: string): Promise<void> {
    this.calls.push("pushThinkingDelta");
    this.thinkingDeltas.push(text);
  }
  async pushError(message: string): Promise<void> {
    this.calls.push("pushError");
    this.errors.push(message);
  }
  async markCompleted(): Promise<void> {}
  async markFailed(): Promise<void> {}
  async close(): Promise<void> {}

  async pushTokenEnd(status = "completed"): Promise<void> {
    this.calls.push("pushTokenEnd");
    this.endPushed = true;
    this.endStatuses.push(status);
  }
  async pushPendingConfirmation(id: string, name: string, args: string): Promise<void> {
    this.calls.push("pushPendingConfirmation");
    this.pendingConfirmations.push({ id, name, args });
  }
  async pushPendingQuestion(id: string, question: string): Promise<void> {
    this.calls.push("pushPendingQuestion");
    this.pendingQuestions.push({ id, question });
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  async writePaused(blob: any): Promise<void> {
    this.calls.push("writePaused");
    this.pausedBlob = blob;
  }
  async markPaused(): Promise<void> {
    this.calls.push("markPaused");
    this.markedPaused = true;
  }
}

/** Wrap an Anthropic raw streaming event into an SDK stream_event message. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const se = (event: any): AnyMsg => ({ type: "stream_event", event });

/** A query() stub that yields a fixed message sequence. */
function fakeQuerySeq(seq: AnyMsg[]) {
  return () =>
    (async function* () {
      for (const m of seq) yield m;
    })() as AnyMsg;
}

function baseEnv(over: Partial<Env> = {}): Env {
  return {
    FOUNDATION_API_URL: "https://foundation.local",
    FOUNDATION_API_TOKEN: "tok",
    FOUNDATION_TENANT_ID: "tnt",
    FOUNDATION_USER_ID: "usr",
    FOUNDATION_SERVICE_SCOPES: "",
    LLM_GATEWAY_URL: "",
    LLM_VIRTUAL_KEY: "",
    LLM_MODEL: "claude-test",
    S3_ENDPOINT: "",
    S3_BUCKET: "",
    S3_ACCESS_KEY_ID: "",
    S3_SECRET_ACCESS_KEY: "",
    S3_REGION: "",
    REDIS_URL: "",
    RUN_ID: "run-test",
    MCP_REGISTRY_URL: "",
    SKILL_STORE_URL: "",
    WEB_FETCH_GATEWAY_URL: "",
    TOOL_BROKER_URL: "",
    TOOL_BROKER_LEASE: "",
    SANDBOX_EGRESS_MODE: "",
    DLP_URL: "",
    DLP_FAILURE_MODE: "",
    OTEL_EXPORTER_OTLP_ENDPOINT: "",
    ...over,
  };
}

function baseTask(over: Partial<Task> = {}): Task {
  return {
    task: "do the thing",
    mcp_servers: [],
    skills: [],
    files: [],
    memory: "",
    history: [],
    sampling_params: {},
    hitl: true,
    resume: null,
    ...over,
  };
}

/** A query() stub yielding assistant(text+usage) → result(success). */
function fakeQuerySuccess(text: string, usage = { input_tokens: 120, output_tokens: 60 }) {
  return () =>
    (async function* () {
      yield {
        type: "assistant",
        message: {
          content: [{ type: "text", text }],
          usage,
        },
      } as AnyMsg;
      yield { type: "result", subtype: "success" } as AnyMsg;
    })() as AnyMsg;
}

describe("worker", () => {
  let ws: string;

  beforeEach(() => {
    ws = mkdtempSync(path.join(tmpdir(), "wworker-"));
    process.env.WORKER_WORKSPACE_DIR = ws;
  });

  afterEach(() => {
    delete process.env.WORKER_WORKSPACE_DIR;
  });

  it("runs query, writes events.jsonl and returns OK with non-zero usage", async () => {
    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: fakeQuerySuccess("hi back") },
    });

    expect(result.status).toBe("OK");
    expect(result.summary).toBe("hi back");
    expect(result.usage.input_tokens).toBe(120);
    expect(result.usage.output_tokens).toBe(60);
    expect(result.usage.total_tokens).toBe(180);
    expect(result.plan_steps).toEqual([]);

    // Persist + re-read the terminal result.json to assert its shape.
    writeResult(worker.getWorkspaceDir(), {
      status: "OK",
      summary: result.summary,
      error: null,
      usage: result.usage,
      plan_steps: result.plan_steps,
    });
    const resultJson = JSON.parse(readFileSync(path.join(ws, "result.json"), "utf8"));
    expect(resultJson.status).toBe("OK");
    expect(resultJson.usage.total_tokens).toBe(180);

    // events.jsonl: TextBlockStart + TextBlockDelta + ModelCallEnd + ReplyEnd = 4.
    const lines = readFileSync(path.join(ws, "events.jsonl"), "utf8").trim().split("\n");
    expect(lines.length).toBe(4);
    const evTypes = lines.map((l) => JSON.parse(l).event_type);
    expect(evTypes).toEqual([
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "ModelCallEndEvent",
      "ReplyEndEvent",
    ]);
  });

  it("returns FAILED when the result message is an error subtype", async () => {
    const queryFn = () =>
      (async function* () {
        yield { type: "result", subtype: "error_during_execution", errors: ["boom"] } as AnyMsg;
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn },
    });

    expect(result.status).toBe("FAILED");
    expect(result.error).toContain("boom");
  });

  it("does not crash when query() throws (failure-open → FAILED)", async () => {
    const queryFn = () => {
      throw new Error("model unreachable");
    };
    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: queryFn as never },
    });
    expect(result.status).toBe("FAILED");
    expect(result.error).toContain("model unreachable");
  });

  it("buildSystemPrompt includes memory, history text and skills index, and stays resume-free (cache-friendly)", async () => {
    const task = baseTask({
      memory: "用户上次说他叫张三",
      sys_prompt: "你是助手",
      skills: ["pdf", "xlsx"],
      history: [
        { role: "user", content: "你好" },
        { role: "assistant", content: "你好，有什么可以帮你" },
        {
          role: "tool",
          tool: { id: "t1", name: "Bash", input: { cmd: "ls" }, result: "a.txt" },
        },
      ],
    });

    const prompt = worker.buildSystemPrompt(task, baseEnv(), ["report.pdf"]);

    // The resume note must NOT appear in the system prompt: it varies per run and
    // would break DeepSeek prefix-cache hits. It now rides on the user prompt
    // (see the runQuery resume test below).
    expect(prompt).not.toContain("resuming from a checkpoint");
    expect(prompt).toContain("用户上次说他叫张三");
    expect(prompt).toContain("你是助手");
    // skills index, with the SKILL.md path
    expect(prompt).toContain("/home/skills/pdf/SKILL.md");
    expect(prompt).toContain("/home/skills/xlsx/SKILL.md");
    // history textualisation
    expect(prompt).toContain("[user]: 你好");
    expect(prompt).toContain("[tool_call:Bash]");
    expect(prompt).toContain("[tool_result:Bash]");
    // uploaded-file hint
    expect(prompt).toContain("report.pdf");
  });

  it("historyToText renders a complete history as-is, with no omission note", () => {
    const task = baseTask({
      history: [
        { role: "user", content: "hi" },
        { role: "assistant", content: "hello" },
      ],
    });
    expect(worker.historyToText(task)).toBe("[user]: hi\n[assistant]: hello");
  });

  it("historyToText says how many earlier messages the orchestrator left out", () => {
    const marker = {
      role: "system",
      type: "history_omitted",
      omitted: 5,
      content: "[5 earlier message(s) omitted: the conversation is too long to send in full]",
    } as const;
    expect(
      worker.historyToText(
        baseTask({ history: [marker, { role: "user", content: "hi" }, { role: "assistant", content: "hello" }] }),
      ),
    ).toBe("[5 earlier message(s) of this conversation are not shown]\n[user]: hi\n[assistant]: hello");
    // Everything left out: the note alone still tells the model there was more.
    expect(worker.historyToText(baseTask({ history: [marker] }))).toBe(
      "[5 earlier message(s) of this conversation are not shown]",
    );
  });

  it("historyToText counts the turns its own budget drops, a tool call together with its result", () => {
    const task = baseTask({
      history: [
        { role: "system", type: "history_omitted", omitted: 2 },
        { role: "user", content: "load the table" },
        // ~6000 tokens of call, a tiny result: dropped as one turn.
        { role: "tool", tool: { id: "t1", name: "etl", input: { sql: "y".repeat(15_000) }, result: "ok" } },
        { role: "assistant", content: "x".repeat(10_000) }, // ~4000 tokens
        { role: "user", content: "latest" },
      ],
    });
    const text = worker.historyToText(task);
    expect(text.split("\n")[0]).toBe("[4 earlier message(s) of this conversation are not shown]");
    expect(text).not.toContain("[tool_call:");
    expect(text).not.toContain("[tool_result:");
    expect(text).toContain("[user]: latest");
    expect(text).not.toContain("load the table");
  });

  it("runQuery prepends the resume note to the user prompt (not the system prompt)", async () => {
    let seenPrompt: unknown;
    const capturingQuery = (args: { prompt: unknown }) => {
      seenPrompt = args.prompt;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(baseTask({ task: "do the thing" }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      resumeNote: "[System: this run is resuming from a checkpoint; 3 step(s) were already completed.]",
      deps: { queryFn: capturingQuery as never },
    });

    // The resume note is prepended to the user task prompt, ahead of the task.
    expect(typeof seenPrompt).toBe("string");
    expect(seenPrompt as string).toContain("resuming from a checkpoint");
    expect((seenPrompt as string).indexOf("resuming from a checkpoint")).toBeLessThan(
      (seenPrompt as string).indexOf("do the thing"),
    );
  });

  it("runQuery passes the bare task as prompt when there is no resume note", async () => {
    let seenPrompt: unknown;
    const capturingQuery = (args: { prompt: unknown }) => {
      seenPrompt = args.prompt;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(baseTask({ task: "do the thing" }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn: capturingQuery as never },
    });

    expect(seenPrompt).toBe("do the thing");
  });

  it("downloadAttachments returns [] when S3 is unconfigured (failure-open)", async () => {
    const out = await worker.downloadAttachments(
      baseTask({ files: [{ key: "k", filename: "f.txt", mime: "text/plain" }] }),
      baseEnv(),
    );
    expect(out).toEqual([]);
  });

  it("loadMcpServers returns {} when registry is unconfigured (failure-open)", async () => {
    const out = await worker.loadMcpServers(baseEnv());
    expect(out.servers).toEqual({});
  });

  it("loadMcpServers parses the registry ListPage envelope (items + transport/endpoint)", async () => {
    const realFetch = global.fetch;
    // Mirror the real mcp-registry /internal/mcp-servers response shape:
    // a ListPage `{items, total}` with `transport`/`endpoint` field names.
    global.fetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          items: [
            {
              id: "mcp_1",
              tenant_id: "tnt-broker",
              name: "echo",
              transport: "http",
              endpoint: "http://echo-mcp:9000/mcp",
              command: null,
              env: {},
              scopes: [],
              enabled: true,
            },
          ],
          total: 1,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;

    try {
      const out = await worker.loadMcpServers(
        baseEnv({
          FOUNDATION_TENANT_ID: "tnt-broker",
          MCP_REGISTRY_URL: "http://tool-broker:8024/api/tool-broker/v1/mcp/v1",
        }),
      );
      expect(out.servers).toEqual({
        echo: { type: "http", url: "http://echo-mcp:9000/mcp" },
      });
    } finally {
      global.fetch = realFetch;
    }
  });

  // ── broker-egress endpoint rewriting ──────────────────────────────────────
  // Stub the registry to return exactly `items` and run loadMcpServers against
  // the given env; restores global.fetch afterward. MCP_REGISTRY_URL is forced
  // on (unless the caller overrides it) so the fetch path is actually exercised
  // — loadMcpServers short-circuits to {} when the registry base is unset.
  async function loadWithRegistryItems(
    items: unknown[],
    env: Env,
  ): Promise<Record<string, unknown>> {
    const withRegistry: Env = {
      ...env,
      MCP_REGISTRY_URL:
        env.MCP_REGISTRY_URL ||
        "http://tool-broker:8024/api/tool-broker/v1/mcp/v1",
    };
    const realFetch = global.fetch;
    global.fetch = vi.fn(async () => {
      return new Response(JSON.stringify({ items, total: items.length }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    try {
      return (await worker.loadMcpServers(withRegistry)).servers as Record<string, unknown>;
    } finally {
      global.fetch = realFetch;
    }
  }

  it("rewrites http servers to the broker passthrough + auth/tenant/lease headers in broker mode", async () => {
    const out = await loadWithRegistryItems(
      [{ id: "mcp_ABC", name: "echo", transport: "http", endpoint: "http://echo-mcp:9000/mcp" }],
      baseEnv({
        FOUNDATION_API_TOKEN: "tok-broker",
        FOUNDATION_TENANT_ID: "tnt-broker",
        MCP_REGISTRY_URL: "http://tool-broker:8024/api/tool-broker/v1/mcp/v1",
        SANDBOX_EGRESS_MODE: "broker",
        TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
        TOOL_BROKER_LEASE: "v1.body.sig",
      }),
    );
    // The broker's _require_auth gate runs BEFORE _require_lease, so the rewrite
    // must carry the foundation bearer + x-tenant-id (like the registry fetch)
    // in addition to the lease, or every tool call 401s before the lease check.
    expect(out).toEqual({
      echo: {
        type: "http",
        url: "http://tool-broker:8024/api/tool-broker/v1/mcp-tenant/v1/mcp_ABC",
        headers: {
          authorization: "Bearer tok-broker",
          "x-tenant-id": "tnt-broker",
          "x-tool-broker-lease": "v1.body.sig",
        },
      },
    });
  });

  it("trims a trailing slash on TOOL_BROKER_URL when rewriting (broker mode)", async () => {
    const out = await loadWithRegistryItems(
      [{ id: "mcp_ABC", name: "echo", transport: "sse", endpoint: "http://echo-mcp:9000/sse" }],
      baseEnv({
        FOUNDATION_API_TOKEN: "tok",
        FOUNDATION_TENANT_ID: "tnt",
        SANDBOX_EGRESS_MODE: "broker",
        TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1/",
        TOOL_BROKER_LEASE: "v1.body.sig",
      }),
    );
    expect(out).toEqual({
      echo: {
        type: "sse",
        url: "http://tool-broker:8024/api/tool-broker/v1/mcp-tenant/v1/mcp_ABC",
        headers: {
          authorization: "Bearer tok",
          "x-tenant-id": "tnt",
          "x-tool-broker-lease": "v1.body.sig",
        },
      },
    });
  });

  it("leaves http servers untouched in foundation mode (regression)", async () => {
    const out = await loadWithRegistryItems(
      [{ id: "mcp_ABC", name: "echo", transport: "http", endpoint: "http://echo-mcp:9000/mcp" }],
      baseEnv({
        SANDBOX_EGRESS_MODE: "foundation",
        TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
        TOOL_BROKER_LEASE: "v1.body.sig",
      }),
    );
    expect(out).toEqual({
      echo: { type: "http", url: "http://echo-mcp:9000/mcp" },
    });
  });

  it("drops a broker-mode http server missing a registry id (failure-open)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const out = await loadWithRegistryItems(
        [{ name: "echo", transport: "http", endpoint: "http://echo-mcp:9000/mcp" }],
        baseEnv({
          SANDBOX_EGRESS_MODE: "broker",
          TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
          TOOL_BROKER_LEASE: "v1.body.sig",
        }),
      );
      expect(out).toEqual({});
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("dropping MCP server echo"),
      );
    } finally {
      warn.mockRestore();
    }
  });

  it("drops a broker-mode http server when TOOL_BROKER_URL is unset (failure-open)", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      const out = await loadWithRegistryItems(
        [{ id: "mcp_ABC", name: "echo", transport: "http", endpoint: "http://echo-mcp:9000/mcp" }],
        baseEnv({
          SANDBOX_EGRESS_MODE: "broker",
          TOOL_BROKER_URL: "",
          TOOL_BROKER_LEASE: "v1.body.sig",
        }),
      );
      expect(out).toEqual({});
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining("dropping MCP server echo"),
      );
    } finally {
      warn.mockRestore();
    }
  });

  it("leaves stdio servers untouched in broker mode", async () => {
    const out = await loadWithRegistryItems(
      [{ id: "mcp_ABC", name: "local", transport: "stdio", command: "run-local", args: ["--x"] }],
      baseEnv({
        SANDBOX_EGRESS_MODE: "broker",
        TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
        TOOL_BROKER_LEASE: "v1.body.sig",
      }),
    );
    expect(out).toEqual({
      local: { type: "stdio", command: "run-local", args: ["--x"] },
    });
  });

  it("loadMcpServers forwards broker auth headers", async () => {
    const realFetch = global.fetch;
    let capturedUrl = "";
    let capturedHeaders: Record<string, string> = {};
    global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      capturedUrl = String(url);
      const headers = init?.headers as Record<string, string>;
      capturedHeaders = Object.fromEntries(
        Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]),
      );
      return new Response(JSON.stringify({ servers: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    try {
      const out = await worker.loadMcpServers(
        baseEnv({
          FOUNDATION_API_TOKEN: "tok-broker",
          FOUNDATION_TENANT_ID: "tnt-broker",
          FOUNDATION_USER_ID: "usr-broker",
          FOUNDATION_SERVICE_SCOPES: "mcp.read_internal",
          MCP_REGISTRY_URL: "http://tool-broker:8024/api/tool-broker/v1/mcp/v1",
          TOOL_BROKER_LEASE: "v1.payload.signature",
        }),
      );

      expect(out.servers).toEqual({});
      // The registry's internal-auth guard requires tenant_id in the query to
      // match the x-tenant-id header, and the broker forwards the query verbatim.
      expect(capturedUrl).toContain("tenant_id=tnt-broker");
      expect(capturedHeaders.authorization).toBe("Bearer tok-broker");
      expect(capturedHeaders["x-tenant-id"]).toBe("tnt-broker");
      expect(capturedHeaders["x-user-id"]).toBe("usr-broker");
      expect(capturedHeaders["x-service-scopes"]).toBe("mcp.read_internal");
      expect(capturedHeaders["x-tool-broker-lease"]).toBe("v1.payload.signature");
    } finally {
      global.fetch = realFetch;
    }
  });

  it("loadMcpServers sends the tenant_id query param matching the x-tenant-id header", async () => {
    const realFetch = global.fetch;
    let capturedUrl = "";
    let capturedHeaders: Record<string, string> = {};
    global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      capturedUrl = String(url);
      const headers = init?.headers as Record<string, string>;
      capturedHeaders = Object.fromEntries(
        Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]),
      );
      return new Response(JSON.stringify({ servers: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    try {
      // A tenant id that requires URL-encoding to verify proper encoding.
      const tenant = "tnt/abc 123";
      await worker.loadMcpServers(
        baseEnv({
          FOUNDATION_TENANT_ID: tenant,
          MCP_REGISTRY_URL: "http://mcp-registry:8000",
        }),
      );

      const parsed = new URL(capturedUrl);
      expect(parsed.pathname).toBe("/internal/mcp-servers");
      // URLSearchParams decodes back to the raw value; the guard compares decoded.
      expect(parsed.searchParams.get("tenant_id")).toBe(tenant);
      expect(parsed.searchParams.get("tenant_id")).toBe(capturedHeaders["x-tenant-id"]);
    } finally {
      global.fetch = realFetch;
    }
  });

  it("loadMcpServers omits the tenant_id query param when tenant is empty", async () => {
    const realFetch = global.fetch;
    let capturedUrl = "";
    global.fetch = vi.fn(async (url: string | URL | Request) => {
      capturedUrl = String(url);
      return new Response(JSON.stringify({ servers: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    try {
      await worker.loadMcpServers(
        baseEnv({
          FOUNDATION_TENANT_ID: "",
          MCP_REGISTRY_URL: "http://mcp-registry:8000",
        }),
      );

      expect(capturedUrl).toBe("http://mcp-registry:8000/internal/mcp-servers");
      expect(capturedUrl).not.toContain("tenant_id");
    } finally {
      global.fetch = realFetch;
    }
  });

  it("broker mode does NOT forward registry upstream auth/headers (broker injects them) but applies timeout_seconds", async () => {
    const out = await loadWithRegistryItems(
      [
        {
          id: "mcp_ABC",
          name: "echo",
          transport: "http",
          endpoint: "http://echo-mcp:9000/mcp",
          headers: { "X-Custom": "reg" },
          auth: { type: "bearer", token: "upstream-tok" },
          timeout_seconds: 12,
        },
      ],
      baseEnv({
        FOUNDATION_API_TOKEN: "tok-broker",
        FOUNDATION_TENANT_ID: "tnt-broker",
        SANDBOX_EGRESS_MODE: "broker",
        TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
        TOOL_BROKER_LEASE: "v1.body.sig",
      }),
    );
    // Only broker-auth headers survive (the broker re-injects upstream auth/
    // headers toward the real upstream from the registry record). The phantom
    // registry headers/auth MUST NOT be forwarded. timeout_seconds is attached
    // for forward-compat (the current SDK/CLI ignores the unknown `timeout`).
    expect(out).toEqual({
      echo: {
        type: "http",
        url: "http://tool-broker:8024/api/tool-broker/v1/mcp-tenant/v1/mcp_ABC",
        headers: {
          authorization: "Bearer tok-broker",
          "x-tenant-id": "tnt-broker",
          "x-tool-broker-lease": "v1.body.sig",
        },
        timeout: 12,
      },
    });
  });

  it("direct (non-broker) mode injects registry auth + headers toward the upstream", async () => {
    const out = await loadWithRegistryItems(
      [
        {
          id: "mcp_ABC",
          name: "echo",
          transport: "http",
          endpoint: "http://echo-mcp:9000/mcp",
          headers: { "X-Custom": "reg" },
          auth: { type: "api_key", api_key: "k", header_name: "X-Api-Key" },
        },
      ],
      baseEnv({ SANDBOX_EGRESS_MODE: "foundation" }),
    );
    expect(out).toEqual({
      echo: {
        type: "http",
        url: "http://echo-mcp:9000/mcp",
        headers: { "X-Custom": "reg", "X-Api-Key": "k" },
      },
    });
  });

  it("stdio server carries args + timeout_seconds", async () => {
    const out = await loadWithRegistryItems(
      [
        {
          id: "mcp_ABC",
          name: "local",
          transport: "stdio",
          command: "run-local",
          args: ["--x", "--y"],
          timeout_seconds: 30,
        },
      ],
      baseEnv({}),
    );
    expect(out).toEqual({
      local: {
        type: "stdio",
        command: "run-local",
        args: ["--x", "--y"],
        timeout: 30,
      },
    });
  });

  it("loadMcpServers returns tool_filter in the filters map", async () => {
    const realFetch = global.fetch;
    global.fetch = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          items: [
            {
              id: "mcp_ABC",
              name: "echo",
              transport: "http",
              endpoint: "http://echo-mcp:9000/mcp",
              tool_filter: { allow: ["echo"], block: ["secret"] },
            },
          ],
          total: 1,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }) as unknown as typeof fetch;
    try {
      const out = await worker.loadMcpServers(
        baseEnv({
          MCP_REGISTRY_URL: "http://mcp-registry:8000",
          FOUNDATION_TENANT_ID: "tnt",
        }),
      );
      expect(out.filters).toEqual({
        echo: { allow: ["echo"], block: ["secret"] },
      });
    } finally {
      global.fetch = realFetch;
    }
  });

  // ── run-level MCP selection (task.json mcp_server_ids) ────────────────────
  // Three-state contract: undefined = tenant default (all enabled servers),
  // [] = MCP off for this run, non-empty = only those ids. `[]` is falsy, so a
  // truthiness-based check would collapse "disable all" into "enable all".
  describe("loadMcpServers run-level selection", () => {
    /** Stub the registry with two http servers and return {fetchCalls, out}. */
    async function loadWithSelection(selectedIds?: readonly string[]) {
      const realFetch = global.fetch;
      let fetchCalls = 0;
      global.fetch = vi.fn(async () => {
        fetchCalls += 1;
        return new Response(
          JSON.stringify({
            items: [
              { id: "mcp_AAA", name: "alpha", transport: "http", endpoint: "http://alpha:9000/mcp" },
              { id: "mcp_BBB", name: "beta", transport: "http", endpoint: "http://beta:9000/mcp" },
            ],
            total: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }) as unknown as typeof fetch;
      try {
        const out = await worker.loadMcpServers(
          baseEnv({ MCP_REGISTRY_URL: "http://mcp-registry:8000", FOUNDATION_TENANT_ID: "tnt" }),
          selectedIds,
        );
        return { out, fetchCalls };
      } finally {
        global.fetch = realFetch;
      }
    }

    it("registers every tenant server when the selection is absent (regression guard)", async () => {
      const { out } = await loadWithSelection(undefined);
      expect(Object.keys(out.servers).sort()).toEqual(["alpha", "beta"]);
    });

    it("registers nothing — and skips the registry call — on an explicit []", async () => {
      const { out, fetchCalls } = await loadWithSelection([]);
      expect(out.servers).toEqual({});
      expect(out.filters).toEqual({});
      // Nothing could survive the filter, so the fetch is pure waste.
      expect(fetchCalls).toBe(0);
    });

    it("registers only the selected ids on a non-empty selection", async () => {
      const { out } = await loadWithSelection(["mcp_BBB"]);
      expect(Object.keys(out.servers)).toEqual(["beta"]);
    });

    it("registers nothing when the selected id matches no registry record", async () => {
      const { out } = await loadWithSelection(["mcp_NOPE"]);
      expect(out.servers).toEqual({});
    });

    it("selects before the transport/broker drop, not after", async () => {
      // In broker mode a server with no broker URL is DROPPED by
      // mapRegistryServer. Selecting the OTHER server must not depend on that
      // drop happening first: selection is by identity, the drop is by
      // capability, and the selected server is stdio (never rewritten).
      const realFetch = global.fetch;
      global.fetch = vi.fn(async () => {
        return new Response(
          JSON.stringify({
            items: [
              { id: "mcp_HTTP", name: "needs-broker", transport: "http", endpoint: "http://x:9000/mcp" },
              { id: "mcp_STDIO", name: "local", transport: "stdio", command: "/bin/local-mcp" },
            ],
            total: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }) as unknown as typeof fetch;
      try {
        const out = await worker.loadMcpServers(
          baseEnv({
            MCP_REGISTRY_URL: "http://mcp-registry:8000",
            FOUNDATION_TENANT_ID: "tnt",
            SANDBOX_EGRESS_MODE: "broker",
            TOOL_BROKER_URL: "",
          }),
          ["mcp_STDIO"],
        );
        expect(Object.keys(out.servers)).toEqual(["local"]);
      } finally {
        global.fetch = realFetch;
      }
    });
  });

  it("runQuery adds tool_filter block list to disallowedTools (exposure filter)", async () => {
    let captured: { disallowedTools: string[] } | undefined;
    const queryFn = (args: AnyMsg) => {
      captured = (args as AnyMsg).options as { disallowedTools: string[] };
      return (async function* () {
        yield {
          type: "assistant",
          message: {
            content: [{ type: "text", text: "done" }],
            usage: { input_tokens: 1, output_tokens: 1 },
          },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      mcpFilters: { echo: { allow: ["echo"], block: ["secret"] } },
      redis: noopRedis(),
      deps: { queryFn },
    });
    expect(captured).toBeDefined();
    // block -> exposure filter via disallowedTools.
    expect(captured!.disallowedTools).toContain("mcp__echo__secret");
    // allow is enforced by the PreToolUse gate, NOT disallowedTools.
    expect(captured!.disallowedTools).not.toContain("mcp__echo__echo");
  });

  // ── MCP timeout env (MCP_TIMEOUT / MCP_TOOL_TIMEOUT) ──────────────────────
  // A capturing queryFn that records the options object and yields a clean
  // success result. Reused across the env-aggregation tests below.
  function capturingQueryForEnv(): {
    queryFn: (args: AnyMsg) => AnyMsg;
    getOptions: () => AnyMsg;
  } {
    let seenOptions: AnyMsg;
    const queryFn = (args: AnyMsg) => {
      seenOptions = args.options;
      return (async function* () {
        yield {
          type: "assistant",
          message: {
            content: [{ type: "text", text: "ok" }],
            usage: { input_tokens: 1, output_tokens: 1 },
          },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };
    return { queryFn, getOptions: () => seenOptions };
  }

  it("runQuery sets options.env MCP_TIMEOUT/MCP_TOOL_TIMEOUT (ms) from the MIN of per-server timeouts", async () => {
    // Two registry-backed servers carrying `timeout` (seconds, attached by
    // withTimeout in mapRegistryServer). The MIN (8s) wins and propagates as ms.
    const { queryFn, getOptions } = capturingQueryForEnv();
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {
        slow: { type: "http", url: "http://slow:9000/mcp", timeout: 30 },
        fast: { type: "http", url: "http://fast:9000/mcp", timeout: 8 },
      } as never,
      redis: noopRedis(),
      deps: { queryFn },
    });

    const env = getOptions().env;
    expect(env).toBeDefined();
    // CLI parses both as parseInt(_, 10) ms; 8s -> 8000ms (not 30000).
    expect(env.MCP_TIMEOUT).toBe("8000");
    expect(env.MCP_TOOL_TIMEOUT).toBe("8000");
    // process.env is inherited (the SDK uses options.env as the CLI subprocess env).
    expect(env.WORKER_WORKSPACE_DIR).toBe(process.env.WORKER_WORKSPACE_DIR);
  });

  it("runQuery leaves options.env unset when no MCP server carries a timeout", async () => {
    // A server without `timeout` (e.g. a registry server with no timeout_seconds,
    // or the in-process webfetch/hitl). No env override -> CLI defaults apply.
    const { queryFn, getOptions } = capturingQueryForEnv();
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {
        echo: { type: "http", url: "http://echo:9000/mcp" },
      } as never,
      redis: noopRedis(),
      deps: { queryFn },
    });

    expect(getOptions().env).toBeUndefined();
  });

  it("runQuery leaves options.env unset when there are no MCP servers", async () => {
    const { queryFn, getOptions } = capturingQueryForEnv();
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn },
    });

    expect(getOptions().env).toBeUndefined();
  });

  it("runQuery MIN-aggregation ignores 0 / negative / non-number timeout values", async () => {
    // 0 and -5 are dropped by withTimeout's guard, so they never reach the config;
    // only the 20s entry is valid -> 20000ms. (Mirrors withTimeout's positive-only
    // filter, so the min sees exactly the same set the per-server config carries.)
    const { queryFn, getOptions } = capturingQueryForEnv();
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {
        a: { type: "http", url: "http://a:9000/mcp" },
        b: { type: "http", url: "http://b:9000/mcp" },
        c: { type: "http", url: "http://c:9000/mcp", timeout: 20 },
      } as never,
      redis: noopRedis(),
      deps: { queryFn },
    });

    const env = getOptions().env;
    expect(env).toBeDefined();
    expect(env.MCP_TIMEOUT).toBe("20000");
    expect(env.MCP_TOOL_TIMEOUT).toBe("20000");
  });

  it("runQuery does NOT regress round-1 oauth: a timeout-bearing server still gets env + config", async () => {
    // Regression guard for the round-1 oauth change (mapRegistryServer async +
    // token cache): the env path is orthogonal to config headers, so a server
    // mapped with a timeout should produce BOTH the `timeout` field on the
    // config AND the MCP_TIMEOUT env on the query options.
    const { queryFn, getOptions } = capturingQueryForEnv();
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {
        echo: {
          type: "http",
          url: "http://echo:9000/mcp",
          headers: { Authorization: "Bearer tok-direct" },
          timeout: 15,
        },
      } as never,
      redis: noopRedis(),
      deps: { queryFn },
    });

    // Config headers (oauth-injected in real flow) survive untouched.
    const echoCfg = getOptions().mcpServers.echo;
    expect(echoCfg.headers.Authorization).toBe("Bearer tok-direct");
    // And the env timeout is derived from the same per-server `timeout`.
    expect(getOptions().env.MCP_TIMEOUT).toBe("15000");
    expect(getOptions().env.MCP_TOOL_TIMEOUT).toBe("15000");
  });

  it("buildResumeContext returns '' when no checkpoint / terminal checkpoint", async () => {
    // noopRedis returns null from readCheckpoint => no resume.
    const note = await worker.buildResumeContext(noopRedis());
    expect(note).toBe("");
  });

  // A redis stub whose readCheckpoint returns a fixed checkpoint blob, so the
  // resume-note decision can be exercised without a real Redis.
  function checkpointRedis(
    checkpoint: Partial<import("../src/redis_state.js").CheckpointState> | null,
  ): RedisStateClient {
    const r = noopRedis();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (r as any).readCheckpoint = async () => checkpoint;
    return r;
  }

  it("buildResumeContext returns '' for the start-checkpoint of a brand-new run", async () => {
    // Regression: the run loop writes {cursor:"start", status:"RUNNING", step:1}
    // right before dispatch. That self-written marker must NOT be mistaken for a
    // prior-run resume — otherwise every fresh run gets a bogus "1 step already completed" note.
    const note = await worker.buildResumeContext(
      checkpointRedis({ status: "RUNNING", cursor: "start", step: 1, plan: "" }),
    );
    expect(note).toBe("");
    expect(note).not.toContain("resuming from a checkpoint");
  });

  it("buildResumeContext returns '' for terminal (COMPLETED / FAILED) checkpoints", async () => {
    expect(
      await worker.buildResumeContext(
        checkpointRedis({ status: "COMPLETED", cursor: "done", step: 5 }),
      ),
    ).toBe("");
    expect(
      await worker.buildResumeContext(
        checkpointRedis({ status: "FAILED", cursor: "x", step: 3 }),
      ),
    ).toBe("");
  });

  it("buildResumeContext returns the resume note for a prior run's real progress", async () => {
    // A genuine crash-resume: prior run left a non-terminal checkpoint past the
    // start marker, with real progress + a plan. The mechanism must still fire.
    const note = await worker.buildResumeContext(
      checkpointRedis({
        status: "RUNNING",
        cursor: "step-3",
        step: 3,
        plan: "调研后写报告",
      }),
    );
    expect(note).toContain("resuming from a checkpoint");
    expect(note).toContain("3 step(s) were already completed");
    expect(note).toContain("调研后写报告");
  });

  it("makeDlpPreToolUse: denies on a DLP hit and forwards the Foundation bearer", async () => {
    const realFetch = global.fetch;
    try {
      let capturedHeaders: Record<string, string> = {};
      global.fetch = vi.fn(
        async (_url: string | URL | Request, init?: RequestInit) => {
          const h = init?.headers as Record<string, string> | undefined;
          if (h) {
            for (const [k, v] of Object.entries(h)) {
              capturedHeaders[k.toLowerCase()] = v;
            }
          }
          return new Response(
            JSON.stringify({ action: "QUARANTINE", max_sensitivity: "L4", hits: [{}] }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        },
      ) as unknown as typeof fetch;

      const hook = worker.makeDlpPreToolUse(baseEnv({ DLP_URL: "http://dlp.test" }));
      const out = await hook({
        hook_event_name: "PreToolUse",
        tool_name: "Write",
        tool_input: { content: "secret" },
        tool_use_id: "tu_1",
        session_id: "s",
        transcript_path: "/tmp/t",
        cwd: "/workspace",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((out as any).hookSpecificOutput.permissionDecision).toBe("deny");
      // The hook must wire FOUNDATION_API_TOKEN through as the bearer, else the
      // tool-broker's _require_auth 401s before the lease is checked (the bug).
      expect(capturedHeaders["authorization"]).toBe("Bearer tok");
    } finally {
      global.fetch = realFetch;
    }
  });

  it("makeDlpPreToolUse: clean scan asserts NO permissionDecision (so canUseTool/HITL gate still runs)", async () => {
    const fetchSpy = vi.fn();
    const realFetch = global.fetch;
    try {
      global.fetch = fetchSpy as unknown as typeof fetch;
      const hook = worker.makeDlpPreToolUse(baseEnv({ DLP_URL: "" }));
      const out = await hook({
        hook_event_name: "PreToolUse",
        tool_name: "Read",
        tool_input: {},
        tool_use_id: "tu_2",
        session_id: "s",
        transcript_path: "/tmp/t",
        cwd: "/workspace",
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } as any);
      // A clean/fail-open scan must NOT emit `permissionDecision: "allow"` — that
      // would short-circuit the SDK permission flow and bypass the HITL
      // canUseTool gate. It returns `{continue: true}` with no decision instead.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((out as any).hookSpecificOutput).toBeUndefined();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((out as any).continue).toBe(true);
      expect(fetchSpy).not.toHaveBeenCalled();
    } finally {
      global.fetch = realFetch;
    }
  });

  /** A PreToolUse input for the DLP hook tests below. */
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const dlpHookInput = (over: Record<string, unknown> = {}): any => ({
    hook_event_name: "PreToolUse",
    tool_name: "Bash",
    tool_input: { command: "ls" },
    tool_use_id: "tu_3",
    session_id: "s",
    transcript_path: "/tmp/t",
    cwd: "/workspace",
    ...over,
  });

  /** fetch stub answering every DLP scan with the pre-fix 403. */
  const dlp403 = () =>
    vi.fn(
      async () =>
        new Response(JSON.stringify({ code: "FOUNDATION.DLP.POLICY.DLP_DENIED" }), {
          status: 403,
          headers: { "content-type": "application/json" },
        }),
    ) as unknown as typeof fetch;

  it("makeDlpPreToolUse: a scan that cannot complete is DENIED by default (fail-closed)", async () => {
    const realFetch = global.fetch;
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      global.fetch = dlp403();
      // DLP_FAILURE_MODE unset → closed.
      const hook = worker.makeDlpPreToolUse(baseEnv({ DLP_URL: "http://dlp.test" }));
      const out = await hook(dlpHookInput());
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const hso = (out as any).hookSpecificOutput;
      expect(hso.permissionDecision).toBe("deny");
      expect(hso.permissionDecisionReason).toContain("could not be completed");
      expect(hso.permissionDecisionReason).toContain("HTTP 403");
      expect(errSpy).toHaveBeenCalled();
    } finally {
      global.fetch = realFetch;
      errSpy.mockRestore();
    }
  });

  it("makeDlpPreToolUse: DLP_FAILURE_MODE=open lets an unscanned call through (no decision) and logs it", async () => {
    const realFetch = global.fetch;
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      global.fetch = dlp403();
      const hook = worker.makeDlpPreToolUse(
        baseEnv({ DLP_URL: "http://dlp.test", DLP_FAILURE_MODE: "open" }),
      );
      const out = await hook(dlpHookInput());
      // Allowed, but WITHOUT permissionDecision:"allow" (that would bypass HITL).
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((out as any).hookSpecificOutput).toBeUndefined();
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((out as any).continue).toBe(true);
      expect(errSpy).toHaveBeenCalledTimes(1);
      expect(String(errSpy.mock.calls[0][0])).toContain("UNSCANNED");
    } finally {
      global.fetch = realFetch;
      errSpy.mockRestore();
    }
  });

  it("makeDlpPreToolUse: an unexpected throw is treated as an incomplete scan, not a silent allow", async () => {
    const realFetch = global.fetch;
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const fetchSpy = vi.fn();
      global.fetch = fetchSpy as unknown as typeof fetch;
      // tool_input's getter throws while the hook builds the scan request.
      const input = dlpHookInput();
      Object.defineProperty(input, "tool_input", {
        enumerable: true,
        get() {
          throw new Error("boom");
        },
      });

      const closed = await worker.makeDlpPreToolUse(
        baseEnv({ DLP_URL: "http://dlp.test" }),
      )(input);
      const open = await worker.makeDlpPreToolUse(
        baseEnv({ DLP_URL: "http://dlp.test", DLP_FAILURE_MODE: "open" }),
      )(input);
      const disabled = await worker.makeDlpPreToolUse(baseEnv({ DLP_URL: "" }))(input);

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((closed as any).hookSpecificOutput.permissionDecision).toBe("deny");
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((open as any).continue).toBe(true);
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      expect((disabled as any).continue).toBe(true);
      expect(fetchSpy).not.toHaveBeenCalled();
      // One error line each for the closed and open cases; disabled is silent.
      expect(errSpy).toHaveBeenCalledTimes(2);
    } finally {
      global.fetch = realFetch;
      errSpy.mockRestore();
    }
  });

  // ── HITL pause path ─────────────────────────────────────────────────────────

  it("pauses on an mcp tool call: writePaused blob, markPaused, pending_confirmation, end(paused), status=paused", async () => {
    const redis = new RecordingRedis();

    // A fake query that drives canUseTool exactly as the SDK would: it presents
    // an mcp tool call to the gate; the gate denies+interrupts → the generator
    // ends without yielding a result message (deny+interrupt terminates the run).
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        const gate = args.options.canUseTool as (
          n: string,
          i: Record<string, unknown>,
          o: { toolUseID: string },
        ) => Promise<{ behavior: string }>;
        // Some assistant text streamed before the tool call.
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "let me search" }], usage: { input_tokens: 5, output_tokens: 3 } },
        } as AnyMsg;
        const decision = await gate(
          "mcp__github__search_code",
          { query: "foo" },
          { toolUseID: "tu_mcp" },
        );
        expect(decision.behavior).toBe("deny");
        // deny+interrupt ends the run; the generator simply returns.
      })() as AnyMsg;

    const result = await worker.runQuery(
      baseTask({ task: "find the repo" }),
      baseEnv(),
      { systemPrompt: "sys", mcpServers: {}, redis: redis as never, deps: { queryFn } },
    );

    expect(result.status).toBe("paused");

    // pending_confirmation pushed (one), then exactly one end(status=paused).
    expect(redis.pendingConfirmations).toEqual([
      { id: "tu_mcp", name: "mcp__github__search_code", args: '{"query":"foo"}' },
    ]);
    expect(redis.pendingQuestions).toEqual([]);
    expect(redis.endStatuses).toEqual(["paused"]);
    expect(redis.errors).toEqual([]);
    // Paused blob + PAUSED state land before the stream announces the pause.
    const order = ["writePaused", "markPaused", "pushPendingConfirmation", "pushTokenEnd"].map(
      (c) => redis.calls.indexOf(c),
    );
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(order[0]).toBeGreaterThanOrEqual(0);

    // paused blob shape.
    expect(redis.markedPaused).toBe(true);
    const blob = redis.pausedBlob!;
    expect(blob.kind).toBe("confirm");
    expect(blob.question).toBeNull();
    expect(blob.tool_calls).toEqual([
      { id: "tu_mcp", name: "mcp__github__search_code", args: '{"query":"foo"}' },
    ]);
    expect(typeof blob.paused_at).toBe("string");
    // agent_state carries our replay transcript (assistant text captured).
    const state = blob.agent_state as {
      original_task: string;
      transcript: unknown[];
      pending: unknown[];
      usage: Record<string, number>;
    };
    expect(state.original_task).toBe("find the repo");
    expect(state.pending).toHaveLength(1);
    expect(state.transcript).toContainEqual({ role: "assistant", text: "let me search" });
    // ...and the usage so far, for the resumed leg to count on from.
    expect(state.usage).toMatchObject({ input_tokens: 5, output_tokens: 3, total_tokens: 8 });
    expect(result.usage).toMatchObject({ input_tokens: 5, output_tokens: 3, total_tokens: 8 });
  });

  it("does NOT push the synthetic tool_result for the paused tool call (would wipe the pending card)", async () => {
    const redis = new RecordingRedis();

    // Mimic the REAL SDK: after canUseTool denies+interrupts, the SDK still
    // emits a `user` message carrying a tool_result block for that call
    // (is_error=true, "paused for human approval"). The worker must NOT forward
    // that result to the token stream, or the frontend acknowledges/removes the
    // pending card before it can render.
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        const gate = args.options.canUseTool as (
          n: string,
          i: Record<string, unknown>,
          o: { toolUseID: string },
        ) => Promise<{ behavior: string }>;
        await gate("mcp__echo__echo", { text: "x" }, { toolUseID: "tu_echo" });
        // The deny+interrupt result the SDK surfaces for the gated call.
        yield {
          type: "user",
          message: {
            content: [
              {
                type: "tool_result",
                tool_use_id: "tu_echo",
                is_error: true,
                content: "paused for human approval",
              },
            ],
          },
        } as AnyMsg;
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      deps: { queryFn },
    });

    expect(result.status).toBe("paused");
    expect(redis.pendingConfirmations).toEqual([
      { id: "tu_echo", name: "mcp__echo__echo", args: '{"text":"x"}' },
    ]);
    // The poisoning tool_result for the paused call must NOT have been pushed.
    expect(redis.toolResults.find((r) => r.id === "tu_echo")).toBeUndefined();
  });

  it("pauses on ask_user: kind=question, pending_question, question field set", async () => {
    const redis = new RecordingRedis();

    const queryFn = (args: AnyMsg) =>
      (async function* () {
        const gate = args.options.canUseTool as (
          n: string,
          i: Record<string, unknown>,
          o: { toolUseID: string },
        ) => Promise<{ behavior: string }>;
        await gate(
          "mcp__hitl__ask_user",
          { question: "Which environment, prod or staging?" },
          { toolUseID: "tu_ask" },
        );
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      deps: { queryFn },
    });

    expect(result.status).toBe("paused");
    expect(redis.pendingQuestions).toEqual([
      { id: "tu_ask", question: "Which environment, prod or staging?" },
    ]);
    expect(redis.pendingConfirmations).toEqual([]);
    expect(redis.endStatuses).toEqual(["paused"]);
    expect(redis.pausedBlob!.kind).toBe("question");
    expect(redis.pausedBlob!.question).toBe("Which environment, prod or staging?");
  });

  it("treats a thrown AbortError-after-pause as a pause, not a failure", async () => {
    const redis = new RecordingRedis();
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        const gate = args.options.canUseTool as (
          n: string,
          i: Record<string, unknown>,
          o: { toolUseID: string },
        ) => Promise<unknown>;
        await gate("mcp__x__y", {}, { toolUseID: "tu_z" });
        // The real SDK may throw on interrupt; simulate that here.
        throw new (await import("@anthropic-ai/claude-agent-sdk")).AbortError("aborted");
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      deps: { queryFn },
    });
    expect(result.status).toBe("paused");
    expect(redis.markedPaused).toBe(true);
    expect(redis.endStatuses).toEqual(["paused"]);
  });

  it("an AbortError with NOTHING pending is a failure, not a pause", async () => {
    const redis = new RecordingRedis();
    const queryFn = () =>
      (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "working" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        throw new (await import("@anthropic-ai/claude-agent-sdk")).AbortError("aborted");
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      deps: { queryFn },
    });
    expect(result.status).toBe("FAILED");
    expect(redis.markedPaused).toBe(false);
    expect(redis.pausedBlob).toBeNull();
    expect(redis.errors).toEqual(["The agent run failed unexpectedly. Please try again."]);
    expect(redis.endStatuses).toEqual(["failed"]);
  });

  it("an abort from outside (abortController) ends the run as cancelled", async () => {
    const redis = new RecordingRedis();
    const abortController = new AbortController();
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        expect(args.options.abortController).toBe(abortController);
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "partial" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        abortController.abort();
        throw new (await import("@anthropic-ai/claude-agent-sdk")).AbortError("aborted");
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      abortController,
      deps: { queryFn },
    });
    expect(result.status).toBe("cancelled");
    expect(redis.errors).toEqual([]);
    expect(redis.endStatuses).toEqual(["cancelled"]);
  });

  it("hitl=false keeps bypass: an mcp tool call does NOT pause", async () => {
    const redis = new RecordingRedis();
    const queryFn = (args: AnyMsg) =>
      (async function* () {
        // In bypass mode canUseTool is not attached.
        expect(args.options.canUseTool).toBeUndefined();
        expect(args.options.permissionMode).toBe("bypassPermissions");
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "done" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;

    const result = await worker.runQuery(baseTask({ hitl: false }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: redis as never,
      deps: { queryFn },
    });
    expect(result.status).toBe("OK");
    expect(redis.markedPaused).toBe(false);
    expect(redis.endStatuses).toEqual(["completed"]);
  });

  it("disallows Anthropic server-side tools (WebSearch/WebFetch) in HITL mode, keeps client-side tools", async () => {
    let seenOptions: AnyMsg;
    const queryFn = (args: AnyMsg) => {
      seenOptions = args.options;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    // baseTask() defaults to HITL (hitl !== false) → permissionMode "default".
    await worker.runQuery(baseTask(), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn },
    });

    const disallowed = seenOptions.disallowedTools as string[];
    expect(disallowed).toContain("WebSearch");
    expect(disallowed).toContain("WebFetch");
    // Must not strip client-side tools.
    expect(disallowed).not.toContain("Bash");
    expect(disallowed).not.toContain("Read");
    // Coexists with the HITL gate (default permission mode + canUseTool).
    expect(seenOptions.permissionMode).toBe("default");
    expect(typeof seenOptions.canUseTool).toBe("function");
    // Coexists with the DLP PreToolUse hook.
    expect(seenOptions.hooks?.PreToolUse).toBeDefined();
  });

  it("disallows Anthropic server-side tools (WebSearch/WebFetch) in headless mode too", async () => {
    let seenOptions: AnyMsg;
    const queryFn = (args: AnyMsg) => {
      seenOptions = args.options;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(baseTask({ hitl: false }), baseEnv(), {
      systemPrompt: "sys",
      mcpServers: {},
      redis: noopRedis(),
      deps: { queryFn },
    });

    const disallowed = seenOptions.disallowedTools as string[];
    expect(disallowed).toContain("WebSearch");
    expect(disallowed).toContain("WebFetch");
    expect(disallowed).not.toContain("Bash");
    expect(disallowed).not.toContain("Read");
    // Coexists with the headless bypass (no canUseTool gate).
    expect(seenOptions.permissionMode).toBe("bypassPermissions");
    expect(seenOptions.canUseTool).toBeUndefined();
  });

  it("resume leg uses an AsyncIterable replay prompt (not a string)", async () => {
    let seenPromptIsAsync = false;
    const queryFn = (args: AnyMsg) => {
      seenPromptIsAsync =
        typeof args.prompt === "object" &&
        typeof args.prompt[Symbol.asyncIterator] === "function";
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "continuing" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    const result = await worker.runQuery(
      baseTask({
        resume: {
          kind: "confirm",
          tool_call_id: "tu_old",
          confirmed: false,
          agent_state: { original_task: "t", transcript: [], pending: [] },
        },
      }),
      baseEnv(),
      { systemPrompt: "sys", mcpServers: {}, redis: noopRedis(), deps: { queryFn } },
    );
    expect(seenPromptIsAsync).toBe(true);
    expect(result.status).toBe("OK");
  });

  it("resume leg reports the whole run's usage, earlier legs included", async () => {
    const queryFn = () =>
      (async function* () {
        yield {
          type: "assistant",
          message: {
            content: [{ type: "text", text: "continuing" }],
            usage: { input_tokens: 40, output_tokens: 10 },
          },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;

    const result = await worker.runQuery(
      baseTask({
        resume: {
          kind: "confirm",
          tool_call_id: "tu_old",
          confirmed: true,
          agent_state: {
            original_task: "t",
            transcript: [],
            pending: [{ id: "tu_old", name: "mcp__x__y", args: "{}" }],
            usage: { input_tokens: 500, output_tokens: 200, total_tokens: 700 },
          },
        },
      }),
      baseEnv(),
      { systemPrompt: "sys", mcpServers: {}, redis: noopRedis(), deps: { queryFn } },
    );
    expect(result.status).toBe("OK");
    expect(result.usage).toMatchObject({
      input_tokens: 540,
      output_tokens: 210,
      total_tokens: 750,
    });
  });

  it("does NOT map sampling.max_tokens onto maxThinkingTokens (output-cap bug fix)", async () => {
    let seenOptions: AnyMsg;
    const queryFn = (args: AnyMsg) => {
      seenOptions = args.options;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(
      baseTask({ sampling_params: { max_tokens: 8192 } }),
      baseEnv(),
      { systemPrompt: "sys", mcpServers: {}, redis: noopRedis(), deps: { queryFn } },
    );

    // The old bug set options.maxThinkingTokens = sampling.max_tokens. With no
    // reasoning_effort on the task, thinking must stay OFF (no budget set), and
    // the output cap must NOT leak into the thinking budget.
    expect(seenOptions.maxThinkingTokens).toBeUndefined();
  });

  it("enables thinking (maxThinkingTokens>0) when the task requests reasoning_effort", async () => {
    let seenOptions: AnyMsg;
    const queryFn = (args: AnyMsg) => {
      seenOptions = args.options;
      return (async function* () {
        yield {
          type: "assistant",
          message: { content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } },
        } as AnyMsg;
        yield { type: "result", subtype: "success" } as AnyMsg;
      })() as AnyMsg;
    };

    await worker.runQuery(
      baseTask({ reasoning_effort: "high", sampling_params: { max_tokens: 4096 } }),
      baseEnv(),
      { systemPrompt: "sys", mcpServers: {}, redis: noopRedis(), deps: { queryFn } },
    );

    // Thinking is switched on via a positive budget; the output cap stays out of it.
    expect(typeof seenOptions.maxThinkingTokens).toBe("number");
    expect(seenOptions.maxThinkingTokens).toBeGreaterThan(0);
    expect(seenOptions.maxThinkingTokens).not.toBe(4096);
  });

  describe("reasoning streams live as thinking_delta (before the answer)", () => {
    // Drive the streaming (includePartialMessages) path: a thinking block opens,
    // streams thinking_delta partials, then closes; the answer follows.
    function streamThinkingThenText(
      thinkingChunks: string[],
      textChunks: string[],
    ) {
      return () =>
        (async function* () {
          yield { type: "stream_event", event: { type: "message_start" } } as AnyMsg;
          // thinking block at index 0
          yield {
            type: "stream_event",
            event: { type: "content_block_start", index: 0, content_block: { type: "thinking" } },
          } as AnyMsg;
          for (const t of thinkingChunks) {
            yield {
              type: "stream_event",
              event: { type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: t } },
            } as AnyMsg;
          }
          yield {
            type: "stream_event",
            event: { type: "content_block_stop", index: 0 },
          } as AnyMsg;
          // text block at index 1
          yield {
            type: "stream_event",
            event: { type: "content_block_start", index: 1, content_block: { type: "text" } },
          } as AnyMsg;
          for (const t of textChunks) {
            yield {
              type: "stream_event",
              event: { type: "content_block_delta", index: 1, delta: { type: "text_delta", text: t } },
            } as AnyMsg;
          }
          yield {
            type: "stream_event",
            event: { type: "content_block_stop", index: 1 },
          } as AnyMsg;
          // consolidated assistant turn carries authoritative usage
          yield {
            type: "assistant",
            message: {
              content: [
                { type: "thinking", thinking: thinkingChunks.join("") },
                { type: "text", text: textChunks.join("") },
              ],
              usage: { input_tokens: 10, output_tokens: 5 },
            },
          } as AnyMsg;
          yield { type: "result", subtype: "success" } as AnyMsg;
        })() as AnyMsg;
    }

    it("pushes each reasoning chunk as thinking_delta, all before the answer text", async () => {
      const redis = new RecordingRedis();
      const chunks = Array.from({ length: 50 }, (_, i) => `t${i} `);
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: { queryFn: streamThinkingThenText(chunks, ["hello", " world"]) },
      });

      expect(result.status).toBe("OK");
      // Live chunks (the Redis client coalesces adjacent ones on the wire).
      expect(redis.thinkingDeltas).toEqual(chunks);
      // No whole-block `thinking` entry for a streamed block — the consolidated
      // message's copy of the block is deduped.
      expect(redis.thinkings).toEqual([]);
      expect(redis.tokens).toEqual(["hello", " world"]);
      // Reasoning reaches the stream before any answer text.
      expect(redis.calls.lastIndexOf("pushThinkingDelta")).toBeLessThan(
        redis.calls.indexOf("pushToken"),
      );
    });

    it("streams an unterminated thinking block too (no wind-down buffering)", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: {
          queryFn: fakeQuerySeq([
            se({ type: "message_start" }),
            se({ type: "content_block_start", index: 0, content_block: { type: "thinking" } }),
            se({ type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "half " } }),
            se({ type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "a thought" } }),
            // NO content_block_stop for index 0 — abruptly finish.
            { type: "result", subtype: "success" },
          ]),
        },
      });

      expect(result.status).toBe("OK");
      expect(redis.thinkingDeltas).toEqual(["half ", "a thought"]);
      expect(redis.thinkings).toEqual([]);
    });

    it("pushes a NON-streamed thinking block as one whole `thinking` entry", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: {
          queryFn: fakeQuerySeq([
            {
              type: "assistant",
              message: {
                id: "msg_ns",
                content: [
                  { type: "thinking", thinking: "whole thought" },
                  { type: "text", text: "answer" },
                ],
                usage: { input_tokens: 3, output_tokens: 2 },
              },
            },
            { type: "result", subtype: "success" },
          ]),
        },
      });
      expect(result.status).toBe("OK");
      expect(redis.thinkings).toEqual(["whole thought"]);
      expect(redis.thinkingDeltas).toEqual([]);
      expect(redis.tokens).toEqual(["answer"]);
    });
  });

  // ── duplicate tool calls (real CLI 0.1.77 message shape) ───────────────────
  // The CLI yields one consolidated `assistant` per content block, at content
  // index 0 and BEFORE that block's content_block_stop; the adapter closes
  // blocks in order. The worker must push ONE tool_call per id with the
  // complete args (never the streamed JSON twice).
  describe("tool calls with the real per-block consolidated shape", () => {
    const MSG = "msg_turn1";
    const consolidated = (block: AnyMsg, usage = { input_tokens: 100, output_tokens: 20 }) => ({
      type: "assistant",
      message: { id: MSG, content: [block], usage },
    });

    function realShapeSeq(): AnyMsg[] {
      return [
        se({ type: "message_start", message: { id: MSG } }),
        se({ type: "content_block_start", index: 0, content_block: { type: "thinking", thinking: "" } }),
        se({ type: "content_block_delta", index: 0, delta: { type: "thinking_delta", thinking: "let me think" } }),
        consolidated({ type: "thinking", thinking: "let me think" }),
        se({ type: "content_block_stop", index: 0 }),
        se({ type: "content_block_start", index: 1, content_block: { type: "text", text: "" } }),
        se({ type: "content_block_delta", index: 1, delta: { type: "text_delta", text: "I'll list files." } }),
        consolidated({ type: "text", text: "I'll list files." }),
        se({ type: "content_block_stop", index: 1 }),
        se({ type: "content_block_start", index: 2, content_block: { type: "tool_use", id: "toolu_1", name: "Bash", input: {} } }),
        se({ type: "content_block_delta", index: 2, delta: { type: "input_json_delta", partial_json: '{"command":' } }),
        se({ type: "content_block_delta", index: 2, delta: { type: "input_json_delta", partial_json: '"ls -la"}' } }),
        consolidated({ type: "tool_use", id: "toolu_1", name: "Bash", input: { command: "ls -la" } }),
        se({ type: "content_block_stop", index: 2 }),
        se({ type: "message_delta", delta: { stop_reason: "tool_use" }, usage: { output_tokens: 20 } }),
        se({ type: "message_stop" }),
        { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "toolu_1", content: "file.txt" }] } },
        se({ type: "message_start", message: { id: "msg_turn2" } }),
        se({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }),
        se({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Found file.txt." } }),
        {
          type: "assistant",
          message: {
            id: "msg_turn2",
            content: [{ type: "text", text: "Found file.txt." }],
            usage: { input_tokens: 130, output_tokens: 5 },
          },
        },
        se({ type: "content_block_stop", index: 0 }),
        { type: "result", subtype: "success" },
      ];
    }

    it("pushes one tool_call with the complete args, once", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask({ hitl: false }), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: { queryFn: fakeQuerySeq(realShapeSeq()) },
      });

      expect(result.status).toBe("OK");
      expect(redis.toolCalls).toEqual([
        { id: "toolu_1", name: "Bash", args: '{"command":"ls -la"}' },
      ]);
      expect(redis.toolResults).toEqual([
        { id: "toolu_1", name: "Bash", result: "file.txt", state: "success" },
      ]);
      expect(redis.tokens).toEqual(["I'll list files.", "Found file.txt."]);
      expect(redis.thinkingDeltas).toEqual(["let me think"]);
      expect(redis.endStatuses).toEqual(["completed"]);

      // events.jsonl carries exactly one ToolCallStart/End for the call.
      const evs = readFileSync(path.join(ws, "events.jsonl"), "utf8")
        .trim()
        .split("\n")
        .map((l) => JSON.parse(l));
      expect(evs.filter((e) => e.event_type === "ToolCallStartEvent")).toHaveLength(1);
      expect(evs.filter((e) => e.event_type === "ToolCallEndEvent")).toHaveLength(1);
      // Usage: turn 1 counted once (not per consolidated), plus turn 2.
      expect(result.usage.input_tokens).toBe(230);
      expect(result.usage.output_tokens).toBe(25);
    });

    it("joins text blocks of different turns with a paragraph break in the summary", async () => {
      const result = await worker.runQuery(baseTask({ hitl: false }), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: new RecordingRedis() as never,
        deps: { queryFn: fakeQuerySeq(realShapeSeq()) },
      });
      expect(result.summary).toBe("I'll list files.\n\nFound file.txt.");
    });

    it("a paused run's snapshot keeps text and tools interleaved", async () => {
      const redis = new RecordingRedis();
      const seq = realShapeSeq().slice(0, 17); // turn 1 incl. the Bash result
      const queryFn = (args: AnyMsg) =>
        (async function* () {
          for (const m of seq) yield m;
          yield se({ type: "message_start", message: { id: "msg_turn2" } });
          yield se({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } });
          yield se({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Posting it." } });
          await args.options.canUseTool("mcp__slack__post", { text: "file.txt" }, { toolUseID: "toolu_2" });
        })() as AnyMsg;

      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: { queryFn },
      });
      expect(result.status).toBe("paused");
      expect(redis.pausedBlob.agent_state.transcript).toEqual([
        { role: "assistant", text: "I'll list files." },
        { role: "tool", id: "toolu_1", name: "Bash", args: '{"command":"ls -la"}', result: "file.txt" },
        { role: "assistant", text: "Posting it." },
      ]);
    });
  });

  // ── failures reach the token stream ────────────────────────────────────────
  describe("failure paths push error + end(failed)", () => {
    it("a failed result pushes a user-safe error, then end(failed)", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: {
          queryFn: fakeQuerySeq([
            { type: "result", subtype: "error_max_turns", errors: ["max turns at /home/x"] },
          ]),
        },
      });
      expect(result.status).toBe("FAILED");
      expect(redis.errors).toEqual(["The agent stopped after reaching its maximum number of steps."]);
      expect(redis.endStatuses).toEqual(["failed"]);
      expect(redis.calls.indexOf("pushError")).toBeLessThan(redis.calls.indexOf("pushTokenEnd"));
    });

    it("a thrown query never leaks the raw error text to the stream", async () => {
      const redis = new RecordingRedis();
      const queryFn = () => {
        throw new Error("connect ECONNREFUSED 10.0.0.7:8011 /srv/secret/path");
      };
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: { queryFn: queryFn as never },
      });
      expect(result.status).toBe("FAILED");
      expect(result.error).toContain("ECONNREFUSED"); // result.json keeps the detail
      expect(redis.errors).toEqual(["The agent run failed unexpectedly. Please try again."]);
      expect(redis.endStatuses).toEqual(["failed"]);
    });

    it("an API error surfaced as an assistant message is never streamed as answer text", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: {
          queryFn: fakeQuerySeq([
            {
              type: "assistant",
              error: "rate_limit",
              message: {
                id: "msg_err",
                content: [{ type: "text", text: 'API Error: gateway 429: {"detail":"tenant t-1 over quota"}' }],
                usage: { input_tokens: 0, output_tokens: 0 },
              },
            },
            { type: "result", subtype: "success", is_error: true, result: "API Error: gateway 429" },
          ]),
        },
      });
      expect(result.status).toBe("FAILED");
      expect(redis.tokens).toEqual([]);
      expect(redis.errors).toEqual([
        "The model service is rate-limiting requests. Please try again in a moment.",
      ]);
      expect(redis.endStatuses).toEqual(["failed"]);
    });

    it("a code-less CLI notice the run recovers from (output cap) is dropped; the run completes", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: {
          queryFn: fakeQuerySeq([
            {
              type: "assistant",
              message: { id: "msg_1", content: [{ type: "text", text: "Part one." }], usage: { input_tokens: 9, output_tokens: 9 } },
            },
            {
              type: "assistant",
              message: {
                id: "syn_1",
                model: "<synthetic>",
                content: [{ type: "text", text: "API Error: Claude's response exceeded the 32000 output token maximum." }],
                usage: { input_tokens: 0, output_tokens: 0 },
              },
            },
            {
              type: "assistant",
              message: { id: "msg_2", content: [{ type: "text", text: "Part two." }], usage: { input_tokens: 9, output_tokens: 9 } },
            },
            { type: "result", subtype: "success" },
          ]),
        },
      });
      expect(result.status).toBe("OK");
      expect(redis.tokens).toEqual(["Part one.", "Part two."]);
      expect(result.summary).toBe("Part one.\n\nPart two.");
      expect(redis.errors).toEqual([]);
      expect(redis.endStatuses).toEqual(["completed"]);
    });

    it("a success result pushes end(completed) and no error", async () => {
      const redis = new RecordingRedis();
      const result = await worker.runQuery(baseTask(), baseEnv(), {
        systemPrompt: "sys",
        mcpServers: {},
        redis: redis as never,
        deps: { queryFn: fakeQuerySuccess("hi") },
      });
      expect(result.status).toBe("OK");
      expect(redis.errors).toEqual([]);
      expect(redis.endStatuses).toEqual(["completed"]);
    });
  });

  describe("userSafeErrorMessage", () => {
    it("maps SDK error codes and result subtypes to fixed user-safe text", () => {
      expect(worker.userSafeErrorMessage({ apiError: "authentication_failed" })).toMatch(/administrator/);
      expect(worker.userSafeErrorMessage({ apiError: "server_error" })).toMatch(/temporarily unavailable/);
      expect(worker.userSafeErrorMessage({ resultSubtype: "error_max_budget_usd" })).toMatch(/budget/);
      expect(worker.userSafeErrorMessage({})).toBe("The agent run failed unexpectedly. Please try again.");
    });
  });

  describe("mapReasoningEffort", () => {
    it("passes high/max through and floors low/medium to high", () => {
      expect(worker.mapReasoningEffort("high")).toBe("high");
      expect(worker.mapReasoningEffort("max")).toBe("max");
      expect(worker.mapReasoningEffort("low")).toBe("high");
      expect(worker.mapReasoningEffort("medium")).toBe("high");
      expect(worker.mapReasoningEffort("HIGH")).toBe("high");
    });
    it("returns undefined for unset / unknown values", () => {
      expect(worker.mapReasoningEffort(undefined)).toBeUndefined();
      expect(worker.mapReasoningEffort("")).toBeUndefined();
      expect(worker.mapReasoningEffort("turbo")).toBeUndefined();
    });
  });

  describe("extractStructuredOutputSchema", () => {
    it("returns the schema verbatim when present", () => {
      const schema = { type: "object", properties: { a: { type: "number" } } };
      expect(
        worker.extractStructuredOutputSchema({ response_format: { type: "json_schema", schema } }),
      ).toEqual(schema);
    });
    it("returns undefined for bare json_object / no schema / no response_format", () => {
      expect(worker.extractStructuredOutputSchema({ response_format: { type: "json_object" } })).toBeUndefined();
      expect(worker.extractStructuredOutputSchema({})).toBeUndefined();
      expect(worker.extractStructuredOutputSchema(undefined)).toBeUndefined();
    });
  });

  // ── OAuth client-credentials (direct-mode MCP servers) ─────────────────────
  describe("oauth client-credentials", () => {
    const TOKEN_URL = "https://idp.example/oauth/token";
    const OAUTH_AUTH = {
      type: "oauth",
      client_id: "cid",
      client_secret: "csec",
      token_url: TOKEN_URL,
      scopes: ["a", "b"],
    };

    /** Stub global.fetch with a per-URL handler map (url -> () => Response).
     * Lookup strips the query string so the registry fetch (which carries
     * ``?tenant_id=``) matches the bare handler key. A handler may return a
     * Promise to simulate token-endpoint latency (single-flight test). */
    function stubFetch(
      handlers: Record<string, (init?: RequestInit) => Response | Promise<Response>>,
    ): { calls: Record<string, RequestInit[]>; restore: () => void } {
      const realFetch = global.fetch;
      const calls: Record<string, RequestInit[]> = {};
      global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
        const u = String(url);
        const key = u.split("?")[0];
        calls[key] = calls[key] ?? [];
        calls[key].push(init ?? ({} as RequestInit));
        const handler = handlers[key];
        if (!handler) {
          return new Response("not found", { status: 404 });
        }
        return await handler(init);
      }) as unknown as typeof fetch;
      return { calls, restore: () => { global.fetch = realFetch; } };
    }

    it("fetchOauthToken posts client_credentials form with space-joined scopes", async () => {
      const { calls, restore } = stubFetch({
        [TOKEN_URL]: () =>
          new Response(JSON.stringify({ access_token: "tok", expires_in: 7200 }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
      });
      try {
        const out = await worker.fetchOauthToken(OAUTH_AUTH);
        expect(out.access_token).toBe("tok");
        const init = calls[TOKEN_URL][0];
        expect(init.method).toBe("POST");
        const body = String(init.body);
        expect(body).toContain("grant_type=client_credentials");
        expect(body).toContain("client_id=cid");
        expect(body).toContain("client_secret=csec");
        // scopes are space-joined; URLSearchParams encodes the space as "+".
        expect(body).toContain("scope=a+b");
        const headers = init.headers as Record<string, string>;
        expect(headers["content-type"]).toBe("application/x-www-form-urlencoded");
        expect(out.expires_at).toBeGreaterThan(Date.now() + 7000);
      } finally {
        restore();
      }
    });

    it("fetchOauthToken throws when the token endpoint returns non-200", async () => {
      const { restore } = stubFetch({
        [TOKEN_URL]: () => new Response("denied", { status: 500 }),
      });
      try {
        await expect(worker.fetchOauthToken(OAUTH_AUTH)).rejects.toThrow(
          /HTTP 500/,
        );
      } finally {
        restore();
      }
    });

    it("fetchOauthToken throws when access_token is missing", async () => {
      const { restore } = stubFetch({
        [TOKEN_URL]: () =>
          new Response(JSON.stringify({ error: "denied" }), { status: 200 }),
      });
      try {
        await expect(worker.fetchOauthToken(OAUTH_AUTH)).rejects.toThrow(
          /no access_token/,
        );
      } finally {
        restore();
      }
    });

    it("getOauthToken caches a valid token (no refetch on second call)", async () => {
      worker._resetOauthTokenCacheForTest();
      const { calls, restore } = stubFetch({
        [TOKEN_URL]: () =>
          new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), {
            status: 200,
          }),
      });
      try {
        const a = await worker.getOauthToken("srv_cache", OAUTH_AUTH);
        const b = await worker.getOauthToken("srv_cache", OAUTH_AUTH);
        expect(a).toBe("tok");
        expect(b).toBe("tok");
        expect(calls[TOKEN_URL]).toHaveLength(1);
      } finally {
        restore();
        worker._resetOauthTokenCacheForTest();
      }
    });

    it("getOauthToken refreshes after expiry", async () => {
      worker._resetOauthTokenCacheForTest();
      const { calls, restore } = stubFetch({
        [TOKEN_URL]: () =>
          // expires_in=0 -> immediately past the refresh margin.
          new Response(JSON.stringify({ access_token: "tok", expires_in: 0 }), {
            status: 200,
          }),
      });
      try {
        await worker.getOauthToken("srv_refresh", OAUTH_AUTH);
        await worker.getOauthToken("srv_refresh", OAUTH_AUTH);
        expect(calls[TOKEN_URL]).toHaveLength(2);
      } finally {
        restore();
        worker._resetOauthTokenCacheForTest();
      }
    });

    it("getOauthToken single-flight: concurrent calls share ONE fetch", async () => {
      worker._resetOauthTokenCacheForTest();
      const { calls, restore } = stubFetch({
        [TOKEN_URL]: () =>
          // Delay the response so concurrent callers arrive while the first is
          // still in flight.
          new Promise<Response>((resolve) => {
            setTimeout(
              () =>
                resolve(
                  new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), {
                    status: 200,
                  }),
                ),
              30,
            );
          }),
      });
      try {
        const results = await Promise.all([
          worker.getOauthToken("srv_sf", OAUTH_AUTH),
          worker.getOauthToken("srv_sf", OAUTH_AUTH),
          worker.getOauthToken("srv_sf", OAUTH_AUTH),
        ]);
        expect(results).toEqual(["tok", "tok", "tok"]);
        expect(calls[TOKEN_URL]).toHaveLength(1);
      } finally {
        restore();
        worker._resetOauthTokenCacheForTest();
      }
    });

    it("direct mode fetches an oauth token and injects Bearer into config headers", async () => {
      worker._resetOauthTokenCacheForTest();
      const registryUrl = "http://mcp-registry:8000";
      const { calls, restore } = stubFetch({
        [`${registryUrl}/internal/mcp-servers`]: () =>
          new Response(
            JSON.stringify({
              items: [
                {
                  id: "mcp_OAUTH",
                  name: "echo",
                  transport: "http",
                  endpoint: "http://echo-mcp:9000/mcp",
                  auth: OAUTH_AUTH,
                },
              ],
              total: 1,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        [TOKEN_URL]: () =>
          new Response(JSON.stringify({ access_token: "tok-direct", expires_in: 3600 }), {
            status: 200,
          }),
      });
      try {
        const out = await worker.loadMcpServers(
          baseEnv({
            FOUNDATION_TENANT_ID: "tnt",
            MCP_REGISTRY_URL: registryUrl,
            SANDBOX_EGRESS_MODE: "foundation",
          }),
        );
        expect(out.servers).toEqual({
          echo: {
            type: "http",
            url: "http://echo-mcp:9000/mcp",
            headers: { Authorization: "Bearer tok-direct" },
          },
        });
        // The token endpoint was hit exactly once.
        expect(calls[TOKEN_URL]).toHaveLength(1);
      } finally {
        restore();
        worker._resetOauthTokenCacheForTest();
      }
    });

    it("direct mode oauth fetch failure is fail-open (no Authorization header)", async () => {
      worker._resetOauthTokenCacheForTest();
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const registryUrl = "http://mcp-registry:8000";
      const { restore } = stubFetch({
        [`${registryUrl}/internal/mcp-servers`]: () =>
          new Response(
            JSON.stringify({
              items: [
                {
                  id: "mcp_OAUTH2",
                  name: "echo",
                  transport: "http",
                  endpoint: "http://echo-mcp:9000/mcp",
                  auth: OAUTH_AUTH,
                },
              ],
              total: 1,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        [TOKEN_URL]: () => new Response("denied", { status: 500 }),
      });
      try {
        const out = await worker.loadMcpServers(
          baseEnv({
            FOUNDATION_TENANT_ID: "tnt",
            MCP_REGISTRY_URL: registryUrl,
            SANDBOX_EGRESS_MODE: "foundation",
          }),
        );
        // Fail-open: the server is still mapped, just without an Authorization header.
        expect(out.servers).toEqual({
          echo: { type: "http", url: "http://echo-mcp:9000/mcp" },
        });
        expect(warn).toHaveBeenCalledWith(
          expect.stringContaining("oauth token fetch failed"),
        );
      } finally {
        restore();
        warn.mockRestore();
        worker._resetOauthTokenCacheForTest();
      }
    });

    it("broker mode does NOT fetch an oauth token (broker injects it)", async () => {
      worker._resetOauthTokenCacheForTest();
      const registryUrl = "http://tool-broker:8024/api/tool-broker/v1/mcp/v1";
      const { calls, restore } = stubFetch({
        [`${registryUrl}/internal/mcp-servers`]: () =>
          new Response(
            JSON.stringify({
              items: [
                {
                  id: "mcp_OAUTH3",
                  name: "echo",
                  transport: "http",
                  endpoint: "http://echo-mcp:9000/mcp",
                  auth: OAUTH_AUTH,
                },
              ],
              total: 1,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
      });
      try {
        const out = await worker.loadMcpServers(
          baseEnv({
            FOUNDATION_API_TOKEN: "tok-broker",
            FOUNDATION_TENANT_ID: "tnt-broker",
            MCP_REGISTRY_URL: registryUrl,
            SANDBOX_EGRESS_MODE: "broker",
            TOOL_BROKER_URL: "http://tool-broker:8024/api/tool-broker/v1",
            TOOL_BROKER_LEASE: "v1.body.sig",
          }),
        );
        // Broker mode rewrites to the passthrough + broker-auth headers; the
        // oauth token is NOT fetched here (the broker does it from the registry
        // record). Only the registry fetch happened - no token-endpoint call.
        expect(out.servers).toEqual({
          echo: {
            type: "http",
            url: "http://tool-broker:8024/api/tool-broker/v1/mcp-tenant/v1/mcp_OAUTH3",
            headers: {
              authorization: "Bearer tok-broker",
              "x-tenant-id": "tnt-broker",
              "x-tool-broker-lease": "v1.body.sig",
            },
          },
        });
        expect(calls[TOKEN_URL]).toBeUndefined();
      } finally {
        restore();
        worker._resetOauthTokenCacheForTest();
      }
    });
  });
});
