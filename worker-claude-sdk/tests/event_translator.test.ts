/**
 * event_translator.test.ts — SDK message stream → agentscope event vocabulary.
 *
 * Feeds constructed SDKMessage sequences and asserts the event_type order,
 * fields, cumulative usage accumulation, and ReplyEndEvent-on-success.
 */

import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { EventTranslator, type EventPayload } from "../src/event_translator.js";

// SDKMessage is a wide union; tests build minimal shapes and cast.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyMsg = any;

function types(events: EventPayload[]): string[] {
  return events.map((e) => e.event_type);
}

describe("EventTranslator", () => {
  it("translates an assistant message with text + tool_use + thinking", () => {
    const t = new EventTranslator();
    const msg: AnyMsg = {
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "hello" },
          { type: "tool_use", id: "tc-1", name: "Bash", input: { cmd: "pwd" } },
          { type: "thinking", thinking: "let me think" },
        ],
        usage: { input_tokens: 100, output_tokens: 40 },
      },
    };

    const events = t.translateMessage(msg);

    expect(types(events)).toEqual([
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "ToolCallStartEvent",
      "ToolCallDeltaEvent",
      "ToolCallEndEvent",
      "ThinkingBlockStartEvent",
      "ThinkingBlockDeltaEvent",
      "ThinkingBlockEndEvent",
      "ModelCallEndEvent",
    ]);

    const textStart = events[0];
    const textDelta = events[1];
    expect(textStart.block_id).toBeTruthy();
    expect(textDelta.block_id).toBe(textStart.block_id);
    expect(textDelta.delta).toBe("hello");

    const toolStart = events[2];
    const toolDelta = events[3];
    expect(toolStart.tool_call_id).toBe("tc-1");
    expect(toolStart.tool_call_name).toBe("Bash");
    expect(toolDelta.delta).toBe(JSON.stringify({ cmd: "pwd" }));

    const thinkStart = events[5];
    const thinkDelta = events[6];
    expect(thinkStart.block_id).toBeTruthy();
    expect(thinkDelta.block_id).toBe(thinkStart.block_id);
    expect(thinkDelta.delta).toBe("let me think");

    const modelEnd = events[8];
    expect(modelEnd.input_tokens).toBe(100);
    expect(modelEnd.output_tokens).toBe(40);
  });

  it("translates a user (tool_result) message and reads the tool name from prior ToolCallStart", () => {
    const t = new EventTranslator();
    // Prime the name map via an assistant tool_use first.
    t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "tool_use", id: "tc-1", name: "Bash", input: {} }],
        usage: { input_tokens: 1, output_tokens: 1 },
      },
    } as AnyMsg);

    const events = t.translateMessage({
      type: "user",
      message: {
        content: [
          {
            type: "tool_result",
            tool_use_id: "tc-1",
            content: "/workspace",
            is_error: false,
          },
        ],
      },
    } as AnyMsg);

    expect(types(events)).toEqual([
      "ToolResultStartEvent",
      "ToolResultTextDeltaEvent",
      "ToolResultEndEvent",
    ]);
    expect(events[0].tool_call_name).toBe("Bash");
    expect(events[1].delta).toBe("/workspace");
    expect(events[2].state).toBe("success");
  });

  it("marks tool_result state=error when is_error is set", () => {
    const t = new EventTranslator();
    const events = t.translateMessage({
      type: "user",
      message: {
        content: [
          {
            type: "tool_result",
            tool_use_id: "tc-x",
            content: [{ type: "text", text: "boom" }],
            is_error: true,
          },
        ],
      },
    } as AnyMsg);
    expect(events[2].state).toBe("error");
    expect(events[1].delta).toBe("boom");
  });

  it("emits ReplyEndEvent on result success and ErrorEvent on result error", () => {
    const t = new EventTranslator();
    const ok = t.translateMessage({ type: "result", subtype: "success" } as AnyMsg);
    expect(types(ok)).toEqual(["ReplyEndEvent"]);

    const err = t.translateMessage({
      type: "result",
      subtype: "error_during_execution",
      errors: ["model unreachable"],
    } as AnyMsg);
    expect(types(err)).toEqual(["ErrorEvent"]);
    expect(err[0].message).toBe("model unreachable");
  });

  it("accumulates usage across multiple assistant messages (B.6 usage fix)", () => {
    const t = new EventTranslator();
    t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "text", text: "a" }],
        usage: { input_tokens: 100, output_tokens: 50 },
      },
    } as AnyMsg);
    t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "text", text: "b" }],
        usage: { input_tokens: 200, output_tokens: 80 },
      },
    } as AnyMsg);

    const usage = t.getCumulativeUsage();
    expect(usage.input_tokens).toBe(300);
    expect(usage.output_tokens).toBe(130);
    expect(usage.total_tokens).toBe(430);
  });

  it("accumulates DeepSeek cache_read_input_tokens across assistant messages", () => {
    const t = new EventTranslator();
    const a = t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "text", text: "a" }],
        // Anthropic-shaped usage produced by the adapter from DeepSeek's
        // prompt_cache_hit_tokens.
        usage: { input_tokens: 100, output_tokens: 50, cache_read_input_tokens: 30 },
      },
    } as AnyMsg);
    // Per-turn increment is surfaced on ModelCallEndEvent.
    const modelEnd = a.find((e) => e.event_type === "ModelCallEndEvent")!;
    expect(modelEnd.cache_read_input_tokens).toBe(30);

    t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "text", text: "b" }],
        usage: { input_tokens: 200, output_tokens: 80, cache_read_input_tokens: 70 },
      },
    } as AnyMsg);

    expect(t.getCumulativeUsage().cache_read_input_tokens).toBe(100);
  });

  it("defaults cache_read_input_tokens to 0 when absent (failure-open)", () => {
    const t = new EventTranslator();
    t.translateMessage({
      type: "assistant",
      message: { content: [{ type: "text", text: "a" }], usage: { input_tokens: 5, output_tokens: 3 } },
    } as AnyMsg);
    expect(t.getCumulativeUsage().cache_read_input_tokens).toBe(0);
  });

  it("logs (no event) on system init and emits UnknownSystemEvent otherwise", () => {
    const t = new EventTranslator();
    const init = t.translateMessage({ type: "system", subtype: "init", tools: [], mcp_servers: [] } as AnyMsg);
    expect(init).toEqual([]);

    const other = t.translateMessage({ type: "system", subtype: "status" } as AnyMsg);
    expect(types(other)).toEqual(["UnknownSystemEvent"]);
    expect(other[0].subtype).toBe("status");
  });

  it("mints distinct block ids per assistant message", () => {
    const t = new EventTranslator();
    const first = t.translateMessage({
      type: "assistant",
      message: { content: [{ type: "text", text: "x" }], usage: {} },
    } as AnyMsg);
    const second = t.translateMessage({
      type: "assistant",
      message: { content: [{ type: "text", text: "y" }], usage: {} },
    } as AnyMsg);
    expect(first[0].block_id).not.toBe(second[0].block_id);
  });
});

// ── Block 2: per-token streaming via type:"stream_event" ─────────────────────
//
// includePartialMessages=true makes the SDK emit SDKPartialAssistantMessage
// (`type:"stream_event"`, carrying an Anthropic RawMessageStreamEvent on
// `.event`). The translator turns each raw delta into one incremental
// agentscope event, and dedups the consolidated `assistant` message that
// follows so text/tool blocks are not written twice. These helpers build the
// raw stream_event wire shapes.

/** Wrap an Anthropic raw streaming event into an SDK stream_event message. */
function streamEvent(event: AnyMsg): AnyMsg {
  return { type: "stream_event", event };
}

const msgStart = (id?: string) =>
  streamEvent({
    type: "message_start",
    message: { ...(id ? { id } : {}), usage: {} },
  });
const cbStart = (index: number, content_block: AnyMsg) =>
  streamEvent({ type: "content_block_start", index, content_block });
const cbDelta = (index: number, delta: AnyMsg) =>
  streamEvent({ type: "content_block_delta", index, delta });
const cbStop = (index: number) =>
  streamEvent({ type: "content_block_stop", index });
const msgStop = () => streamEvent({ type: "message_stop" });

describe("EventTranslator — per-token streaming (stream_event)", () => {
  it("emits one TextBlockDeltaEvent per text_delta (per-token, not coalesced)", () => {
    const t = new EventTranslator();
    t.translateMessage(msgStart());
    const start = t.translateMessage(cbStart(0, { type: "text", text: "" }));
    expect(types(start)).toEqual(["TextBlockStartEvent"]);
    const blockId = start[0].block_id as string;
    expect(blockId).toBeTruthy();

    // Three consecutive text_delta tokens -> three separate delta events.
    const d1 = t.translateMessage(cbDelta(0, { type: "text_delta", text: "Hel" }));
    const d2 = t.translateMessage(cbDelta(0, { type: "text_delta", text: "lo " }));
    const d3 = t.translateMessage(cbDelta(0, { type: "text_delta", text: "world" }));

    for (const d of [d1, d2, d3]) {
      expect(types(d)).toEqual(["TextBlockDeltaEvent"]);
      // Every delta carries the same block_id minted on content_block_start.
      expect(d[0].block_id).toBe(blockId);
    }
    // Per-token: deltas are NOT merged — three events, three fragments in order.
    expect([d1[0].delta, d2[0].delta, d3[0].delta]).toEqual([
      "Hel",
      "lo ",
      "world",
    ]);

    const stop = t.translateMessage(cbStop(0));
    expect(types(stop)).toEqual(["TextBlockEndEvent"]);
    expect(stop[0].block_id).toBe(blockId);
  });

  it("does NOT re-emit text/tool blocks on the consolidated assistant message when partials streamed this turn (dedup)", () => {
    const t = new EventTranslator();
    // Streamed turn: a text block and a tool_use block arrive as partials.
    t.translateMessage(msgStart());
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "hi" }));
    t.translateMessage(cbStop(0));
    t.translateMessage(
      cbStart(1, { type: "tool_use", id: "tc-1", name: "Bash" }),
    );
    t.translateMessage(cbDelta(1, { type: "input_json_delta", partial_json: '{"cmd":"pwd"}' }));
    t.translateMessage(cbStop(1));
    t.translateMessage(msgStop());

    // The SDK now delivers the consolidated assistant message for the SAME turn.
    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "hi" },
          { type: "tool_use", id: "tc-1", name: "Bash", input: { cmd: "pwd" } },
        ],
        usage: { input_tokens: 120, output_tokens: 30 },
      },
    } as AnyMsg);

    // Dedup: NO TextBlock*/ToolCall* re-emission — only the usage accounting.
    expect(types(consolidated)).toEqual(["ModelCallEndEvent"]);
    expect(consolidated[0].input_tokens).toBe(120);
    expect(consolidated[0].output_tokens).toBe(30);
  });

  it("uses consolidated tool input when the SDK omits input_json_delta", () => {
    const t = new EventTranslator();
    const id = "msg_tool_input_fallback";

    t.translateMessage(msgStart(id));
    expect(
      types(t.translateMessage(cbStart(0, { type: "tool_use", id: "tc-fallback", name: "Bash" }))),
    ).toEqual(["ToolCallStartEvent"]);

    // The SDK can close the tool block without sending any argument deltas.
    expect(t.translateMessage(cbStop(0))).toEqual([]);

    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "tool_use", id: "tc-fallback", name: "Bash", input: { cmd: "pwd" } }],
        usage: { input_tokens: 10, output_tokens: 2 },
      },
    } as AnyMsg);

    expect(types(consolidated)).toEqual([
      "ToolCallDeltaEvent",
      "ToolCallEndEvent",
      "ModelCallEndEvent",
    ]);
    expect(consolidated[0].delta).toBe(JSON.stringify({ cmd: "pwd" }));
    expect(t.translateMessage(cbStop(0))).toEqual([]);
  });

  it("counts usage exactly once (from final assistant), never doubled by partials", () => {
    const t = new EventTranslator();
    t.translateMessage(msgStart());
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "x" }));
    t.translateMessage(cbStop(0));
    // message_delta carries Anthropic's running output usage — must be ignored
    // so it does not double-count against the final assistant message.
    t.translateMessage(
      streamEvent({
        type: "message_delta",
        delta: {},
        usage: { input_tokens: 999, output_tokens: 999 },
      }),
    );
    t.translateMessage(msgStop());

    t.translateMessage({
      type: "assistant",
      message: {
        content: [{ type: "text", text: "x" }],
        usage: { input_tokens: 200, output_tokens: 50 },
      },
    } as AnyMsg);

    // Only the authoritative assistant usage is accumulated; partials added 0.
    const usage = t.getCumulativeUsage();
    expect(usage.input_tokens).toBe(200);
    expect(usage.output_tokens).toBe(50);
    expect(usage.total_tokens).toBe(250);
  });

  it("translates input_json_delta into per-token ToolCallDeltaEvent carrying the tool_call_id", () => {
    const t = new EventTranslator();
    t.translateMessage(msgStart());
    const start = t.translateMessage(
      cbStart(0, { type: "tool_use", id: "tc-9", name: "Write" }),
    );
    expect(types(start)).toEqual(["ToolCallStartEvent"]);
    expect(start[0].tool_call_id).toBe("tc-9");
    expect(start[0].tool_call_name).toBe("Write");
    // The name is remembered for the later tool_result lookup.
    expect(t.toolNameFor("tc-9")).toBe("Write");

    // Streamed argument JSON arrives in fragments — each is its own delta event.
    const f1 = t.translateMessage(
      cbDelta(0, { type: "input_json_delta", partial_json: '{"path":' }),
    );
    const f2 = t.translateMessage(
      cbDelta(0, { type: "input_json_delta", partial_json: '"/tmp/a"}' }),
    );
    expect(types(f1)).toEqual(["ToolCallDeltaEvent"]);
    expect(types(f2)).toEqual(["ToolCallDeltaEvent"]);
    expect(f1[0].tool_call_id).toBe("tc-9");
    expect(f2[0].tool_call_id).toBe("tc-9");
    expect(f1[0].delta).toBe('{"path":');
    expect(f2[0].delta).toBe('"/tmp/a"}');

    // content_block_stop on a tool_use index closes with ToolCallEndEvent.
    const stop = t.translateMessage(cbStop(0));
    expect(types(stop)).toEqual(["ToolCallEndEvent"]);
    expect(stop[0].tool_call_id).toBe("tc-9");
  });

  it("translates thinking_delta into per-token ThinkingBlockDeltaEvent", () => {
    const t = new EventTranslator();
    t.translateMessage(msgStart());
    const start = t.translateMessage(cbStart(0, { type: "thinking", thinking: "" }));
    expect(types(start)).toEqual(["ThinkingBlockStartEvent"]);
    const blockId = start[0].block_id as string;
    expect(blockId).toBeTruthy();

    const d1 = t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: "step " }));
    const d2 = t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: "one" }));
    expect(types(d1)).toEqual(["ThinkingBlockDeltaEvent"]);
    expect(types(d2)).toEqual(["ThinkingBlockDeltaEvent"]);
    expect(d1[0].block_id).toBe(blockId);
    expect(d2[0].block_id).toBe(blockId);
    expect(d1[0].delta).toBe("step ");
    expect(d2[0].delta).toBe("one");

    const stop = t.translateMessage(cbStop(0));
    expect(types(stop)).toEqual(["ThinkingBlockEndEvent"]);
    expect(stop[0].block_id).toBe(blockId);
  });

  it("produces no stray content events for non-delta framing (message_start / *_stop unmatched / message_stop)", () => {
    const t = new EventTranslator();
    // message_start: bookkeeping reset only, no events.
    expect(t.translateMessage(msgStart())).toEqual([]);
    // content_block_stop for an index we never opened: nothing to close.
    expect(t.translateMessage(cbStop(7))).toEqual([]);
    // message_delta / message_stop: framing only, no events.
    expect(
      t.translateMessage(
        streamEvent({ type: "message_delta", delta: {}, usage: { output_tokens: 5 } }),
      ),
    ).toEqual([]);
    expect(t.translateMessage(msgStop())).toEqual([]);
  });

  // NOTE (corrected assumption): the previous version of this test asserted the
  // dedup state is "cleared after the consolidated message". That was the OLD
  // (buggy) behavior — clearing per consolidated wiped the records the next
  // per-block consolidated of the SAME turn needs, re-emitting the whole reply.
  // The real SDK delivers MULTIPLE per-block consolidated assistants per turn,
  // so state is now reset only on a NEW-turn message_start (different id), never
  // after a consolidated. This test keeps the same end-to-end guarantee — a
  // later turn whose text differs still emits full blocks — but no longer
  // depends on the wrong "clears after consolidated" mechanic. The dedup is
  // text-prefix based, so distinct texts ("a" then "b") never false-match.
  it("a later turn with different text still emits full blocks (text-prefix dedup, not index/clear)", () => {
    const t = new EventTranslator();
    // Turn 1: streamed -> consolidated assistant deduped to usage only.
    t.translateMessage(msgStart("msg_clear_1"));
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "a" }));
    t.translateMessage(cbStop(0));
    const turn1 = t.translateMessage({
      type: "assistant",
      message: { id: "msg_clear_1", content: [{ type: "text", text: "a" }], usage: { input_tokens: 10, output_tokens: 5 } },
    } as AnyMsg);
    expect(types(turn1)).toEqual(["ModelCallEndEvent"]);

    // Turn 2: NO partials this turn, NEW id, different text -> full-block emit.
    const turn2 = t.translateMessage({
      type: "assistant",
      message: { id: "msg_clear_2", content: [{ type: "text", text: "b" }], usage: { input_tokens: 20, output_tokens: 8 } },
    } as AnyMsg);
    expect(types(turn2)).toEqual([
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "ModelCallEndEvent",
    ]);
    expect(turn2[1].delta).toBe("b");

    // Both turns' usage accumulated exactly once each.
    expect(t.getCumulativeUsage().input_tokens).toBe(30);
    expect(t.getCumulativeUsage().output_tokens).toBe(13);
  });

  // ── regression: render doubling on a thinking-style turn ───────────────────
  // The bug: under includePartialMessages, a stray `message_start` arrives AFTER
  // the last text_delta and BEFORE the consolidated `assistant` message. The old
  // single-bool `streamedThisTurn` was reset by that trailing message_start, so
  // the consolidated message re-emitted the WHOLE answer as one big delta — the
  // reply text rendered twice. Per-block dedup keyed on the SDK message id (same
  // on the partials' message_start and the consolidated message) survives the
  // stray reset. This reproduces the exact wire sequence we captured.
  it("does NOT re-emit the full reply when a stray message_start lands after the last delta (render-doubling regression)", () => {
    const t = new EventTranslator();
    const id = "msg_deepseek_001";
    // Real deepseek turn: per-token text deltas under one message id.
    t.translateMessage(msgStart(id));
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    const d1 = t.translateMessage(cbDelta(0, { type: "text_delta", text: "hello" }));
    const d2 = t.translateMessage(cbDelta(0, { type: "text_delta", text: " world" }));
    expect(d1[0].delta).toBe("hello");
    expect(d2[0].delta).toBe(" world");
    t.translateMessage(cbStop(0));
    // The stray trailing message_start (SAME id) that defeated the old bool.
    t.translateMessage(msgStart(id));

    // Consolidated assistant for the SAME turn carries the whole "hello world".
    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "text", text: "hello world" }],
        usage: { input_tokens: 200, output_tokens: 90 },
      },
    } as AnyMsg);

    // The big whole-text delta must NOT be re-emitted — only usage is accounted.
    expect(types(consolidated)).toEqual(["ModelCallEndEvent"]);
    expect(
      consolidated.some(
        (e) =>
          e.event_type === "TextBlockDeltaEvent" && e.delta === "hello world",
      ),
    ).toBe(false);
    // "hello world" appears exactly once in the streamed token deltas, never
    // again from the consolidated message.
    expect(consolidated[0].input_tokens).toBe(200);
    expect(consolidated[0].output_tokens).toBe(90);
  });

  it("dedups via the (kind,index)/text-prefix fallback when the SDK omits a message id", () => {
    const t = new EventTranslator();
    // id-less partials (older/leaner SDK shape).
    t.translateMessage(msgStart());
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "the " }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "answer" }));
    t.translateMessage(cbStop(0));
    // Tool block streamed too — text-prefix fallback can't cover this, but the
    // (kind,index) fallback can.
    t.translateMessage(cbStart(1, { type: "tool_use", id: "tc-7", name: "Bash" }));
    t.translateMessage(
      cbDelta(1, { type: "input_json_delta", partial_json: '{"cmd":"ls"}' }),
    );
    t.translateMessage(cbStop(1));
    t.translateMessage(msgStop());

    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        // no id
        content: [
          { type: "text", text: "the answer" },
          { type: "tool_use", id: "tc-7", name: "Bash", input: { cmd: "ls" } },
        ],
        usage: { input_tokens: 50, output_tokens: 20 },
      },
    } as AnyMsg);

    // Both the text block and the tool block are recognized as already streamed.
    expect(types(consolidated)).toEqual(["ModelCallEndEvent"]);
  });

  it("does NOT re-emit a streamed thinking block on the consolidated assistant message", () => {
    const t = new EventTranslator();
    const id = "msg_think_001";
    t.translateMessage(msgStart(id));
    t.translateMessage(cbStart(0, { type: "thinking", thinking: "" }));
    t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: "step " }));
    t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: "one" }));
    t.translateMessage(cbStop(0));
    t.translateMessage(cbStart(1, { type: "text", text: "" }));
    t.translateMessage(cbDelta(1, { type: "text_delta", text: "done" }));
    t.translateMessage(cbStop(1));
    t.translateMessage(msgStart(id)); // stray trailing reset

    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [
          { type: "thinking", thinking: "step one" },
          { type: "text", text: "done" },
        ],
        usage: { input_tokens: 80, output_tokens: 40 },
      },
    } as AnyMsg);

    // Neither the thinking block nor the text block is re-emitted.
    expect(types(consolidated)).toEqual(["ModelCallEndEvent"]);
    expect(
      consolidated.some((e) => e.event_type.startsWith("ThinkingBlock")),
    ).toBe(false);
  });

  it("STILL emits full blocks on a non-streamed consolidated message (no partials at all)", () => {
    const t = new EventTranslator();
    // No stream_event partials this turn — the model returned a complete message.
    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        id: "msg_nonstream_001",
        content: [
          { type: "thinking", thinking: "reasoning" },
          { type: "text", text: "final answer" },
        ],
        usage: { input_tokens: 30, output_tokens: 12 },
      },
    } as AnyMsg);

    // Full reconstruction: text + thinking blocks ARE emitted (nothing lost).
    expect(types(consolidated)).toEqual([
      "ThinkingBlockStartEvent",
      "ThinkingBlockDeltaEvent",
      "ThinkingBlockEndEvent",
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "ModelCallEndEvent",
    ]);
    const textDelta = consolidated.find(
      (e) => e.event_type === "TextBlockDeltaEvent",
    )!;
    expect(textDelta.delta).toBe("final answer");
  });

  it("counts usage exactly once across both streamed and non-streamed turns", () => {
    const t = new EventTranslator();
    // Streamed turn with stray trailing message_start.
    const id = "msg_u1";
    t.translateMessage(msgStart(id));
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    t.translateMessage(cbDelta(0, { type: "text_delta", text: "x" }));
    t.translateMessage(cbStop(0));
    t.translateMessage(msgStart(id));
    t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "text", text: "x" }],
        usage: { input_tokens: 100, output_tokens: 40 },
      },
    } as AnyMsg);
    // Non-streamed turn.
    t.translateMessage({
      type: "assistant",
      message: {
        id: "msg_u2",
        content: [{ type: "text", text: "y" }],
        usage: { input_tokens: 60, output_tokens: 25 },
      },
    } as AnyMsg);

    const usage = t.getCumulativeUsage();
    expect(usage.input_tokens).toBe(160);
    expect(usage.output_tokens).toBe(65);
    expect(usage.total_tokens).toBe(225);
  });

  // ── real SDK shape: per-block consolidated assistants interleaved with the
  // partial stream (Bug B ground truth) ──────────────────────────────────────
  //
  // Captured from a real deepseek-v4-pro turn (see sdk-messages-sample.jsonl):
  // one message_start, then thinking streams at content index 0, then text
  // streams at content index 1. The SDK delivers a SEPARATE consolidated
  // `assistant` per block — the thinking one MID-STREAM (before text finishes),
  // the text one after — and BOTH place their single block at content-array
  // index 0 (so the consolidated array index != the streaming index). Both
  // consolidated messages and the message_start share ONE msg_… id, and both
  // carry the SAME usage snapshot.
  //
  // The old code (clear-per-consolidated + index-based dedup) re-emitted the
  // whole text block (answer rendered twice) and double-counted usage. This
  // reproduces that exact shape and asserts single emission + single usage.
  it("does NOT duplicate text when per-block consolidated assistants interleave the stream (real SDK shape)", () => {
    const t = new EventTranslator();
    const id = "msg_real_001";
    const answer =
      "北京是中华人民共和国的首都，是全国的政治、文化、国际交往和科技创新中心。";
    const reasoning = "用户问北京，我给一段简介。";

    const allDeltas: string[] = [];
    const push = (events: EventPayload[]) => {
      for (const e of events) {
        if (e.event_type === "TextBlockDeltaEvent") allDeltas.push(e.delta as string);
      }
    };

    t.translateMessage(msgStart(id));
    // thinking streams at content index 0
    t.translateMessage(cbStart(0, { type: "thinking", thinking: "" }));
    for (const ch of [...reasoning]) {
      t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: ch }));
    }
    // text streams at content index 1
    t.translateMessage(cbStart(1, { type: "text", text: "" }));
    for (const ch of [...answer]) {
      push(t.translateMessage(cbDelta(1, { type: "text_delta", text: ch })));
    }

    // per-block consolidated for the THINKING block arrives mid-stream, at array
    // index 0, SAME id, full usage snapshot.
    const cThink = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "thinking", thinking: reasoning }],
        usage: { input_tokens: 13154, output_tokens: 117, cache_read_input_tokens: 13056 },
      },
    } as AnyMsg);
    push(cThink);
    t.translateMessage(cbStop(0));

    // per-block consolidated for the TEXT block, also at array index 0, SAME id,
    // SAME usage snapshot — must NOT re-emit the answer.
    const cText = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "text", text: answer }],
        usage: { input_tokens: 13154, output_tokens: 117, cache_read_input_tokens: 13056 },
      },
    } as AnyMsg);
    push(cText);
    t.translateMessage(cbStop(1));

    // The full answer appears EXACTLY ONCE across all TextBlockDeltaEvents.
    const joined = allDeltas.join("");
    expect(joined).toBe(answer);
    expect(joined.split(answer).length - 1).toBe(1);
    // The thinking consolidated (first of the turn) dedups its block but DOES
    // account the turn's usage exactly once -> ModelCallEndEvent only, no
    // re-emitted ThinkingBlock*.
    expect(types(cThink)).toEqual(["ModelCallEndEvent"]);
    expect(cThink.some((e) => e.event_type.startsWith("ThinkingBlock"))).toBe(
      false,
    );
    // The text consolidated (same id, already-accounted) re-emits nothing at
    // all — neither the text block nor a second usage event.
    expect(types(cText)).toEqual([]);
    expect(cText.some((e) => e.event_type === "TextBlockDeltaEvent")).toBe(false);

    // Usage counted exactly once for the whole turn (one snapshot, repeated).
    const usage = t.getCumulativeUsage();
    expect(usage.input_tokens).toBe(13154);
    expect(usage.output_tokens).toBe(117);
    expect(usage.cache_read_input_tokens).toBe(13056);
  });

  // Replays the captured 127-message sequence verbatim and asserts the reply
  // text appears exactly once (163 chars, not 326) — the precise real-stack
  // failure. This is the ground-truth regression: it reads the SAME file the
  // worker would receive, with no hand-authored assumptions about SDK ordering.
  it("real sample replay: reply text and thinking each appear exactly once, usage counted once", () => {
    const sample = readFileSync(
      new URL("../sdk-messages-sample.jsonl", import.meta.url),
      "utf8",
    )
      .split("\n")
      .filter((l) => l.trim());

    // Derive the single ground-truth answer/thinking straight from raw deltas.
    let rawAnswer = "";
    let rawThink = "";
    for (const l of sample) {
      const m = JSON.parse(l);
      if (m.type === "stream_event" && m.event?.type === "content_block_delta") {
        const d = m.event.delta ?? {};
        if (d.type === "text_delta") rawAnswer += d.text ?? "";
        if (d.type === "thinking_delta") rawThink += d.thinking ?? "";
      }
    }
    expect(rawAnswer.length).toBeGreaterThan(0);

    const t = new EventTranslator();
    let textConcat = "";
    let thinkConcat = "";
    for (const l of sample) {
      const events = t.translateMessage(JSON.parse(l) as AnyMsg);
      for (const e of events) {
        if (e.event_type === "TextBlockDeltaEvent") textConcat += e.delta as string;
        if (e.event_type === "ThinkingBlockDeltaEvent")
          thinkConcat += e.delta as string;
      }
    }

    // Exactly one copy of the answer in the emitted text stream (not doubled).
    expect(textConcat).toBe(rawAnswer);
    expect(textConcat.split(rawAnswer).length - 1).toBe(1);
    // Thinking likewise emitted exactly once.
    expect(thinkConcat).toBe(rawThink);

    // Usage from the turn's repeated consolidated snapshots counted ONCE.
    const usage = t.getCumulativeUsage();
    expect(usage.input_tokens).toBe(13154);
    expect(usage.output_tokens).toBe(117);
    expect(usage.cache_read_input_tokens).toBe(13056);
  });

  // Covers "plain conversation also duplicated": a turn with only a text block
  // (no thinking consolidated to prematurely clear state). Must still emit once.
  it("pure-text turn (no thinking) emits the reply exactly once", () => {
    const t = new EventTranslator();
    const id = "msg_plain_001";
    const answer = "这是一个普通的纯文本回答。";

    const deltas: string[] = [];
    t.translateMessage(msgStart(id));
    t.translateMessage(cbStart(0, { type: "text", text: "" }));
    for (const ch of [...answer]) {
      const ev = t.translateMessage(cbDelta(0, { type: "text_delta", text: ch }));
      for (const e of ev)
        if (e.event_type === "TextBlockDeltaEvent") deltas.push(e.delta as string);
    }
    t.translateMessage(cbStop(0));

    const consolidated = t.translateMessage({
      type: "assistant",
      message: {
        id,
        content: [{ type: "text", text: answer }],
        usage: { input_tokens: 50, output_tokens: 20 },
      },
    } as AnyMsg);

    // Consolidated re-emits nothing; the streamed deltas already hold the answer.
    expect(types(consolidated)).toEqual(["ModelCallEndEvent"]);
    const joined = deltas.join("");
    expect(joined).toBe(answer);
    expect(joined.split(answer).length - 1).toBe(1);
  });

  // Two full turns each with their own message_start + new id. The second turn's
  // consolidated must not be skipped because of residual first-turn records, and
  // each turn's reply appears exactly once with its own usage.
  it("two consecutive turns: each reply emitted once, no cross-turn false dedup", () => {
    const t = new EventTranslator();

    const collectText = (id: string, answer: string) => {
      const deltas: string[] = [];
      t.translateMessage(msgStart(id));
      t.translateMessage(cbStart(0, { type: "text", text: "" }));
      for (const ch of [...answer]) {
        const ev = t.translateMessage(cbDelta(0, { type: "text_delta", text: ch }));
        for (const e of ev)
          if (e.event_type === "TextBlockDeltaEvent") deltas.push(e.delta as string);
      }
      t.translateMessage(cbStop(0));
      const consolidated = t.translateMessage({
        type: "assistant",
        message: {
          id,
          content: [{ type: "text", text: answer }],
          usage: { input_tokens: 100, output_tokens: 40 },
        },
      } as AnyMsg);
      return { joined: deltas.join(""), consolidated };
    };

    const a1 = "第一轮的答案内容。";
    const a2 = "第二轮完全不同的答案。";
    const turn1 = collectText("msg_turn_1", a1);
    const turn2 = collectText("msg_turn_2", a2);

    expect(turn1.joined).toBe(a1);
    expect(turn2.joined).toBe(a2);
    expect(turn1.joined.split(a1).length - 1).toBe(1);
    expect(turn2.joined.split(a2).length - 1).toBe(1);
    // Neither consolidated re-emitted text.
    expect(types(turn1.consolidated)).toEqual(["ModelCallEndEvent"]);
    expect(types(turn2.consolidated)).toEqual(["ModelCallEndEvent"]);
    // Two turns, two usage increments.
    expect(t.getCumulativeUsage().input_tokens).toBe(200);
    expect(t.getCumulativeUsage().output_tokens).toBe(80);
  });
});

// ── Real CLI (SDK 0.1.77) shape ───────────────────────────────────────────────
// Most streaming tests above use the stop-first / one-aggregate-consolidated
// shape. The real CLI instead yields each content block's consolidated
// `assistant` message (that block alone, at content index 0, the turn's msg id)
// BEFORE the block's content_block_stop, with the block normalised: tool input
// re-parsed/coerced, whitespace-only text replaced by "(no content)".
describe("EventTranslator — real CLI shape (per-block consolidated before the stop)", () => {
  const consolidated = (
    id: string | undefined,
    block: AnyMsg,
    usage: AnyMsg = { input_tokens: 100, output_tokens: 20 },
  ): AnyMsg => ({
    type: "assistant",
    message: { ...(id ? { id } : {}), content: [block], usage },
  });
  const runAll = (t: EventTranslator, msgs: AnyMsg[]): EventPayload[] =>
    msgs.flatMap((m) => t.translateMessage(m));

  it("thinking → text → tool: one Start/End per call, End args = consolidated input, nothing re-emitted", () => {
    const t = new EventTranslator();
    const id = "msg_cli_1";
    const events = runAll(t, [
      msgStart(id),
      cbStart(0, { type: "thinking", thinking: "" }),
      cbDelta(0, { type: "thinking_delta", thinking: "plan: " }),
      cbDelta(0, { type: "thinking_delta", thinking: "list files" }),
      consolidated(id, { type: "thinking", thinking: "plan: list files" }),
      cbStop(0),
      cbStart(1, { type: "text", text: "" }),
      cbDelta(1, { type: "text_delta", text: "Listing " }),
      cbDelta(1, { type: "text_delta", text: "files." }),
      consolidated(id, { type: "text", text: "Listing files." }),
      cbStop(1),
      cbStart(2, { type: "tool_use", id: "toolu_1", name: "Bash", input: {} }),
      cbDelta(2, { type: "input_json_delta", partial_json: '{"command": "ls -la", ' }),
      cbDelta(2, { type: "input_json_delta", partial_json: '"timeout": "5000"}' }),
      // The CLI re-parses and coerces the input ("5000" -> 5000).
      consolidated(id, {
        type: "tool_use",
        id: "toolu_1",
        name: "Bash",
        input: { command: "ls -la", timeout: 5000 },
      }),
      cbStop(2),
      streamEvent({ type: "message_delta", delta: { stop_reason: "tool_use" }, usage: { output_tokens: 20 } }),
      msgStop(),
    ]);

    expect(types(events)).toEqual([
      "ThinkingBlockStartEvent",
      "ThinkingBlockDeltaEvent",
      "ThinkingBlockDeltaEvent",
      "ModelCallEndEvent", // the turn's first consolidated accounts its usage
      "ThinkingBlockEndEvent",
      "TextBlockStartEvent",
      "TextBlockDeltaEvent",
      "TextBlockDeltaEvent",
      "TextBlockEndEvent",
      "ToolCallStartEvent",
      "ToolCallDeltaEvent",
      "ToolCallDeltaEvent",
      "ToolCallEndEvent",
    ]);
    const end = events.find((e) => e.event_type === "ToolCallEndEvent")!;
    expect(end.tool_call_id).toBe("toolu_1");
    // The complete arguments the tool runs with, once.
    expect(end.args).toBe('{"command":"ls -la","timeout":5000}');
    // The streamed fragments are never re-appended (the doubled-args bug).
    const argDeltas = events
      .filter((e) => e.event_type === "ToolCallDeltaEvent")
      .map((e) => e.delta);
    expect(argDeltas.join("")).toBe('{"command": "ls -la", "timeout": "5000"}');
    expect(t.getCumulativeUsage()).toMatchObject({ input_tokens: 100, output_tokens: 20 });
  });

  it("a tool call surfaced again (second consolidated, replayed start, late delta) is never re-announced", () => {
    const t = new EventTranslator();
    const id = "msg_cli_2";
    runAll(t, [
      msgStart(id),
      cbStart(0, { type: "tool_use", id: "toolu_2", name: "Read", input: {} }),
      cbDelta(0, { type: "input_json_delta", partial_json: '{"file_path":"/a"}' }),
      consolidated(id, { type: "tool_use", id: "toolu_2", name: "Read", input: { file_path: "/a" } }),
      cbStop(0),
    ]);
    const again = runAll(t, [
      consolidated(id, { type: "tool_use", id: "toolu_2", name: "Read", input: { file_path: "/a" } }),
      cbStart(0, { type: "tool_use", id: "toolu_2", name: "Read", input: {} }),
      cbDelta(0, { type: "input_json_delta", partial_json: '{"file_path":"/a"}' }),
      cbStop(0),
    ]);
    expect(again.filter((e) => e.event_type.startsWith("ToolCall"))).toEqual([]);
  });

  it("text → tool → text → tool across two turns: every block once, in stream order", () => {
    const t = new EventTranslator();
    const turn = (id: string, text: string, toolId: string, cmd: string): AnyMsg[] => [
      msgStart(id),
      cbStart(0, { type: "text", text: "" }),
      cbDelta(0, { type: "text_delta", text }),
      consolidated(id, { type: "text", text }),
      cbStop(0),
      cbStart(1, { type: "tool_use", id: toolId, name: "Bash", input: {} }),
      cbDelta(1, { type: "input_json_delta", partial_json: JSON.stringify({ command: cmd }) }),
      consolidated(id, { type: "tool_use", id: toolId, name: "Bash", input: { command: cmd } }),
      cbStop(1),
      msgStop(),
      {
        type: "user",
        message: { content: [{ type: "tool_result", tool_use_id: toolId, content: `${cmd} ok` }] },
      },
    ];
    const events = runAll(t, [
      ...turn("msg_a", "First I list.", "toolu_a", "ls"),
      ...turn("msg_b", "Now I count.", "toolu_b", "wc -l"),
    ]);

    const seq = events
      .filter((e) =>
        ["TextBlockDeltaEvent", "ToolCallEndEvent", "ToolResultEndEvent"].includes(e.event_type),
      )
      .map((e) =>
        e.event_type === "TextBlockDeltaEvent"
          ? `text:${e.delta}`
          : `${e.event_type}:${e.tool_call_id}`,
      );
    expect(seq).toEqual([
      "text:First I list.",
      "ToolCallEndEvent:toolu_a",
      "ToolResultEndEvent:toolu_a",
      "text:Now I count.",
      "ToolCallEndEvent:toolu_b",
      "ToolResultEndEvent:toolu_b",
    ]);
    expect(events.filter((e) => e.event_type === "ToolCallStartEvent")).toHaveLength(2);
    expect(t.getCumulativeUsage().input_tokens).toBe(200);
  });

  it("a whitespace-only streamed text block is not re-emitted as \"(no content)\"", () => {
    const t = new EventTranslator();
    const id = "msg_ws";
    const events = runAll(t, [
      msgStart(id),
      cbStart(0, { type: "text", text: "" }),
      cbDelta(0, { type: "text_delta", text: "\n\n" }),
      consolidated(id, { type: "text", text: "(no content)" }),
      cbStop(0),
    ]);
    expect(
      events.filter((e) => e.event_type === "TextBlockDeltaEvent").map((e) => e.delta),
    ).toEqual(["\n\n"]);
  });

  it("two text blocks with the same text in one turn are matched one-to-one, in order", () => {
    const t = new EventTranslator();
    const id = "msg_same";
    const events = runAll(t, [
      msgStart(id),
      cbStart(0, { type: "text", text: "" }),
      cbDelta(0, { type: "text_delta", text: "OK." }),
      consolidated(id, { type: "text", text: "OK." }),
      cbStop(0),
      cbStart(1, { type: "tool_use", id: "toolu_s", name: "Bash", input: {} }),
      cbDelta(1, { type: "input_json_delta", partial_json: "{}" }),
      consolidated(id, { type: "tool_use", id: "toolu_s", name: "Bash", input: {} }),
      cbStop(1),
      cbStart(2, { type: "text", text: "" }),
      cbDelta(2, { type: "text_delta", text: "OK." }),
      consolidated(id, { type: "text", text: "OK." }),
      cbStop(2),
    ]);
    expect(
      events.filter((e) => e.event_type === "TextBlockDeltaEvent").map((e) => e.delta),
    ).toEqual(["OK.", "OK."]);
  });

  it("a consolidated message is only deduped against partials of the SAME message id", () => {
    const t = new EventTranslator();
    // Streamed id-less turn "Done."
    runAll(t, [
      msgStart(),
      cbStart(0, { type: "text", text: "" }),
      cbDelta(0, { type: "text_delta", text: "Done." }),
      cbStop(0),
    ]);
    // A different (non-streamed) message with the same text is real content.
    const other = t.translateMessage(consolidated("msg_other", { type: "text", text: "Done." }));
    expect(types(other)).toEqual(["TextBlockStartEvent", "TextBlockDeltaEvent", "ModelCallEndEvent"]);
    expect(other[1].delta).toBe("Done.");
  });

  it("usage: a partial snapshot on an early consolidated is topped up by message_delta, counted once", () => {
    const t = new EventTranslator();
    const id = "msg_usage";
    const early = { input_tokens: 500, output_tokens: 3, cache_read_input_tokens: 400 };
    const events = runAll(t, [
      msgStart(id),
      cbStart(0, { type: "thinking", thinking: "" }),
      cbDelta(0, { type: "thinking_delta", thinking: "hmm" }),
      consolidated(id, { type: "thinking", thinking: "hmm" }, early),
      cbStop(0),
      cbStart(1, { type: "text", text: "" }),
      cbDelta(1, { type: "text_delta", text: "answer" }),
      consolidated(id, { type: "text", text: "answer" }, early),
      cbStop(1),
      streamEvent({
        type: "message_delta",
        delta: { stop_reason: "end_turn" },
        usage: { input_tokens: 500, output_tokens: 42, cache_read_input_tokens: 400 },
      }),
      msgStop(),
    ]);
    expect(t.getCumulativeUsage()).toMatchObject({
      input_tokens: 500,
      output_tokens: 42,
      cache_read_input_tokens: 400,
    });
    // The ModelCallEndEvent increments sum to the cumulative usage.
    const inc = events.filter((e) => e.event_type === "ModelCallEndEvent");
    expect(inc.reduce((s, e) => s + Number(e.input_tokens), 0)).toBe(500);
    expect(inc.reduce((s, e) => s + Number(e.output_tokens), 0)).toBe(42);
    expect(inc.reduce((s, e) => s + Number(e.cache_read_input_tokens), 0)).toBe(400);
  });

  it("an API-error assistant message becomes ApiErrorEvent only (its raw text is never emitted)", () => {
    const t = new EventTranslator();
    const out = t.translateMessage({
      type: "assistant",
      error: "rate_limit",
      message: {
        id: "msg_err",
        content: [{ type: "text", text: 'API Error: 429 {"detail":"tenant t-1 over quota"}' }],
        usage: { input_tokens: 0, output_tokens: 0 },
      },
    } as AnyMsg);
    expect(out).toEqual([{ event_type: "ApiErrorEvent", error: "rate_limit" }]);
  });

  it("a code-less synthetic \"API Error\" notice is an ApiErrorEvent too; real model text is not", () => {
    const t = new EventTranslator();
    const notice = t.translateMessage({
      type: "assistant",
      message: {
        id: "b2c1",
        model: "<synthetic>",
        content: [
          {
            type: "text",
            text: "API Error: Claude's response exceeded the 32000 output token maximum. To configure this behavior, set the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable.",
          },
        ],
        usage: { input_tokens: 0, output_tokens: 0 },
      },
    } as AnyMsg);
    expect(notice).toEqual([{ event_type: "ApiErrorEvent", error: "unknown" }]);

    // The model itself writing "API Error…" is ordinary answer text.
    const real = t.translateMessage({
      type: "assistant",
      message: {
        id: "msg_real",
        model: "deepseek-chat",
        content: [{ type: "text", text: "API Error codes are listed below." }],
        usage: { input_tokens: 5, output_tokens: 5 },
      },
    } as AnyMsg);
    expect(types(real)).toEqual(["TextBlockStartEvent", "TextBlockDeltaEvent", "ModelCallEndEvent"]);
  });

  it("result success with is_error=true is an ErrorEvent that does not copy the raw text", () => {
    const t = new EventTranslator();
    expect(
      t.translateMessage({
        type: "result",
        subtype: "success",
        is_error: true,
        result: "API Error: 500 upstream 10.0.0.7",
      } as AnyMsg),
    ).toEqual([{ event_type: "ErrorEvent", message: "model API error", subtype: "success" }]);
    expect(t.translateMessage({ type: "result", subtype: "error_max_turns" } as AnyMsg)).toEqual([
      {
        event_type: "ErrorEvent",
        message: "result subtype error_max_turns",
        subtype: "error_max_turns",
      },
    ]);
  });

  it("flags a non-streamed thinking block whole_block; streamed thinking deltas are live chunks", () => {
    const t = new EventTranslator();
    const whole = t.translateMessage(
      consolidated("msg_ns", { type: "thinking", thinking: "all at once" }),
    );
    const wholeDelta = whole.find((e) => e.event_type === "ThinkingBlockDeltaEvent")!;
    expect(wholeDelta.whole_block).toBe(true);
    expect(wholeDelta.delta).toBe("all at once");

    t.translateMessage(msgStart("msg_s"));
    t.translateMessage(cbStart(0, { type: "thinking", thinking: "" }));
    const live = t.translateMessage(cbDelta(0, { type: "thinking_delta", thinking: "chunk" }));
    expect(live[0].whole_block).toBeUndefined();
  });
});
