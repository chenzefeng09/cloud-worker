/**
 * event_translator.ts — Claude SDK message stream → agentscope event vocabulary.
 *
 * The scheduler / orchestrator SSE bridge / frontend consume events.jsonl using
 * the SAME event_type vocabulary the AgentScope runtime_worker emits (see
 * agentscope-ext/tests/test_runtime_worker.py). A Claude SDK run must therefore
 * re-shape the SDK's `SDKMessage` stream into that exact vocabulary so the two
 * runtimes are interchangeable upstream.
 *
 * One SDKMessage produces 0..N agentscope EventPayloads. The translator is
 * stateful (a class, not a pure function) for two reasons:
 *
 *   1. SDK content blocks carry no stable id for text / thinking blocks, so we
 *      mint a uuid per (message, blockIndex) and remember it.
 *   2. Tool-result messages reference a tool_use_id but not the tool name; we
 *      remember name-per-id from the preceding ToolCall events.
 *
 * usage fix (B.6): the SDKResultMessage.usage can be 0 (the spike's `usage in=0`
 * root cause). We instead accumulate `message.message.usage` from every
 * assistant message into `cumulativeUsage`, expose it via getCumulativeUsage(),
 * and the worker writes THAT into result.json.
 */

import { randomUUID } from "node:crypto";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";

/** A single agentscope event line. `event_type` is the agentscope class name. */
export interface EventPayload {
  event_type: string;
  [k: string]: unknown;
}

/** Accumulated token usage across all model calls in a run. */
export interface CumulativeUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  /**
   * DeepSeek prefix-cache hit observability: prompt tokens served from the
   * automatic prefix cache, summed across all model calls. The adapter maps
   * DeepSeek's `prompt_cache_hit_tokens` onto Anthropic's
   * `cache_read_input_tokens`, which is what we read here.
   */
  cache_read_input_tokens: number;
}

// The SDK's content blocks are dynamic Anthropic JSON; we narrow as we go.
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic wire JSON
type Json = any;

export class EventTranslator {
  /** blockIndex (within one assistant message) -> minted uuid. */
  private textBlockIds = new Map<number, string>();
  /** blockIndex (within one assistant message) -> minted uuid. */
  private thinkingBlockIds = new Map<number, string>();
  /** tool_call_id -> tool name, remembered from ToolCallStart for the result. */
  private toolNames = new Map<string, string>();

  private cumulative: CumulativeUsage = {
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_input_tokens: 0,
  };

  /** Cumulative usage accumulated across all assistant messages so far. */
  getCumulativeUsage(): CumulativeUsage {
    return {
      input_tokens: this.cumulative.input_tokens,
      output_tokens: this.cumulative.output_tokens,
      total_tokens: this.cumulative.input_tokens + this.cumulative.output_tokens,
      cache_read_input_tokens: this.cumulative.cache_read_input_tokens,
    };
  }

  /** Tool name previously seen for a tool_call_id (or "tool"). */
  toolNameFor(toolCallId: string): string {
    return this.toolNames.get(toolCallId) ?? "tool";
  }

  /**
   * Translate one SDK message into 0..N agentscope events. Side effect: updates
   * the cumulative usage accumulator from assistant messages.
   */
  translateMessage(msg: SDKMessage): EventPayload[] {
    switch (msg.type) {
      case "assistant":
        return this.translateAssistant(msg as Json);
      case "user":
        return this.translateUser(msg as Json);
      case "result":
        return this.translateResult(msg as Json);
      case "system":
        return this.translateSystem(msg as Json);
      case "stream_event":
        // adapter forces stream:false, so this should not appear in v1; emit a
        // tolerant UnknownEvent rather than throwing if it ever does.
        return [{ event_type: "UnknownEvent", message_type: "stream_event" }];
      default:
        // tool_progress / auth_status / hook_response / status / etc. — surface
        // as a custom event that downstream can ignore.
        return [
          {
            event_type: "ToolProgressEvent",
            message_type: (msg as Json).type,
            subtype: (msg as Json).subtype ?? null,
          },
        ];
    }
  }

  // ── type:"assistant" ───────────────────────────────────────────────────────
  private translateAssistant(msg: Json): EventPayload[] {
    const out: EventPayload[] = [];
    // A fresh assistant message starts a fresh set of text/thinking blocks.
    this.textBlockIds.clear();
    this.thinkingBlockIds.clear();

    const content: Json[] = msg.message?.content ?? [];
    content.forEach((block: Json, index: number) => {
      const kind = block?.type;
      if (kind === "text") {
        const blockId = this.mintTextBlockId(index);
        out.push({ event_type: "TextBlockStartEvent", block_id: blockId });
        out.push({
          event_type: "TextBlockDeltaEvent",
          block_id: blockId,
          delta: String(block.text ?? ""),
        });
      } else if (kind === "tool_use") {
        const toolCallId = String(block.id ?? "");
        const toolName = String(block.name ?? "tool");
        if (toolCallId) this.toolNames.set(toolCallId, toolName);
        out.push({
          event_type: "ToolCallStartEvent",
          tool_call_id: toolCallId,
          tool_call_name: toolName,
        });
        out.push({
          event_type: "ToolCallDeltaEvent",
          tool_call_id: toolCallId,
          delta: JSON.stringify(block.input ?? {}),
        });
        out.push({ event_type: "ToolCallEndEvent", tool_call_id: toolCallId });
      } else if (kind === "thinking") {
        const blockId = this.mintThinkingBlockId(index);
        out.push({ event_type: "ThinkingBlockStartEvent", block_id: blockId });
        out.push({
          event_type: "ThinkingBlockDeltaEvent",
          block_id: blockId,
          delta: String(block.thinking ?? ""),
        });
        out.push({ event_type: "ThinkingBlockEndEvent", block_id: blockId });
      }
      // image / other blocks are ignored in v1.
    });

    // Accumulate usage from this assistant turn and emit the per-turn increment.
    const usage: Json = msg.message?.usage ?? {};
    const input = Number(usage.input_tokens ?? 0) || 0;
    const output = Number(usage.output_tokens ?? 0) || 0;
    // DeepSeek prefix-cache hits, mapped by the adapter onto Anthropic's
    // cache_read_input_tokens. Failure-open: absent => 0.
    const cacheRead = Number(usage.cache_read_input_tokens ?? 0) || 0;
    this.cumulative.input_tokens += input;
    this.cumulative.output_tokens += output;
    this.cumulative.cache_read_input_tokens += cacheRead;
    out.push({
      event_type: "ModelCallEndEvent",
      input_tokens: input,
      output_tokens: output,
      cache_read_input_tokens: cacheRead,
    });

    return out;
  }

  // ── type:"user" (tool results) ─────────────────────────────────────────────
  private translateUser(msg: Json): EventPayload[] {
    const out: EventPayload[] = [];
    const content = msg.message?.content;
    if (!Array.isArray(content)) return out;

    for (const block of content as Json[]) {
      if (block?.type !== "tool_result") continue;
      const toolCallId = String(block.tool_use_id ?? "");
      const toolName = this.toolNameFor(toolCallId);
      const text = flattenToolResultContent(block.content);
      const state = block.is_error ? "error" : "success";

      out.push({
        event_type: "ToolResultStartEvent",
        tool_call_id: toolCallId,
        tool_call_name: toolName,
      });
      out.push({
        event_type: "ToolResultTextDeltaEvent",
        tool_call_id: toolCallId,
        delta: text,
      });
      out.push({
        event_type: "ToolResultEndEvent",
        tool_call_id: toolCallId,
        state,
      });
    }
    return out;
  }

  // ── type:"result" ──────────────────────────────────────────────────────────
  private translateResult(msg: Json): EventPayload[] {
    if (msg.subtype === "success") {
      return [{ event_type: "ReplyEndEvent" }];
    }
    // error_during_execution / error_max_turns / error_max_budget_usd / ...
    const errors: string[] = Array.isArray(msg.errors) ? msg.errors : [];
    const message =
      errors.length > 0 ? errors.join("; ") : `result subtype ${msg.subtype}`;
    return [{ event_type: "ErrorEvent", message }];
  }

  // ── type:"system" ──────────────────────────────────────────────────────────
  private translateSystem(msg: Json): EventPayload[] {
    if (msg.subtype === "init") {
      // init carries tool/mcp/skill inventory; log-only, no event written.
      console.log(
        `[event_translator] system init: model=${msg.model} tools=${(msg.tools ?? []).length} mcp=${(msg.mcp_servers ?? []).length}`,
      );
      return [];
    }
    // compact_boundary / status / hook_response — emit for debugging.
    return [
      { event_type: "UnknownSystemEvent", subtype: msg.subtype ?? "unknown" },
    ];
  }

  private mintTextBlockId(index: number): string {
    let id = this.textBlockIds.get(index);
    if (!id) {
      id = randomUUID();
      this.textBlockIds.set(index, id);
    }
    return id;
  }

  private mintThinkingBlockId(index: number): string {
    let id = this.thinkingBlockIds.get(index);
    if (!id) {
      id = randomUUID();
      this.thinkingBlockIds.set(index, id);
    }
    return id;
  }
}

/**
 * A tool_result block's `content` is a string OR an array of content blocks
 * ({type:"text",text} | image | ...). Flatten to plain text for the event delta.
 */
function flattenToolResultContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return (content as Json[])
      .map((b: Json) => {
        if (b?.type === "text" && typeof b.text === "string") return b.text;
        if (typeof b === "string") return b;
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}
