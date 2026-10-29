/**
 * event_translator.ts — Claude SDK message stream → platform event vocabulary.
 *
 * The scheduler / orchestrator SSE bridge / frontend consume events.jsonl using
 * a fixed event_type vocabulary (see contracts/worker-wire-contract.md). The
 * names were inherited from the retired AgentScope worker and are kept for
 * wire compatibility, so the SDK's `SDKMessage` stream is re-shaped into them.
 *
 * One SDKMessage produces 0..N EventPayloads. The translator is
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
 *
 * Event fields added on top of the inherited vocabulary:
 *   - ToolCallEndEvent.args — the complete JSON args string for the call (the
 *     consolidated, CLI-normalised input when available, else the streamed
 *     input_json_delta concatenation). Emitted AT MOST ONCE per tool_call_id.
 *   - ThinkingBlockDeltaEvent.whole_block — true when the delta is a whole
 *     non-streamed block (from a consolidated message), absent for live chunks.
 *   - ApiErrorEvent {error} — the CLI surfaced an API failure as a synthetic
 *     assistant message ("API Error: …"); `error` is the SDK's error code
 *     ("unknown" when the notice has none). Its raw text is deliberately NOT
 *     forwarded as answer text.
 *   - ErrorEvent.subtype — the SDK result subtype behind a failed run.
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

/**
 * The CLI replaces a whitespace-only text block with this placeholder in the
 * consolidated assistant message (cli.js `ML`), so the streamed and
 * consolidated texts of that block differ.
 */
const NO_CONTENT_PLACEHOLDER = "(no content)";

/** `model` of the assistant messages the CLI makes up itself (cli.js `dWA`). */
const SYNTHETIC_MODEL = "<synthetic>";
/** Prefix of the CLI's API-error notices (cli.js `TV`). */
const API_ERROR_PREFIX = "API Error";

/**
 * A CLI-made API-error notice (never model output: the model cannot produce a
 * "<synthetic>" message), whether or not it carries an SDK error code.
 */
function isSyntheticApiErrorNotice(message: Json): boolean {
  if (message?.model !== SYNTHETIC_MODEL) return false;
  const content: Json[] = Array.isArray(message?.content) ? message.content : [];
  return content.some(
    (b: Json) =>
      b?.type === "text" &&
      typeof b.text === "string" &&
      b.text.startsWith(API_ERROR_PREFIX),
  );
}

/** One text/thinking content block streamed this turn, in stream order. */
interface StreamedBlock {
  kind: "text" | "thinking";
  text: string;
  /** Matched by a consolidated assistant block already (never re-matched). */
  consumed: boolean;
}

interface UsageSnapshot {
  input: number;
  output: number;
  cacheRead: number;
}

export class EventTranslator {
  /** blockIndex (within one assistant message) -> minted uuid. */
  private textBlockIds = new Map<number, string>();
  /** blockIndex (within one assistant message) -> minted uuid. */
  private thinkingBlockIds = new Map<number, string>();
  /** tool_call_id -> tool name, remembered from ToolCallStart for the result. */
  private toolNames = new Map<string, string>();

  // ── streaming (includePartialMessages) bookkeeping ─────────────────────────
  // The SDK's `stream_event` partials carry a per-content-block `index`. We map
  // each index to the block id / tool_call_id we minted on content_block_start
  // so the matching content_block_delta / _stop events can resolve it. These are
  // reset at every `message_start` (a fresh assistant turn).
  /** streaming content-block index -> minted text block uuid. */
  private streamTextBlockIds = new Map<number, string>();
  /** streaming content-block index -> minted thinking block uuid. */
  private streamThinkingBlockIds = new Map<number, string>();
  /** streaming content-block index -> tool_call_id (from content_block_start). */
  private streamToolCallIds = new Map<number, string>();
  /** tool_call_id -> streamed JSON args, while the call is still open. */
  private streamedToolArgs = new Map<string, string>();
  /**
   * Run-wide tool-call bookkeeping, keyed on the globally unique tool_use id:
   * ids whose ToolCallStartEvent / ToolCallEndEvent were already emitted. A
   * tool call is announced and closed exactly once no matter how many times the
   * SDK surfaces it (partials + per-block consolidated message).
   */
  private startedToolCalls = new Set<string>();
  private endedToolCalls = new Set<string>();

  // ── consolidated-assistant dedup (per turn, keyed on SDK message id) ───────
  // Ground truth (sdk-messages-sample.jsonl + cli.js): under
  // includePartialMessages the CLI emits ONE `message_start` per turn, streams
  // the content blocks, and delivers a SEPARATE consolidated `assistant`
  // message for EACH content block. Each one is emitted when the CLI sees that
  // block's content_block_stop, so it reaches us BEFORE the matching
  // `stream_event` content_block_stop, puts its single block at content-array
  // index 0 (whatever the streaming index was), and shares the turn's `msg_…`
  // id with the message_start. The CLI also normalises the block: tool_use
  // input is re-parsed/coerced, a whitespace-only text becomes "(no content)".
  //
  // Consequences:
  //   (a) no index-keyed dedup: the consolidated index is always 0;
  //   (b) tool calls dedup on their unique id (start/end sets above);
  //   (c) text/thinking dedup matches the consolidated block against this
  //       turn's streamed blocks of the same kind in stream order (first
  //       unconsumed block whose text agrees) — only within the SAME message
  //       id, so a consolidated message with an id never matches partials of a
  //       different (or id-less) message;
  //   (d) records are never cleared per consolidated message (one turn yields
  //       several); they reset only when a message_start opens a turn with a
  //       different message id.
  /** SDK message id of the turn currently being streamed (from message_start). */
  private streamingMessageId: string | null = null;
  /** Text/thinking blocks streamed under streamingMessageId, in order. */
  private turnBlocks: StreamedBlock[] = [];
  /** Streaming content-block index -> its turnBlocks entry (per message). */
  private streamBlockByIndex = new Map<number, StreamedBlock>();
  /**
   * Highest usage snapshot seen per SDK message id. Every per-block
   * consolidated message of a turn repeats a usage snapshot (which may be
   * partial for a block closed mid-stream), and the turn's message_delta
   * carries the final one; taking the per-field max and accounting only the
   * growth counts each turn exactly once.
   */
  private usageById = new Map<string, UsageSnapshot>();

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
        // includePartialMessages=true: incremental Anthropic raw streaming
        // events. Translate them into per-token agentscope deltas.
        return this.translateStreamEvent(msg as Json);
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

  // ── type:"stream_event" (includePartialMessages) ───────────────────────────
  /**
   * Translate one Anthropic raw streaming event (carried on msg.event) into 0..N
   * incremental agentscope events. This is the per-token path: each text_delta
   * becomes one TextBlockDeltaEvent, each input_json_delta one ToolCallDeltaEvent.
   *
   * The companion `translateAssistant` skips re-emitting any text/thinking block
   * that was streamed here (see consumeStreamedBlock) and closes each tool call
   * exactly once (see consolidatedToolUse), so nothing is duplicated in Redis.
   * message_delta usage feeds the per-message-id max snapshot (observeUsage).
   */
  private translateStreamEvent(msg: Json): EventPayload[] {
    const ev: Json = msg.event ?? {};
    switch (ev.type) {
      case "message_start": {
        // Per-index partial maps are scoped to one streamed message, so reset
        // them. The id of the message being streamed becomes the dedup key the
        // per-block consolidated `assistant` messages are matched against.
        //
        // Reset the turn-scoped dedup records ONLY when this message_start opens
        // a genuinely NEW turn — its message id differs from the one we are
        // currently tracking — never on a consolidated message (one turn yields
        // several, which must all still see their blocks as already-streamed).
        this.streamTextBlockIds.clear();
        this.streamThinkingBlockIds.clear();
        this.streamToolCallIds.clear();
        this.streamBlockByIndex.clear();
        const id = this.messageIdOf(ev.message);
        if (id !== this.streamingMessageId) {
          this.turnBlocks = [];
        }
        this.streamingMessageId = id;
        return [];
      }
      case "content_block_start": {
        const index = Number(ev.index ?? 0);
        const block: Json = ev.content_block ?? {};
        const kind = block?.type;
        if (kind === "text") {
          const blockId = randomUUID();
          this.streamTextBlockIds.set(index, blockId);
          this.openStreamedBlock(index, "text");
          return [{ event_type: "TextBlockStartEvent", block_id: blockId }];
        }
        if (kind === "thinking") {
          const blockId = randomUUID();
          this.streamThinkingBlockIds.set(index, blockId);
          this.openStreamedBlock(index, "thinking");
          return [{ event_type: "ThinkingBlockStartEvent", block_id: blockId }];
        }
        if (kind === "tool_use") {
          const toolCallId = String(block.id ?? "");
          const toolName = String(block.name ?? "tool");
          this.streamToolCallIds.set(index, toolCallId);
          if (toolCallId) {
            this.toolNames.set(toolCallId, toolName);
            // A replayed start for a call we already announced emits nothing.
            if (this.startedToolCalls.has(toolCallId)) return [];
            this.startedToolCalls.add(toolCallId);
            this.streamedToolArgs.set(toolCallId, "");
          }
          return [
            {
              event_type: "ToolCallStartEvent",
              tool_call_id: toolCallId,
              tool_call_name: toolName,
            },
          ];
        }
        return [];
      }
      case "content_block_delta": {
        const index = Number(ev.index ?? 0);
        const delta: Json = ev.delta ?? {};
        if (delta.type === "text_delta") {
          const blockId = this.streamTextBlockIds.get(index) ?? randomUUID();
          if (!this.streamTextBlockIds.has(index)) {
            this.streamTextBlockIds.set(index, blockId);
          }
          // Record even if content_block_start was missed (defensive) and feed
          // the per-turn block record that translateAssistant matches against.
          const text = String(delta.text ?? "");
          this.appendStreamedBlockText(index, "text", text);
          return [
            {
              event_type: "TextBlockDeltaEvent",
              block_id: blockId,
              delta: text,
            },
          ];
        }
        if (delta.type === "thinking_delta") {
          const blockId = this.streamThinkingBlockIds.get(index) ?? randomUUID();
          if (!this.streamThinkingBlockIds.has(index)) {
            this.streamThinkingBlockIds.set(index, blockId);
          }
          const text = String(delta.thinking ?? "");
          this.appendStreamedBlockText(index, "thinking", text);
          return [
            {
              event_type: "ThinkingBlockDeltaEvent",
              block_id: blockId,
              delta: text,
            },
          ];
        }
        if (delta.type === "input_json_delta") {
          const toolCallId = this.streamToolCallIds.get(index) ?? "";
          const partial = String(delta.partial_json ?? "");
          if (toolCallId) {
            // A call that is already closed never grows again.
            if (this.endedToolCalls.has(toolCallId)) return [];
            this.streamedToolArgs.set(
              toolCallId,
              (this.streamedToolArgs.get(toolCallId) ?? "") + partial,
            );
          }
          return [
            {
              event_type: "ToolCallDeltaEvent",
              tool_call_id: toolCallId,
              delta: partial,
            },
          ];
        }
        return [];
      }
      case "content_block_stop": {
        const index = Number(ev.index ?? 0);
        // Real CLI order: the block's consolidated assistant message (handled
        // in translateAssistant) arrives BEFORE this stop and already closed
        // the call, so this is normally a no-op. When the stop comes first and
        // arguments were streamed, close the call with the streamed args;
        // without streamed args wait for the consolidated message, which
        // carries the complete input for SDKs that omit input_json_delta.
        if (this.streamToolCallIds.has(index)) {
          const toolCallId = this.streamToolCallIds.get(index) ?? "";
          if (!toolCallId || this.endedToolCalls.has(toolCallId)) return [];
          const toolArgs = this.streamedToolArgs.get(toolCallId) ?? "";
          if (!toolArgs) return [];
          return [this.endToolCall(toolCallId, toolArgs)];
        }
        if (this.streamTextBlockIds.has(index)) {
          const blockId = this.streamTextBlockIds.get(index) ?? "";
          return [{ event_type: "TextBlockEndEvent", block_id: blockId }];
        }
        if (this.streamThinkingBlockIds.has(index)) {
          const blockId = this.streamThinkingBlockIds.get(index) ?? "";
          return [{ event_type: "ThinkingBlockEndEvent", block_id: blockId }];
        }
        return [];
      }
      case "message_delta":
        // The turn's final usage. Folded into the per-id max snapshot so a
        // consolidated message carrying a partial snapshot (a block closed
        // mid-stream) cannot under-count the turn; never summed on top of the
        // consolidated usage. Id-less partials cannot be correlated: ignored.
        if (this.streamingMessageId === null) return [];
        return this.observeUsage(this.streamingMessageId, ev.usage);
      case "message_stop":
      default:
        return [];
    }
  }

  // ── type:"assistant" ───────────────────────────────────────────────────────
  private translateAssistant(msg: Json): EventPayload[] {
    const out: EventPayload[] = [];
    // A fresh assistant message starts a fresh set of text/thinking blocks.
    this.textBlockIds.clear();
    this.thinkingBlockIds.clear();

    const msgId = this.messageIdOf(msg.message);

    // The CLI surfaces an API failure (auth, billing, rate limit, gateway 5xx…)
    // as a synthetic assistant message whose text is "API Error: <raw upstream
    // message>" and whose `error` carries the SDK error code. That text is not
    // an answer and can leak gateway internals, so it is never forwarded as
    // text; the worker maps the code to a user-safe message instead.
    if (typeof msg.error === "string" && msg.error.length > 0) {
      return [{ event_type: "ApiErrorEvent", error: msg.error }];
    }
    // Some of those notices carry no code (output-token cap — after which the
    // CLI asks the model to continue — or context window exhausted). They are
    // still recognisable: synthetic (model "<synthetic>") and "API Error"-led.
    if (isSyntheticApiErrorNotice(msg.message)) {
      return [{ event_type: "ApiErrorEvent", error: "unknown" }];
    }

    // Per-block dedup (see the bookkeeping doc above): a text/thinking block
    // that was streamed this turn is skipped (already in Redis); otherwise the
    // full block is emitted — the non-streamed path where the model returned a
    // complete assistant message with no partials at all. Tool calls are
    // closed exactly once by id.
    const content: Json[] = msg.message?.content ?? [];
    content.forEach((block: Json, index: number) => {
      const kind = block?.type;
      if (kind === "text") {
        const text = String(block.text ?? "");
        if (this.consumeStreamedBlock(msgId, "text", text)) return;
        const blockId = this.mintTextBlockId(index);
        out.push({ event_type: "TextBlockStartEvent", block_id: blockId });
        out.push({
          event_type: "TextBlockDeltaEvent",
          block_id: blockId,
          delta: text,
        });
      } else if (kind === "tool_use") {
        out.push(...this.consolidatedToolUse(block));
      } else if (kind === "thinking") {
        const text = String(block.thinking ?? "");
        if (this.consumeStreamedBlock(msgId, "thinking", text)) return;
        const blockId = this.mintThinkingBlockId(index);
        out.push({ event_type: "ThinkingBlockStartEvent", block_id: blockId });
        out.push({
          event_type: "ThinkingBlockDeltaEvent",
          block_id: blockId,
          delta: text,
          // A whole non-streamed block, not a live chunk (worker pushes a
          // single `thinking` entry for it).
          whole_block: true,
        });
        out.push({ event_type: "ThinkingBlockEndEvent", block_id: blockId });
      }
      // image / other blocks are ignored in v1.
    });

    // Usage: per-id max snapshot, accounting only growth (see usageById).
    out.push(...this.observeUsage(msgId, msg.message?.usage));

    return out;
  }

  /**
   * The consolidated copy of a tool_use block. Emits nothing for a call that
   * is already closed. For a call announced by the partials it emits only the
   * closing ToolCallEndEvent (plus the arguments when no input_json_delta was
   * streamed); a never-streamed call gets the full Start/Delta/End triple.
   * The End's `args` is the consolidated input — the CLI-normalised arguments
   * the tool actually runs with — and the partial args are never re-appended.
   */
  private consolidatedToolUse(block: Json): EventPayload[] {
    const toolCallId = String(block.id ?? "");
    const toolName = String(block.name ?? "tool");
    const argsJson = JSON.stringify(block.input ?? {});
    if (toolCallId) {
      this.toolNames.set(toolCallId, toolName);
      if (this.endedToolCalls.has(toolCallId)) return [];
    }
    const out: EventPayload[] = [];
    if (!toolCallId || !this.startedToolCalls.has(toolCallId)) {
      if (toolCallId) this.startedToolCalls.add(toolCallId);
      out.push({
        event_type: "ToolCallStartEvent",
        tool_call_id: toolCallId,
        tool_call_name: toolName,
      });
      out.push({
        event_type: "ToolCallDeltaEvent",
        tool_call_id: toolCallId,
        delta: argsJson,
      });
    } else if (!this.streamedToolArgs.get(toolCallId)) {
      // Announced by a content_block_start but no input_json_delta streamed:
      // the consolidated input is the only copy of the arguments.
      out.push({
        event_type: "ToolCallDeltaEvent",
        tool_call_id: toolCallId,
        delta: argsJson,
      });
    }
    out.push(this.endToolCall(toolCallId, argsJson));
    return out;
  }

  /** Build the single ToolCallEndEvent for a call and mark it closed. */
  private endToolCall(toolCallId: string, args: string): EventPayload {
    if (toolCallId) {
      this.endedToolCalls.add(toolCallId);
      this.streamedToolArgs.delete(toolCallId);
    }
    return { event_type: "ToolCallEndEvent", tool_call_id: toolCallId, args };
  }

  /** Open a streamed text/thinking block record for the current turn. */
  private openStreamedBlock(index: number, kind: "text" | "thinking"): StreamedBlock {
    const block: StreamedBlock = { kind, text: "", consumed: false };
    this.turnBlocks.push(block);
    this.streamBlockByIndex.set(index, block);
    return block;
  }

  /** Append streamed text to the block at `index` (opening it if missed). */
  private appendStreamedBlockText(
    index: number,
    kind: "text" | "thinking",
    text: string,
  ): void {
    let block = this.streamBlockByIndex.get(index);
    if (!block || block.kind !== kind) {
      block = this.openStreamedBlock(index, kind);
    }
    block.text += text;
  }

  /**
   * Was this consolidated text/thinking block already streamed? Matches it to
   * the first not-yet-matched block of the same kind streamed under the SAME
   * message id whose text agrees (equal, the streamed text extends it, or the
   * CLI's whitespace-only "(no content)" placeholder), and marks that block
   * matched so it is never matched twice. A message id that differs from the
   * streamed turn's (a non-streamed message, or id vs id-less) never matches,
   * so the full block is emitted rather than silently dropped.
   */
  private consumeStreamedBlock(
    messageId: string | null,
    kind: "text" | "thinking",
    text: string,
  ): boolean {
    if (messageId !== this.streamingMessageId) return false;
    for (const block of this.turnBlocks) {
      if (block.kind !== kind || block.consumed) continue;
      if (sameBlockText(block.text, text)) {
        block.consumed = true;
        return true;
      }
    }
    return false;
  }

  /** Extract the `msg_…` id from a consolidated message or a partial's
   * message_start envelope. Returns null when absent (triggers the fallback). */
  private messageIdOf(message: Json): string | null {
    const id = message?.id;
    return typeof id === "string" && id.length > 0 ? id : null;
  }

  /**
   * Fold one usage snapshot of message `msgId` into the cumulative total and
   * return a ModelCallEndEvent carrying the INCREMENT, or nothing when the
   * snapshot adds nothing. Every per-block consolidated message of a turn
   * repeats a snapshot (real SDK: thinking + text consolidated both carry
   * in=13154/out=117) and message_delta carries the final one, so per id we
   * keep the per-field max and account only its growth: a turn is counted
   * once, and a snapshot taken mid-stream (a block closed early) cannot
   * under-count it. The ModelCallEndEvent increments of a run sum to the
   * cumulative usage (what the orchestrator aggregates). When no id is present
   * (leaner SDK shape) snapshots cannot be correlated, so each is counted as-is
   * — the legacy one-consolidated-per-turn behaviour.
   */
  private observeUsage(msgId: string | null, usage: Json): EventPayload[] {
    const cur = readUsage(usage);
    let inc: UsageSnapshot = cur;
    if (msgId !== null) {
      const prev = this.usageById.get(msgId) ?? { input: 0, output: 0, cacheRead: 0 };
      const next: UsageSnapshot = {
        input: Math.max(prev.input, cur.input),
        output: Math.max(prev.output, cur.output),
        cacheRead: Math.max(prev.cacheRead, cur.cacheRead),
      };
      this.usageById.set(msgId, next);
      inc = {
        input: next.input - prev.input,
        output: next.output - prev.output,
        cacheRead: next.cacheRead - prev.cacheRead,
      };
    }
    if (inc.input === 0 && inc.output === 0 && inc.cacheRead === 0) return [];
    this.cumulative.input_tokens += inc.input;
    this.cumulative.output_tokens += inc.output;
    this.cumulative.cache_read_input_tokens += inc.cacheRead;
    return [
      {
        event_type: "ModelCallEndEvent",
        input_tokens: inc.input,
        output_tokens: inc.output,
        cache_read_input_tokens: inc.cacheRead,
      },
    ];
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
    // The CLI reports subtype "success" with is_error=true when the turn ended
    // on an API error (its result text is the raw "API Error: …" message).
    if (msg.subtype === "success" && msg.is_error !== true) {
      return [{ event_type: "ReplyEndEvent" }];
    }
    // error_during_execution / error_max_turns / error_max_budget_usd / ...
    const errors: string[] = Array.isArray(msg.errors) ? msg.errors : [];
    const subtype = String(msg.subtype ?? "unknown");
    let message: string;
    if (errors.length > 0) {
      message = errors.join("; ");
    } else if (msg.subtype === "success") {
      // Do not copy the raw upstream error text (may echo gateway internals).
      message = "model API error";
    } else {
      message = `result subtype ${subtype}`;
    }
    return [{ event_type: "ErrorEvent", message, subtype }];
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

/** Read an Anthropic usage object; absent/garbage fields count as 0. */
function readUsage(usage: Json): UsageSnapshot {
  const u: Json = usage ?? {};
  return {
    input: Number(u.input_tokens ?? 0) || 0,
    output: Number(u.output_tokens ?? 0) || 0,
    // DeepSeek prefix-cache hits, mapped by the adapter onto Anthropic's
    // cache_read_input_tokens. Failure-open: absent => 0.
    cacheRead: Number(u.cache_read_input_tokens ?? 0) || 0,
  };
}

/**
 * Does a consolidated block's text agree with a streamed block's text? Equal;
 * the streamed text extends it (snapshot taken before the last delta); or the
 * streamed block was whitespace-only, which the CLI consolidates as "(no
 * content)".
 */
function sameBlockText(streamed: string, consolidated: string): boolean {
  if (streamed === consolidated) return true;
  if (streamed.trim() === "") {
    return consolidated.trim() === "" || consolidated === NO_CONTENT_PLACEHOLDER;
  }
  return consolidated.length > 0 && streamed.startsWith(consolidated);
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
