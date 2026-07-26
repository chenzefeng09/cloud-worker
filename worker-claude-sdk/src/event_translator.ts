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
  /** tool_call_id -> streamed JSON args, when the SDK emits input deltas. */
  private streamedToolArgs = new Map<string, string>();
  /** Tool calls whose end event has already been emitted. */
  private streamedToolCallEnds = new Set<string>();

  // ── consolidated-assistant dedup (per turn, keyed on SDK message id) ───────
  // Ground truth (worker-claude-sdk/sdk-messages-sample.jsonl, a real
  // deepseek-v4-pro turn): under includePartialMessages the SDK emits ONE
  // `message_start` at the top of the turn, then streams content blocks, and
  // delivers a SEPARATE consolidated `assistant` message for EACH content block
  // — interleaved with the partials, NOT a single aggregate at the end (the
  // thinking-block consolidated arrives while text is still streaming). Every
  // such per-block consolidated puts its one block at content-array index 0,
  // regardless of the streaming content-block `index` (thinking streamed at
  // index 0, text at index 1, but both consolidate at array index 0). All of
  // them — and the message_start — share ONE stable `msg_…` id.
  //
  // Two consequences drive the design:
  //   (a) An index-based dedup key cannot work: the consolidated array index is
  //       always 0 while the streaming index varies, so they never line up. We
  //       therefore dedup on (messageId, kind) via streamed-text prefix match,
  //       which is index-independent.
  //   (b) The streamed-block record must NOT be cleared per consolidated
  //       message: the first (thinking) consolidated would otherwise wipe the
  //       record the second (text) consolidated needs, re-emitting the whole
  //       reply (the observed 163-char answer rendered twice). We reset ONLY
  //       when a genuinely new turn begins — a `message_start` whose message id
  //       differs from the one we are currently tracking.
  /** SDK message id of the turn currently being streamed (from message_start). */
  private streamingMessageId: string | null = null;
  /**
   * Index-independent dedup for tool_use blocks (which carry no accumulated
   * text to prefix-match): `${messageId}|${kind}|${index}` for each streamed
   * block. tool_use consolidated arrives at array index matching its streamed
   * index in practice, but we keep the bare `${kind}|${index}` fallback too for
   * the id-less SDK shape. Reset only on a new-turn message_start.
   */
  private streamedBlockKinds = new Set<string>();
  /**
   * Primary dedup for text/thinking: the accumulated text streamed per
   * (messageId, kind) this turn. translateAssistant prefix-matches a
   * consolidated block's whole text against what was already streamed for the
   * same (messageId, kind) — index-independent, which is required because the
   * consolidated array index never matches the streaming index. Keyed by
   * `${messageId}|${kind}` (id-less fallback: bare kind). Reset only on a
   * new-turn message_start.
   */
  private streamedTextByKey = new Map<string, string>();
  /**
   * Usage is carried IDENTICALLY on every per-block consolidated assistant of a
   * turn (the same final snapshot repeated). Accounting it per consolidated
   * message would double/triple-count a single turn's tokens. We instead account
   * usage at most once per SDK message id; this set remembers which ids were
   * already counted. Bounded to the current turn's id on a new-turn reset.
   */
  private accountedUsageIds = new Set<string>();

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
   * that was streamed here (matched by `${message.id}|${index}` in
   * this.streamedBlocks, with a streamed-text prefix fallback), so text/thinking
   * are NOT duplicated in Redis. Usage accounting stays solely in
   * translateAssistant (the final, authoritative `assistant` message);
   * message_delta usage here is intentionally ignored to avoid double-counting.
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
        // currently tracking. The real SDK sequence emits one message_start per
        // turn (no trailing/duplicate), so a differing id is the reliable
        // new-turn signal; resetting here (and never on the consolidated
        // messages) means the multiple per-block consolidated messages of ONE
        // turn all still see their blocks as already-streamed. A new turn's
        // first message_start clears the previous turn's records so nothing
        // stale leaks across turns (and the consolidated of the new turn emits
        // its blocks when it was genuinely streamed / dedups when it wasn't).
        this.streamTextBlockIds.clear();
        this.streamThinkingBlockIds.clear();
        this.streamToolCallIds.clear();
        const id = this.messageIdOf(ev.message);
        if (id !== this.streamingMessageId) {
          this.streamedBlockKinds.clear();
          this.streamedTextByKey.clear();
          this.streamedToolArgs.clear();
          this.streamedToolCallEnds.clear();
          // accountedUsageIds is intentionally NOT cleared: it guards against
          // re-counting a turn's repeated usage snapshot, and an id is only ever
          // accounted once anyway. Pruning to the current id keeps it bounded.
          this.accountedUsageIds.clear();
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
          this.recordStreamedBlock(index, "text");
          return [{ event_type: "TextBlockStartEvent", block_id: blockId }];
        }
        if (kind === "thinking") {
          const blockId = randomUUID();
          this.streamThinkingBlockIds.set(index, blockId);
          this.recordStreamedBlock(index, "thinking");
          return [{ event_type: "ThinkingBlockStartEvent", block_id: blockId }];
        }
        if (kind === "tool_use") {
          const toolCallId = String(block.id ?? "");
          const toolName = String(block.name ?? "tool");
          if (toolCallId) this.toolNames.set(toolCallId, toolName);
          this.streamToolCallIds.set(index, toolCallId);
          if (toolCallId) this.streamedToolArgs.set(toolCallId, "");
          this.recordStreamedBlock(index, "tool_use");
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
          // the (messageId,kind) prefix accumulator that translateAssistant
          // matches against — index-independent on purpose.
          this.recordStreamedBlock(index, "text");
          const text = String(delta.text ?? "");
          this.appendStreamedText("text", text);
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
          this.recordStreamedBlock(index, "thinking");
          const text = String(delta.thinking ?? "");
          this.appendStreamedText("thinking", text);
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
          if (toolCallId) {
            this.streamedToolArgs.set(
              toolCallId,
              (this.streamedToolArgs.get(toolCallId) ?? "") +
                String(delta.partial_json ?? ""),
            );
          }
          return [
            {
              event_type: "ToolCallDeltaEvent",
              tool_call_id: toolCallId,
              delta: String(delta.partial_json ?? ""),
            },
          ];
        }
        return [];
      }
      case "content_block_stop": {
        const index = Number(ev.index ?? 0);
        // If tool arguments were streamed, close the call now. Otherwise wait
        // for the consolidated assistant message, which carries the complete
        // tool input for SDKs that omit input_json_delta.
        if (this.streamToolCallIds.has(index)) {
          const toolCallId = this.streamToolCallIds.get(index) ?? "";
          const toolArgs = this.streamedToolArgs.get(toolCallId) ?? "";
          if (!toolArgs || this.streamedToolCallEnds.has(toolCallId)) return [];
          this.streamedToolCallEnds.add(toolCallId);
          return [{ event_type: "ToolCallEndEvent", tool_call_id: toolCallId }];
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
      // message_delta / message_stop: usage is intentionally NOT counted here —
      // the consolidated `assistant` message carries the authoritative usage and
      // is the single source of truth for cumulative accounting (see class doc).
      case "message_delta":
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

    // Per-block dedup, index-independent (see streamedTextByKey doc). The real
    // SDK delivers a SEPARATE consolidated assistant per content block, each
    // putting its block at array index 0 — so the streaming index never matches
    // and we must NOT key dedup on index for text/thinking. Instead wasStreamed()
    // prefix-matches the block's whole text against what was streamed for this
    // (messageId, kind). If it was streamed we skip re-emitting (already in
    // Redis); otherwise we emit the full block — the non-streamed path where the
    // model returns a complete assistant message with no partials at all.
    // tool_use (no accumulated text) keeps the (messageId,kind,index) /
    // (kind,index) key so it is never mistakenly re-emitted nor lost.
    const msgId = this.messageIdOf(msg.message);
    const content: Json[] = msg.message?.content ?? [];
    content.forEach((block: Json, index: number) => {
      const kind = block?.type;
      if (kind === "text") {
        if (this.wasStreamed(msgId, index, "text", String(block.text ?? ""))) {
          return;
        }
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
        if (this.wasStreamed(msgId, index, "tool_use", "")) {
          const streamedArgs = this.streamedToolArgs.get(toolCallId);
          if (streamedArgs === undefined || streamedArgs.length === 0) {
            out.push({
              event_type: "ToolCallDeltaEvent",
              tool_call_id: toolCallId,
              delta: JSON.stringify(block.input ?? {}),
            });
            if (!this.streamedToolCallEnds.has(toolCallId)) {
              this.streamedToolCallEnds.add(toolCallId);
              out.push({ event_type: "ToolCallEndEvent", tool_call_id: toolCallId });
            }
          }
          return;
        }
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
        if (
          this.wasStreamed(msgId, index, "thinking", String(block.thinking ?? ""))
        ) {
          return;
        }
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

    // Do NOT clear the turn-scoped dedup records here. One turn delivers MULTIPLE
    // per-block consolidated assistants (real SDK: a thinking one mid-stream,
    // then a text one); clearing after the first would wipe the records the next
    // needs and re-emit the whole reply (the 163-char-twice bug). Records are
    // reset only when a new turn's message_start arrives with a different id.
    //
    // The genuinely non-streamed path (a complete assistant with no partials,
    // hence no message_start) carries a NEW message id, which differs from the
    // last streamed turn's id — but there is no message_start to trigger the
    // reset, so stale streamedTextByKey from a previous streamed turn could in
    // principle linger. It cannot cause a false skip here because the keys are
    // `${messageId}|${kind}` and this message's id is different (id-less is the
    // only ambiguous case, handled by the bare-kind fallback only when no id
    // exists on either side). For safety and boundedness we still re-home the
    // tracking id; we do not wipe the maps so a later same-turn consolidated of
    // THIS message keeps matching.
    if (msgId !== null && msgId !== this.streamingMessageId) {
      this.streamingMessageId = msgId;
    }

    // Accumulate usage from this assistant turn AT MOST ONCE per message id. Each
    // per-block consolidated of a turn repeats the SAME usage snapshot; counting
    // every one would multiply a single turn's tokens (observed 2× on the real
    // two-block turn). The per-turn increment event is emitted only on that first
    // accounting so downstream sees one ModelCallEnd per turn.
    out.push(...this.accountUsage(msg, msgId));

    return out;
  }

  /**
   * Record that block `index` (of `kind`) of the currently-streaming message was
   * streamed. Adds the (messageId,kind,index) key and the id-less (kind,index)
   * fallback — used to dedup tool_use blocks, which carry no accumulated text to
   * prefix-match; text/thinking are deduped by the streamed-text accumulator
   * (appendStreamedText) instead.
   */
  private recordStreamedBlock(index: number, kind: string): void {
    this.streamedBlockKinds.add(
      `${this.streamingMessageId ?? "?"}|${kind}|${index}`,
    );
    this.streamedBlockKinds.add(`${kind}|${index}`);
  }

  /**
   * Accumulate streamed text for the current turn's (messageId, kind). Keyed on
   * `${messageId}|${kind}` so it survives the multiple per-block consolidated
   * messages of one turn and is distinct across turns; bare `${kind}` is also
   * recorded as an id-less fallback for the leaner SDK shape that omits ids.
   */
  private appendStreamedText(kind: string, text: string): void {
    const id = this.streamingMessageId;
    if (id !== null) {
      const k = `${id}|${kind}`;
      this.streamedTextByKey.set(k, (this.streamedTextByKey.get(k) ?? "") + text);
    }
    this.streamedTextByKey.set(
      kind,
      (this.streamedTextByKey.get(kind) ?? "") + text,
    );
  }

  /**
   * Did partials already stream this consolidated block? INDEX-INDEPENDENT by
   * design — the real SDK puts every per-block consolidated at array index 0
   * while the streaming index varies, so an index-keyed test never lines up.
   *
   *   • text / thinking: prefix-match the consolidated block's whole text against
   *     the streamed-text accumulator for this (messageId, kind) (id-less
   *     fallback: bare kind). If the consolidated text is a prefix of (or equals)
   *     what was streamed, it was already emitted token-by-token → skip.
   *   • tool_use: no accumulated text, so fall back to the (messageId,kind,index)
   *     / (kind,index) key recorded during streaming. In the real SDK a streamed
   *     tool_use consolidates at the same index it streamed at, and the id-less
   *     key covers the leaner shape — never re-emitting nor dropping a tool call.
   *
   * Returns false for the genuinely non-streamed case so the full block emits.
   */
  private wasStreamed(
    messageId: string | null,
    index: number,
    kind: string,
    text: string,
  ): boolean {
    if (kind === "tool_use") {
      if (
        messageId !== null &&
        this.streamedBlockKinds.has(`${messageId}|${kind}|${index}`)
      ) {
        return true;
      }
      return this.streamedBlockKinds.has(`${kind}|${index}`);
    }
    // text / thinking: index-independent streamed-text prefix match.
    if (text.length === 0) return false;
    if (messageId !== null) {
      const streamed = this.streamedTextByKey.get(`${messageId}|${kind}`);
      if (streamed && streamed.startsWith(text)) return true;
    }
    const fallback = this.streamedTextByKey.get(kind);
    return fallback !== undefined && fallback.length > 0 && fallback.startsWith(text);
  }

  /** Extract the `msg_…` id from a consolidated message or a partial's
   * message_start envelope. Returns null when absent (triggers the fallback). */
  private messageIdOf(message: Json): string | null {
    const id = message?.id;
    return typeof id === "string" && id.length > 0 ? id : null;
  }

  /**
   * Accumulate this assistant turn's usage into the cumulative total and return
   * the single per-turn ModelCallEndEvent — AT MOST ONCE per SDK message id. A
   * turn delivers multiple per-block consolidated assistants that each repeat the
   * SAME usage snapshot (real SDK: thinking + text consolidated both carry
   * in=13154/out=117); counting each would multiply one turn's tokens. We account
   * the first time we see an id and skip every later consolidated of that id.
   * When no id is present (leaner SDK shape) we cannot dedup, so we account it —
   * matching the legacy one-consolidated-per-turn behavior. The streaming
   * `message_delta` path deliberately adds no usage, so a turn contributes
   * exactly one ModelCallEnd worth of input/output regardless of partials.
   */
  private accountUsage(msg: Json, msgId: string | null): EventPayload[] {
    if (msgId !== null) {
      if (this.accountedUsageIds.has(msgId)) return [];
      this.accountedUsageIds.add(msgId);
    }
    const usage: Json = msg.message?.usage ?? {};
    const input = Number(usage.input_tokens ?? 0) || 0;
    const output = Number(usage.output_tokens ?? 0) || 0;
    // DeepSeek prefix-cache hits, mapped by the adapter onto Anthropic's
    // cache_read_input_tokens. Failure-open: absent => 0.
    const cacheRead = Number(usage.cache_read_input_tokens ?? 0) || 0;
    this.cumulative.input_tokens += input;
    this.cumulative.output_tokens += output;
    this.cumulative.cache_read_input_tokens += cacheRead;
    return [
      {
        event_type: "ModelCallEndEvent",
        input_tokens: input,
        output_tokens: output,
        cache_read_input_tokens: cacheRead,
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
