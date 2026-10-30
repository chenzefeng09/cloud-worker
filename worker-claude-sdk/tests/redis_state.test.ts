/**
 * redis_state.test.ts — RedisStateClient contract tests with a mocked ioredis.
 *
 * The mock records xadd/set/get/expire calls so we can assert the exact stream
 * entry fields (aligned with the Python redis_state.py), checkpoint parsing,
 * and failure-open behaviour (a throwing client must not throw out of a method).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StateTransport } from "../src/redis_state.js";

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

  /** Commands queued per MULTI, in order (asserts XADD+EXPIRE pairing). */
  multiBatches: string[][] = [];
  /** Make EXEC report a per-command error for the XADD. */
  failXaddInMulti = false;

  multi(): FakeMulti {
    return new FakeMulti(this);
  }

  async quit(): Promise<"OK"> {
    return "OK";
  }
}

/** ioredis ChainableCommander stand-in: queues, then applies on exec(). */
class FakeMulti {
  private readonly ops: Array<() => Promise<[Error | null, unknown]>> = [];
  private readonly names: string[] = [];

  constructor(private readonly redis: FakeRedis) {}

  xadd(key: string, ...args: unknown[]): this {
    this.names.push("xadd");
    this.ops.push(async () => {
      if (this.redis.throwOnXadd || this.redis.failXaddInMulti) {
        return [new Error("boom-xadd"), null];
      }
      this.redis.xaddCalls.push({ key, args });
      return [null, "1-0"];
    });
    return this;
  }

  expire(key: string, ttl: number): this {
    this.names.push("expire");
    this.ops.push(async () => {
      this.redis.expireCalls.push({ key, ttl });
      return [null, 1];
    });
    return this;
  }

  async exec(): Promise<Array<[Error | null, unknown]>> {
    this.redis.multiBatches.push([...this.names]);
    const out: Array<[Error | null, unknown]> = [];
    for (const op of this.ops) out.push(await op());
    return out;
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
    // The terminal entry carries its status (default "completed").
    expect(streamFields(calls[1])).toEqual({ type: "end", text: "", status: "completed" });
  });

  it("pushTokenEnd carries the given terminal status", async () => {
    for (const status of ["failed", "paused", "cancelled"] as const) {
      const c = new RedisStateClient("run_x", "redis://localhost:6379");
      await c.pushTokenEnd(status);
      expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({ type: "end", text: "", status });
    }
  });

  it("pushes exactly one end entry and drops every push after it", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushTokenEnd("failed");
    await c.pushTokenEnd("completed");
    await c.pushToken("late");
    await c.pushToolCall("tc-late", "Bash", "{}");
    await c.flush();
    const entries = FakeRedis.last!.xaddCalls.map(streamFields);
    expect(entries).toEqual([{ type: "end", text: "", status: "failed" }]);
  });

  it("pushError writes type=error with the message", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushError("The model service is temporarily unavailable. Please try again.");
    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "error",
      message: "The model service is temporarily unavailable. Please try again.",
    });
  });

  it("pushThinkingDelta writes type=thinking_delta", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushThinkingDelta("step one");
    await c.flush();
    expect(streamFields(FakeRedis.last!.xaddCalls[0])).toEqual({
      type: "thinking_delta",
      text: "step one",
    });
  });

  it("writes at most one tool_call entry per tool_call_id", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToolCall("tc-1", "Bash", '{"cmd":"pwd"}');
    await c.pushToolCall("tc-1", "Bash", '{"cmd":"pwd"}{"cmd":"pwd"}');
    await c.pushToolCall("tc-2", "Read", '{"f":"a"}');
    const entries = FakeRedis.last!.xaddCalls.map(streamFields);
    expect(entries).toEqual([
      { type: "tool_call", tool_call_id: "tc-1", name: "Bash", args: '{"cmd":"pwd"}' },
      { type: "tool_call", tool_call_id: "tc-2", name: "Read", args: '{"f":"a"}' },
    ]);
  });

  it("trims with MAXLEN ~ 100000", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToolCall("tc-1", "Bash", "{}");
    expect(FakeRedis.last!.xaddCalls[0].args.slice(0, 4)).toEqual(["MAXLEN", "~", 100000, "*"]);
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

  it("refreshes the stream TTL on EVERY write, atomically with the XADD", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379", null, {
      coalesceWindowMs: 0,
    });
    await c.pushToken("a");
    await c.pushToken("b");
    await c.pushToolCall("tc-1", "Bash", "{}");
    expect(FakeRedis.last!.expireCalls).toEqual([
      { key: "agent:run:run_x:tokens", ttl: 1800 },
      { key: "agent:run:run_x:tokens", ttl: 1800 },
      { key: "agent:run:run_x:tokens", ttl: 1800 },
    ]);
    // One MULTI per entry: the entry never lands without its TTL refresh.
    expect(FakeRedis.last!.multiBatches).toEqual([
      ["xadd", "expire"],
      ["xadd", "expire"],
      ["xadd", "expire"],
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

describe("delta coalescing", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("merges adjacent delta chunks into one entry", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("Hel");
    await c.pushToken("lo ");
    await c.pushToken("world");
    // Still buffered (inside the window) …
    expect(FakeRedis.last!.xaddCalls).toHaveLength(0);
    await c.flush();
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([
      { type: "delta", text: "Hello world" },
    ]);
  });

  it("flushes the buffer when the window elapses", async () => {
    vi.useFakeTimers();
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("a");
    await c.pushToken("b");
    await vi.advanceTimersByTimeAsync(60);
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([{ type: "delta", text: "ab" }]);
    await c.pushToken("c");
    await vi.advanceTimersByTimeAsync(60);
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([
      { type: "delta", text: "ab" },
      { type: "delta", text: "c" },
    ]);
  });

  it("flushes as soon as the buffer reaches 512 chars", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("x".repeat(300));
    expect(FakeRedis.last!.xaddCalls).toHaveLength(0);
    await c.pushToken("y".repeat(300));
    const entries = FakeRedis.last!.xaddCalls.map(streamFields);
    expect(entries).toEqual([{ type: "delta", text: "x".repeat(300) + "y".repeat(300) }]);
  });

  it("flushes buffered text before any other entry (order preserved)", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushThinkingDelta("think ");
    await c.pushThinkingDelta("more");
    await c.pushToken("I'll ");
    await c.pushToken("look.");
    await c.pushToolCall("tc-1", "Bash", '{"cmd":"ls"}');
    await c.pushToken("Done.");
    await c.pushTokenEnd("completed");
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([
      { type: "thinking_delta", text: "think more" },
      { type: "delta", text: "I'll look." },
      { type: "tool_call", tool_call_id: "tc-1", name: "Bash", args: '{"cmd":"ls"}' },
      { type: "delta", text: "Done." },
      { type: "end", text: "", status: "completed" },
    ]);
  });

  it("close() flushes the buffer before closing", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("tail");
    await c.close();
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([{ type: "delta", text: "tail" }]);
  });

  it("writes every chunk immediately when coalescing is disabled", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379", null, {
      coalesceWindowMs: 0,
    });
    await c.pushToken("a");
    await c.pushToken("b");
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([
      { type: "delta", text: "a" },
      { type: "delta", text: "b" },
    ]);
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

  it("pending pushes alone do not end the stream; end(paused) does", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushPendingConfirmation("tc-1", "mcp__x__y", "{}");
    expect(FakeRedis.last!.xaddCalls.map((x) => streamFields(x).type)).toEqual([
      "pending_confirmation",
    ]);
    await c.pushTokenEnd("paused");
    expect(streamFields(FakeRedis.last!.xaddCalls[1])).toEqual({
      type: "end",
      text: "",
      status: "paused",
    });
  });

  it("markPaused extends the token stream to the paused TTL, now and for later writes", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToolCall("tc-0", "Read", "{}");
    await c.markPaused();
    await c.pushPendingConfirmation("tc-1", "mcp__x__y", "{}");
    await c.pushTokenEnd("paused");
    expect(FakeRedis.last!.expireCalls).toEqual([
      { key: "agent:run:run_x:tokens", ttl: 1800 }, // tool_call (running)
      { key: "agent:run:run_x:tokens", ttl: PAUSED_TTL }, // markPaused itself
      { key: "agent:run:run_x:tokens", ttl: PAUSED_TTL }, // pending_confirmation
      { key: "agent:run:run_x:tokens", ttl: PAUSED_TTL }, // end(paused)
    ]);
  });

  it("a delta buffered before markPaused is written with the paused TTL", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.pushToken("partial answer");
    await c.markPaused();
    await c.flush();
    expect(FakeRedis.last!.xaddCalls.map(streamFields)).toEqual([
      { type: "delta", text: "partial answer" },
    ]);
    // The buffered delta's MULTI and markPaused's own EXPIRE both use 7 days —
    // nothing can shrink the stream back to the running TTL.
    expect(FakeRedis.last!.expireCalls).toEqual([
      { key: "agent:run:run_x:tokens", ttl: PAUSED_TTL },
      { key: "agent:run:run_x:tokens", ttl: PAUSED_TTL },
    ]);
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

describe("heartbeat keep-alive", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const types = (redis: FakeRedis) => redis.xaddCalls.map((x) => streamFields(x).type);

  it("a quiet run gets a heartbeat per interval, refreshing the stream and state TTLs", async () => {
    vi.useFakeTimers();
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    await c.writeStep({ cursor: "start", status: "RUNNING" });
    c.startHeartbeat(1000);
    // The tool call is announced, then runs without writing anything.
    await c.pushToolCall("tc-1", "mcp__etl__run", "{}");
    const redis = FakeRedis.last!;
    const runningBlob = redis.setCalls[0].value;

    await vi.advanceTimersByTimeAsync(1000);
    expect(redis.xaddCalls.map(streamFields).at(-1)).toEqual({ type: "heartbeat" });
    expect(redis.multiBatches.at(-1)).toEqual(["xadd", "expire"]);
    expect(redis.expireCalls.at(-1)).toEqual({ key: "agent:run:run_x:tokens", ttl: 1800 });
    // The RUNNING blob is re-set unchanged (no step bump), with the running TTL.
    expect(redis.setCalls.at(-1)).toEqual({ key: "agent:run:run_x:state", value: runningBlob });
    expect(redis.setExArgs.at(-1)).toBe(1800);

    await vi.advanceTimersByTimeAsync(2000);
    expect(types(redis)).toEqual(["tool_call", "heartbeat", "heartbeat", "heartbeat"]);
    await c.close();
  });

  it("writes no heartbeat while the stream keeps flowing", async () => {
    vi.useFakeTimers();
    const c = new RedisStateClient("run_x", "redis://localhost:6379", null, {
      coalesceWindowMs: 0,
    });
    c.startHeartbeat(1000);
    for (let i = 0; i < 5; i += 1) {
      await vi.advanceTimersByTimeAsync(600);
      await c.pushToken(`t${i}`);
    }
    expect(types(FakeRedis.last!)).toEqual(["delta", "delta", "delta", "delta", "delta"]);
    await c.close();
  });

  const stops: Array<[string, (c: InstanceType<typeof RedisStateClient>) => Promise<void>]> = [
    ["the terminal end", (c) => c.pushTokenEnd("completed")],
    ["a pause", (c) => c.markPaused()],
    ["a terminal status", (c) => c.markFailed()],
    ["close()", (c) => c.close()],
  ];
  for (const [name, stop] of stops) {
    it(`stops at ${name}`, async () => {
      vi.useFakeTimers();
      const c = new RedisStateClient("run_x", "redis://localhost:6379");
      await c.writeStep({ cursor: "start", status: "RUNNING" });
      c.startHeartbeat(1000);
      await stop(c);
      const setsBefore = FakeRedis.last!.setCalls.length;
      await vi.advanceTimersByTimeAsync(5000);
      expect(types(FakeRedis.last!)).not.toContain("heartbeat");
      expect(FakeRedis.last!.setCalls).toHaveLength(setsBefore);
    });
  }

  it("a heartbeat already writing lands before the terminal status, never over it", async () => {
    vi.useFakeTimers();
    let stored: string | null = null;
    const statuses: string[] = [];
    let gate: Promise<void> | null = null;
    let release: () => void = () => {};
    const transport: StateTransport = {
      async get() {
        return stored;
      },
      async set(_key, value) {
        if (gate && JSON.parse(value).status === "RUNNING") await gate;
        stored = value;
        statuses.push(JSON.parse(value).status);
      },
      async xadd() {},
      async close() {},
    };
    const c = new RedisStateClient("run_x", undefined, transport);
    await c.writeStep({ cursor: "start", status: "RUNNING" });
    c.startHeartbeat(1000);
    gate = new Promise((resolve) => {
      release = resolve;
    });
    await vi.advanceTimersByTimeAsync(1000); // the heartbeat's RUNNING re-set is now stuck

    const marking = c.markCompleted();
    await vi.advanceTimersByTimeAsync(0); // an unguarded status write would land here
    release();
    await marking;

    expect(statuses).toEqual(["RUNNING", "RUNNING", "COMPLETED"]);
    expect(JSON.parse(stored!).status).toBe("COMPLETED");
  });
});

describe("failure-open", () => {
  it("does not throw when xadd throws", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.throwOnXadd = true;
    await expect(c.pushToken("x")).resolves.toBeUndefined();
    await expect(c.flush()).resolves.toBeUndefined();
    await expect(c.pushTokenEnd("failed")).resolves.toBeUndefined();
  });

  it("does not throw when EXEC reports a per-command error", async () => {
    const c = new RedisStateClient("run_x", "redis://localhost:6379");
    FakeRedis.last!.failXaddInMulti = true;
    await expect(c.pushToolCall("tc-1", "Bash", "{}")).resolves.toBeUndefined();
    await expect(c.close()).resolves.toBeUndefined();
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
    await c.flush(); // deltas are coalesced; flush writes the buffered chunk

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
      maxlen: 100000,
      expire: 1800,
    });
    // No key is sent — the broker reconstructs the token stream key from run_id.
    expect(body).not.toHaveProperty("key");
  });

  it("every xadd carries the TTL; after markPaused it carries the paused TTL", async () => {
    const calls = stubFetch((url) =>
      new URL(url).pathname === "/state/v1/get"
        ? Response.json({ value: null })
        : Response.json({ ok: true }),
    );
    const c = fromEnv();
    await c.pushToolCall("tc-0", "Read", "{}");
    await c.markPaused();
    await c.pushPendingConfirmation("tc-1", "mcp__x__y", "{}");
    await c.pushTokenEnd("paused");
    const xadds = calls
      .filter((x) => new URL(x.url).pathname === "/state/v1/xadd")
      .map((x) => JSON.parse(x.init.body as string));
    expect(xadds.map((b) => [b.fields.type, b.expire])).toEqual([
      ["tool_call", 1800],
      ["pending_confirmation", 7 * 24 * 3600],
      ["end", 7 * 24 * 3600],
    ]);
    expect(xadds[2].fields).toEqual({ type: "end", text: "", status: "paused" });
  });

  it("a heartbeat goes through the broker xadd (TTL folded in) and re-sets kind=state", async () => {
    vi.useFakeTimers();
    try {
      const calls = stubFetch(() => Response.json({ ok: true }));
      const c = fromEnv();
      await c.writeStep({ cursor: "start", status: "RUNNING" });
      c.startHeartbeat(1000);
      await vi.advanceTimersByTimeAsync(1000);

      const sent = calls.map((x) => ({
        path: new URL(x.url).pathname,
        body: JSON.parse(x.init.body as string),
      }));
      expect(sent.map((s) => s.path)).toEqual([
        "/state/v1/set",
        "/state/v1/xadd",
        "/state/v1/set",
      ]);
      expect(sent[1].body).toEqual({
        run_id: "run_b",
        fields: { type: "heartbeat" },
        maxlen: 100000,
        expire: 1800,
      });
      expect(sent[2].body).toEqual({ ...sent[0].body, ttl: 1800 });
      await c.close();
    } finally {
      vi.useRealTimers();
    }
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
