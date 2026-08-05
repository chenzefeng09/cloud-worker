/**
 * redis_state.test.ts — RedisStateClient contract tests with a mocked ioredis.
 *
 * The mock records xadd/set/get/expire calls so we can assert the exact stream
 * entry fields (aligned with the Python redis_state.py), checkpoint parsing,
 * and failure-open behaviour (a throwing client must not throw out of a method).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ── ioredis mock ─────────────────────────────────────────────────────────────
// One shared instance per test so we can inspect what was written.
interface XaddCall {
  key: string;
  args: unknown[];
}

class FakeRedis {
  static last: FakeRedis | null = null;
  xaddCalls: XaddCall[] = [];
  setCalls: Array<{ key: string; value: string }> = [];
  /** EX (seconds) recorded per set call, in order — used to assert TTLs. */
  setExArgs: Array<number | undefined> = [];
  expireCalls: Array<{ key: string; ttl: number }> = [];
  getValue: string | null = null;
  throwOnXadd = false;
  throwOnGet = false;

  constructor(_url: string, _opts?: unknown) {
    FakeRedis.last = this;
  }

  on(): this {
    return this;
  }

  async get(_key: string): Promise<string | null> {
    if (this.throwOnGet) throw new Error("boom-get");
    return this.getValue;
  }

  async set(key: string, value: string, ...rest: unknown[]): Promise<"OK"> {
    this.setCalls.push({ key, value });
    // ioredis SET signature: set(key, value, "EX", ttl). Capture the EX seconds.
    const exIdx = rest.findIndex((r) => r === "EX");
    this.setExArgs.push(exIdx >= 0 ? (rest[exIdx + 1] as number) : undefined);
    return "OK";
  }

  async expire(key: string, ttl: number): Promise<number> {
    this.expireCalls.push({ key, ttl });
    return 1;
  }

  async xadd(key: string, ...args: unknown[]): Promise<string> {
    if (this.throwOnXadd) throw new Error("boom-xadd");
    this.xaddCalls.push({ key, args });
    return "1-0";
  }

  async quit(): Promise<"OK"> {
    return "OK";
  }
}

vi.mock("ioredis", () => ({ Redis: FakeRedis, default: FakeRedis }));

// Import AFTER the mock is registered.
const { RedisStateClient } = await import("../src/redis_state.js");

/** Pull the trailing field key/value pairs out of a recorded xadd call. */
function streamFields(call: XaddCall): Record<string, string> {
  // args layout: "MAXLEN", "~", <maxlen>, "*", k, v, k, v, ...
  const starIdx = call.args.indexOf("*");
  const pairs = call.args.slice(starIdx + 1) as string[];
  const out: Record<string, string> = {};
  for (let i = 0; i < pairs.length; i += 2) out[pairs[i]] = pairs[i + 1];
  return out;
}

beforeEach(() => {
  FakeRedis.last = null;
});

describe("token stream entry fields", () => {
  it("pushToken / pushTokenEnd write delta then end entries", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("hi back");
    await c.pushTokenEnd();

    const calls = FakeRedis.last!.xaddCalls;
    expect(calls).toHaveLength(2);
    expect(calls[0].key).toBe("agent:run:run_x:tokens");
    expect(streamFields(calls[0])).toEqual({ type: "delta", text: "hi back" });
    expect(streamFields(calls[1])).toEqual({ type: "end", text: "" });
  });

  it("pushToolCall writes type=tool_call with id/name/args", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToolCall("tc-1", "Bash", '{"cmd":"pwd"}');

    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "tool_call",
      tool_call_id: "tc-1",
      name: "Bash",
      args: '{"cmd":"pwd"}',
    });
  });

  it("pushToolResult writes type=tool_result with id/name/result/state", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToolResult("tc-1", "Bash", "/workspace", "success");

    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "tool_result",
      tool_call_id: "tc-1",
      name: "Bash",
      result: "/workspace",
      state: "success",
    });
  });

  it("pushThinking writes type=thinking", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushThinking("hmm");
    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({ type: "thinking", text: "hmm" });
  });

  it("sets the stream TTL exactly once on first push", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("a");
    await c.pushToken("b");
    expect(FakeRedis.last!.expireCalls).toEqual([
      { key: "agent:run:run_x:tokens", ttl: 1800 },
    ]);
  });

  it("truncates oversized tool results", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    const big = "x".repeat(9000);
    await c.pushToolResult("tc-1", "Read", big, "success");
    const fields = streamFields(FakeRedis.last!.xaddCalls[0]);
    expect(fields.result.endsWith("…[truncated]")).toBe(true);
    expect(fields.result.length).toBeLessThan(big.length);
  });
});

describe("HITL pause (ioredis)", () => {
  const PAUSED_TTL = 7 * 24 * 3600;

  it("pushPendingConfirmation writes type=pending_confirmation with id/name/args", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushPendingConfirmation("tc-1", "mcp__github__search_code", '{"q":"x"}');
    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "pending_confirmation",
      tool_call_id: "tc-1",
      name: "mcp__github__search_code",
      args: '{"q":"x"}',
    });
  });

  it("pushPendingQuestion writes type=pending_question with id/question", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushPendingQuestion("tc-q", "Which env?");
    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "pending_question",
      tool_call_id: "tc-q",
      question: "Which env?",
    });
  });

  it("does NOT push a type=end token on pause (pending pushes only)", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushPendingConfirmation("tc-1", "mcp__x__y", "{}");
    const types = FakeRedis.last!.xaddCalls.map((x) => streamFields(x).type);
    expect(types).not.toContain("end");
  });

  it("writePaused sets the :paused key with the 7-day TTL", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    const blob = {
      kind: "confirm" as const,
      reply_id: "",
      tool_calls: [{ id: "tc-1", name: "mcp__x__y", args: "{}" }],
      question: null,
      agent_state: { transcript: [] },
      paused_at: "2026-06-15T00:00:00.000Z",
    };
    await c.writePaused(blob);

    const set = FakeRedis.last!.setCalls.at(-1)!;
    expect(set.key).toBe("agent:run:run_x:paused");
    expect(JSON.parse(set.value)).toEqual(blob);
    // 7-day TTL is passed to the ioredis SET ... EX argument list.
    const exArg = FakeRedis.last!.setExArgs.at(-1);
    expect(exArg).toBe(PAUSED_TTL);
  });

  it("markPaused sets status=PAUSED with the 7-day TTL, preserving fields", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.getValue = JSON.stringify({
      run_id: "run_x",
      step: 4,
      plan: "p",
      cursor: "c",
      last_event_seq: 4,
      status: "RUNNING",
      updated_step: 4,
    });
    await c.markPaused();
    const set = FakeRedis.last!.setCalls.at(-1)!;
    expect(set.key).toBe("agent:run:run_x:state");
    const written = JSON.parse(set.value);
    expect(written.status).toBe("PAUSED");
    expect(written.step).toBe(4);
    expect(FakeRedis.last!.setExArgs.at(-1)).toBe(PAUSED_TTL);
  });
});

describe("checkpoint state", () => {
  it("writeStep persists the aligned state blob", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.writeStep({ plan: "do x", cursor: "TextBlockDeltaEvent", status: "RUNNING" });

    const set = FakeRedis.last!.setCalls[0];
    expect(set.key).toBe("agent:run:run_x:state");
    expect(JSON.parse(set.value)).toEqual({
      run_id: "run_x",
      step: 1,
      plan: "do x",
      cursor: "TextBlockDeltaEvent",
      last_event_seq: 1,
      status: "RUNNING",
      updated_step: 1,
    });
  });

  it("readCheckpoint parses the state blob", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.getValue = JSON.stringify({
      run_id: "run_x",
      step: 3,
      plan: "p",
      cursor: "c",
      last_event_seq: 3,
      status: "RUNNING",
      updated_step: 3,
    });
    const cp = await c.readCheckpoint();
    expect(cp).toMatchObject({ run_id: "run_x", step: 3, status: "RUNNING" });
  });

  it("markCompleted / markFailed update status, preserving prior fields", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.getValue = JSON.stringify({
      run_id: "run_x",
      step: 5,
      plan: "p",
      cursor: "c",
      last_event_seq: 5,
      status: "RUNNING",
      updated_step: 5,
    });
    await c.markCompleted();
    const written = JSON.parse(FakeRedis.last!.setCalls.at(-1)!.value);
    expect(written.status).toBe("COMPLETED");
    expect(written.step).toBe(5);
  });
});

describe("failure-open", () => {
  it("does not throw when xadd throws", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.throwOnXadd = true;
    await expect(c.pushToken("x")).resolves.toBeUndefined();
  });

  it("readCheckpoint returns null when get throws", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.throwOnGet = true;
    await expect(c.readCheckpoint()).resolves.toBeNull();
  });

  it("is a no-op (no client constructed) when redisUrl is empty", async () => {
    const c = new RedisStateClient("run_x", undefined);
    expect(FakeRedis.last).toBeNull();
    await expect(c.pushToken("x")).resolves.toBeUndefined();
    await expect(c.readCheckpoint()).resolves.toBeNull();
  });
});

// ── broker egress mode (TOOL_BROKER_URL set, REDIS_URL blank) ────────────────
// In production the scheduler blanks REDIS_URL and injects TOOL_BROKER_URL +
// TOOL_BROKER_LEASE; hot-state then routes through the tool-broker /state/v1
// façade over HTTP (global fetch) instead of ioredis. fromEnv() builds the
// broker transport; we mock fetch and assert the exact requests it issues.

const { fromEnv } = await import("../src/redis_state.js");

interface FetchCall {
  url: string;
  init: RequestInit;
}

/** Install a fetch stub that records calls and returns `responder(url)`. */
function stubFetch(
  responder: (url: string, init: RequestInit) => Response | Promise<Response>,
): FetchCall[] {
  const calls: FetchCall[] = [];
  vi.stubGlobal(
    "fetch",
    async (input: unknown, init?: RequestInit): Promise<Response> => {
      const url = input instanceof URL ? input.toString() : String(input);
      calls.push({ url, init: init ?? {} });
      return responder(url, init ?? {});
    },
  );
  return calls;
}

const BROKER_ENV = {
  RUN_ID: "run_b",
  REDIS_URL: "",
  TOOL_BROKER_URL: "http://tool-broker:8080/",
  TOOL_BROKER_LEASE: "lease-abc",
  FOUNDATION_TENANT_ID: "ten-1",
  FOUNDATION_USER_ID: "usr-1",
  FOUNDATION_API_TOKEN: "tok-xyz",
};

describe("broker egress transport", () => {
  beforeEach(() => {
    for (const [k, v] of Object.entries(BROKER_ENV)) {
      vi.stubEnv(k, v);
    }
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("does not construct an ioredis client in broker mode", async () => {
    stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    await c.pushToken("hi");
    expect(FakeRedis.last).toBeNull();
  });

  it("pushToken POSTs /state/v1/xadd with the right body and headers", async () => {
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    await c.pushToken("hi back");

    expect(calls).toHaveLength(1);
    const call = calls[0];
    expect(call.url).toBe("http://tool-broker:8080/state/v1/xadd");
    expect(call.init.method).toBe("POST");
    const headers = call.init.headers as Record<string, string>;
    expect(headers["Authorization"]).toBe("Bearer tok-xyz");
    expect(headers["X-Tenant-Id"]).toBe("ten-1");
    expect(headers["X-User-Id"]).toBe("usr-1");
    expect(headers["X-Tool-Broker-Lease"]).toBe("lease-abc");
    const body = JSON.parse(call.init.body as string);
    expect(body).toEqual({
      run_id: "run_b",
      fields: { type: "delta", text: "hi back" },
      maxlen: 10000,
      expire: 1800,
    });
    // No key is sent — the broker reconstructs the token stream key from run_id.
    expect(body).not.toHaveProperty("key");
  });

  it("writeStep POSTs /state/v1/set with kind=state and the aligned blob", async () => {
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    await c.writeStep({ plan: "do x", cursor: "TextBlockDeltaEvent" });

    expect(calls).toHaveLength(1);
    const call = calls[0];
    expect(call.url).toBe("http://tool-broker:8080/state/v1/set");
    expect(call.init.method).toBe("POST");
    const body = JSON.parse(call.init.body as string);
    expect(body.run_id).toBe("run_b");
    expect(body.kind).toBe("state");
    expect(body.ttl).toBe(1800);
    expect(JSON.parse(body.value)).toEqual({
      run_id: "run_b",
      step: 1,
      plan: "do x",
      cursor: "TextBlockDeltaEvent",
      last_event_seq: 1,
      status: "RUNNING",
      updated_step: 1,
    });
  });

  it("readCheckpoint GETs /state/v1/get?kind=state and parses {value}", async () => {
    const blob = JSON.stringify({
      run_id: "run_b",
      step: 4,
      plan: "p",
      cursor: "c",
      last_event_seq: 4,
      status: "RUNNING",
      updated_step: 4,
    });
    const calls = stubFetch(() => Response.json({ value: blob }));
    const c = fromEnv();
    const cp = await c.readCheckpoint();

    expect(calls).toHaveLength(1);
    const call = calls[0];
    const url = new URL(call.url);
    expect(url.pathname).toBe("/state/v1/get");
    expect(url.searchParams.get("run_id")).toBe("run_b");
    expect(url.searchParams.get("kind")).toBe("state");
    expect(call.init.method).toBe("GET");
    expect(cp).toMatchObject({ run_id: "run_b", step: 4, status: "RUNNING" });
  });

  it("readCheckpoint returns null when the broker reports value=null", async () => {
    stubFetch(() => Response.json({ value: null }));
    const c = fromEnv();
    await expect(c.readCheckpoint()).resolves.toBeNull();
  });

  it("markCompleted reads then writes status=COMPLETED preserving fields", async () => {
    const prior = JSON.stringify({
      run_id: "run_b",
      step: 5,
      plan: "p",
      cursor: "c",
      last_event_seq: 5,
      status: "RUNNING",
      updated_step: 5,
    });
    const calls = stubFetch((url) =>
      new URL(url).pathname === "/state/v1/get"
        ? Response.json({ value: prior })
        : Response.json({ ok: true }),
    );
    const c = fromEnv();
    await c.markCompleted();

    // One GET (read prior) then one POST /set.
    expect(calls.map((x) => new URL(x.url).pathname)).toEqual([
      "/state/v1/get",
      "/state/v1/set",
    ]);
    const setBody = JSON.parse(calls[1].init.body as string);
    expect(setBody.kind).toBe("state");
    const written = JSON.parse(setBody.value);
    expect(written.status).toBe("COMPLETED");
    expect(written.step).toBe(5);
  });

  it("truncates oversized tool results before xadd", async () => {
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    const big = "x".repeat(9000);
    await c.pushToolResult("tc-1", "Read", big, "success");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.fields.result.endsWith("…[truncated]")).toBe(true);
    expect(body.fields.result.length).toBeLessThan(big.length);
  });

  it("swallows fetch failures (xadd) without throwing", async () => {
    stubFetch(() => {
      throw new Error("network down");
    });
    const c = fromEnv();
    await expect(c.pushToken("x")).resolves.toBeUndefined();
  });

  it("swallows non-2xx responses (get) and returns null", async () => {
    stubFetch(() => new Response("nope", { status: 502 }));
    const c = fromEnv();
    await expect(c.readCheckpoint()).resolves.toBeNull();
  });

  it("stays a no-op when the broker lease is missing", async () => {
    vi.stubEnv("TOOL_BROKER_LEASE", "");
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    await c.pushToken("x");
    await expect(c.readCheckpoint()).resolves.toBeNull();
    expect(calls).toHaveLength(0);
    expect(FakeRedis.last).toBeNull();
  });

  it("writePaused POSTs /state/v1/set with kind=paused and the 7-day TTL", async () => {
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    const blob = {
      kind: "confirm" as const,
      reply_id: "",
      tool_calls: [{ id: "tc-1", name: "mcp__x__y", args: "{}" }],
      question: null,
      agent_state: { transcript: [] },
      paused_at: "2026-06-15T00:00:00.000Z",
    };
    await c.writePaused(blob);

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("http://tool-broker:8080/state/v1/set");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.run_id).toBe("run_b");
    // The broker SET_KINDS routes the `:paused` key suffix to kind="paused".
    expect(body.kind).toBe("paused");
    expect(body.ttl).toBe(7 * 24 * 3600);
    expect(JSON.parse(body.value)).toEqual(blob);
  });

  it("markPaused reads state then writes status=PAUSED with the 7-day TTL", async () => {
    const prior = JSON.stringify({
      run_id: "run_b",
      step: 6,
      plan: "p",
      cursor: "c",
      last_event_seq: 6,
      status: "RUNNING",
      updated_step: 6,
    });
    const calls = stubFetch((url) =>
      new URL(url).pathname === "/state/v1/get"
        ? Response.json({ value: prior })
        : Response.json({ ok: true }),
    );
    const c = fromEnv();
    await c.markPaused();

    expect(calls.map((x) => new URL(x.url).pathname)).toEqual([
      "/state/v1/get",
      "/state/v1/set",
    ]);
    const setBody = JSON.parse(calls[1].init.body as string);
    expect(setBody.kind).toBe("state");
    expect(setBody.ttl).toBe(7 * 24 * 3600);
    expect(JSON.parse(setBody.value).status).toBe("PAUSED");
    expect(JSON.parse(setBody.value).step).toBe(6);
  });

  it("pushPendingConfirmation xadds the documented field shape via the broker", async () => {
    const calls = stubFetch(() => Response.json({ ok: true }));
    const c = fromEnv();
    await c.pushPendingConfirmation("tc-1", "mcp__github__search_code", '{"q":"x"}');
    expect(calls[0].url).toBe("http://tool-broker:8080/state/v1/xadd");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.fields).toEqual({
      type: "pending_confirmation",
      tool_call_id: "tc-1",
      name: "mcp__github__search_code",
      args: '{"q":"x"}',
    });
  });
});
