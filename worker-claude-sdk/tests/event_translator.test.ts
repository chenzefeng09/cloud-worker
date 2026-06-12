/**
 * event_translator.test.ts — SDK message stream → agentscope event vocabulary.
 *
 * Feeds constructed SDKMessage sequences and asserts the event_type order,
 * fields, cumulative usage accumulation, and ReplyEndEvent-on-success.
 */

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
