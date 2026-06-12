/**
 * redis_state.test.ts — RedisStateClient contract tests with a mocked ioredis.
 *
 * The mock records xadd/set/get/expire calls so we can assert the exact stream
 * entry fields (aligned with the Python redis_state.py), checkpoint parsing,
 * and failure-open behaviour (a throwing client must not throw out of a method).
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

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

  async set(key: string, value: string, ..._rest: unknown[]): Promise<"OK"> {
    this.setCalls.push({ key, value });
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
