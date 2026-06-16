/**
 * adapter.ts — in-process Anthropic ⇄ OpenAI bridge for the Claude SDK worker.
 *
 * The Claude Agent SDK speaks the Anthropic Messages protocol with a single
 * `Authorization: Bearer <token>`. The llm-gateway speaks ONLY the OpenAI
 * chat-completions protocol and demands three headers (Authorization +
 * X-Tenant-Id + X-LLM-Virtual-Key). This server sits between them:
 *
 *   SDK ──Anthropic POST /v1/messages──▶ adapter ──OpenAI POST /chat/completions──▶ gateway ──▶ model
 *
 * F.1 auth decision: in production the adapter is a same-process, trusted
 * component, so it does NOT decode identity out of the inbound bearer. It reads
 * FOUNDATION_API_TOKEN / FOUNDATION_TENANT_ID / LLM_VIRTUAL_KEY directly from
 * process.env and injects the gateway's three headers. The SDK-supplied
 * ANTHROPIC_AUTH_TOKEN is ignored beyond a non-empty presence check.
 *
 * The Anthropic⇄OpenAI translation + tool-loop consistency cleanup below was
 * validated in the spike; it is carried over unchanged.
 */

import http from "node:http";
import { Buffer } from "node:buffer";

/** Anything translated out of the SDK's dynamic Anthropic/OpenAI JSON. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any -- dynamic wire JSON
type Json = any;

export interface GatewayAdapterOptions {
  /** Gateway base, e.g. http://localhost:8011/api/llm-gateway/v1. */
  gatewayUrl?: string;
  /** Model name to rewrite every inbound Claude model to. */
  targetModel: string;
  /** Foundation bearer token injected into llm-gateway requests. */
  foundationToken?: string;
  /** Tenant context injected into llm-gateway requests. */
  tenantId?: string;
  /** User context forwarded for broker/upstream audit attribution. */
  userId?: string;
  /** Tenant virtual key injected into llm-gateway requests. */
  virtualKey?: string;
  /** Signed tool-broker lease when the gateway URL is a broker façade. */
  brokerLease?: string;
  /**
   * Model output-token cap (task.sampling_params.max_tokens). The Claude Agent
   * SDK exposes NO Options field for the model's output max_tokens — only
   * `maxThinkingTokens` (the thinking budget). So the requested output cap is
   * carried here and applied to the OpenAI `max_tokens` in anthropicToOpenAIBody,
   * overriding the SDK's model-default. Unset => keep whatever the SDK sends.
   */
  maxOutputTokens?: number;
  /**
   * DeepSeek reasoning effort (task.reasoning_effort). DeepSeek only honours
   * `high`/`max`; the worker has already mapped Anthropic low/medium → high
   * before constructing the adapter. The SDK has no wire path for effort either,
   * so it is carried here and emitted as the OpenAI `reasoning_effort` field.
   * Unset => no reasoning_effort emitted.
   */
  reasoningEffort?: string;
  /**
   * Structured-output JSON schema (task.sampling_params.response_format /
   * output_config.format). Self-hosted vLLM/SGLang supports constrained decoding
   * via OpenAI `response_format:{type:"json_schema", json_schema:{...}}`. The SDK
   * cannot forward output_config, so the raw schema is carried here and emitted
   * verbatim. Unset => no response_format (current behaviour preserved).
   */
  structuredOutputSchema?: Json;
  /** Bind port. 0 (default) picks a random free port. */
  port?: number;
}

const DEFAULT_GATEWAY_URL = "http://localhost:8011/api/llm-gateway/v1";

// ── Anthropic → OpenAI message mapping ──────────────────────────────────────
// Anthropic content may be a string OR an array of blocks ({type:"text",text},
// plus tool_use / tool_result / image). We flatten to a single OpenAI string.
function flattenContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block: Json) => {
        if (block?.type === "text" && typeof block.text === "string") return block.text;
        // tool_result blocks carry their payload under `content`.
        if (block?.type === "tool_result") return flattenContent(block.content);
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

// Tool schema mapping. Anthropic: {name, description, input_schema};
// OpenAI: {type:"function", function:{name, description, parameters}}.
// An Anthropic tool carrying `strict:true` (constrained tool-arg decoding) has
// the flag passed through onto the OpenAI function definition; self-hosted
// vLLM/SGLang honour `function.strict`. Absent/false `strict` => omitted.
function anthropicToolsToOpenAI(tools: Json[]): Json[] {
  return (tools ?? [])
    .filter((t: Json) => t && t.name && t.input_schema)
    .map((t: Json) => {
      const fn: Json = { name: t.name, description: t.description ?? "", parameters: t.input_schema };
      if (t.strict === true) fn.strict = true;
      return { type: "function", function: fn };
    });
}

// One Anthropic message may expand into several OpenAI messages: an assistant
// turn carrying tool_use blocks becomes an assistant message with `tool_calls`,
// and each Anthropic tool_result block becomes its own {role:"tool"} message
// keyed by tool_call_id. This keeps the multi-turn tool loop intact.
function anthropicMessageToOpenAI(m: Json): Json[] {
  const content = m.content;
  if (typeof content === "string") return [{ role: m.role, content }];
  if (!Array.isArray(content)) return [{ role: m.role, content: "" }];

  if (m.role === "assistant") {
    const text = content.filter((b: Json) => b?.type === "text").map((b: Json) => b.text).join("\n");
    // De-dup tool_use blocks by id WITHIN this assistant message. After a hook
    // deny, the SDK can replay the denied tool_use twice in the same turn; two
    // tool_calls with the same id makes the backend 400 ("Duplicate value for
    // 'tool_call_id'"). Keep the first occurrence of each id.
    const seen = new Set<string>();
    const toolUses = content.filter(
      (b: Json) => b?.type === "tool_use" && b.id && !seen.has(b.id) && seen.add(b.id),
    );
    const msg: Json = { role: "assistant", content: text || null };
    if (toolUses.length) {
      msg.tool_calls = toolUses.map((b: Json) => ({
        id: b.id,
        type: "function",
        function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
      }));
    }
    return [msg];
  }

  // user role: tool_result blocks -> {role:"tool"} messages; plain text stays user.
  const out: Json[] = [];
  const toolResults = content.filter((b: Json) => b?.type === "tool_result");
  for (const tr of toolResults) {
    out.push({ role: "tool", tool_call_id: tr.tool_use_id, content: flattenContent(tr.content) });
  }
  const text = content.filter((b: Json) => b?.type === "text").map((b: Json) => b.text).join("\n");
  if (text) out.push({ role: "user", content: text });
  return out.length ? out : [{ role: "user", content: "" }];
}

/** Request-level translation knobs sourced from the trusted adapter snapshot. */
interface RequestTranslationOptions {
  targetModel: string;
  /** Override the OpenAI `max_tokens` (model output cap). */
  maxOutputTokens?: number;
  /** Emit OpenAI `reasoning_effort` (already mapped to high/max by the worker). */
  reasoningEffort?: string;
  /** Emit OpenAI `response_format:{type:"json_schema",...}` with this schema. */
  structuredOutputSchema?: Json;
  /**
   * Structured-output fallback mode. When false (default) and a schema is
   * configured, the body carries `response_format:{type:"json_schema",strict}`
   * (constrained decoding — self-hosted vLLM/SGLang). When true, the upstream has
   * been observed to reject json_schema (typically the DeepSeek official API,
   * which only supports `{type:"json_object"}`), so the body instead carries
   * `response_format:{type:"json_object"}` and a schema-hint message is appended
   * so the model still emits schema-conformant JSON. No effect without a schema.
   */
  jsonSchemaFallback?: boolean;
}

// Detection terms for "upstream does not accept json_schema response_format".
// Kept narrow on purpose so unrelated 400s (bad params, content policy, etc.)
// are NOT mistaken for a capability gap and do NOT trigger a fallback retry.
const JSON_SCHEMA_REJECTION_TERMS = ["response_format", "json_schema", "json schema"];
const JSON_SCHEMA_REJECTION_HINTS = ["unsupported", "unknown", "not support", "invalid", "must be", "expected"];

/**
 * Decide whether a gateway error means "this upstream does not support
 * json_schema response_format" (=> safe to fall back to json_object) vs any
 * other failure (=> propagate via the normal error path, do NOT downgrade).
 *
 * Robust rule: only a 4xx (the upstream actively rejected the request — 400/422
 * are the typical "unsupported response_format type" shapes) whose error body
 * names a response_format/json_schema field AND carries a rejection hint
 * (unsupported / unknown / invalid / must be …). Both conditions are required so
 * a generic 400 that merely happens to echo the request (which may contain the
 * literal "json_schema") is not misclassified. 429/5xx are never a capability
 * gap and always take the normal error path.
 */
function isJsonSchemaUnsupportedError(status: number, body: string): boolean {
  if (status < 400 || status >= 500) return false; // only client-side rejections
  if (status === 429) return false; // rate limit — transient, not a capability gap
  const lower = body.toLowerCase();
  const namesField = JSON_SCHEMA_REJECTION_TERMS.some((t) => lower.includes(t));
  if (!namesField) return false;
  const hasHint = JSON_SCHEMA_REJECTION_HINTS.some((h) => lower.includes(h));
  return hasHint;
}

/**
 * Build the schema-hint message injected when falling back to json_object. The
 * DeepSeek json_object mode requires the prompt to contain the word "json" and
 * works best when the target schema/example is shown, so we spell out the schema
 * and demand a single valid JSON object with no extra prose.
 */
function buildJsonSchemaHintMessage(schema: Json): { role: "system"; content: string } {
  let schemaText: string;
  try {
    schemaText = JSON.stringify(schema, null, 2);
  } catch {
    schemaText = String(schema);
  }
  return {
    role: "system",
    content:
      "You must respond with a single valid JSON object that conforms to the " +
      "following JSON schema. Output only the JSON object — no prose, no " +
      "markdown code fences, no commentary.\n\nJSON schema:\n" +
      schemaText,
  };
}

/**
 * Map an inbound Anthropic `thinking` field onto DeepSeek's request-level
 * `thinking:{type:"enabled"|"disabled"}`. The Claude Agent SDK emits the standard
 * Anthropic shape `{type:"enabled", budget_tokens:N}` when `maxThinkingTokens` is
 * set, and omits the field otherwise. We collapse every "on" spelling
 * (enabled/adaptive) to `{type:"enabled"}` and `disabled` to `{type:"disabled"}`;
 * DeepSeek returns the chain-of-thought in `reasoning_content`. The budget is not
 * forwarded (DeepSeek controls thinking length via reasoning_effort, not a token
 * budget). Returns undefined when no thinking field is present (no change).
 */
function mapThinking(thinking: Json): { type: "enabled" | "disabled" } | undefined {
  if (thinking == null || typeof thinking !== "object") return undefined;
  const t = String(thinking.type ?? "").toLowerCase();
  if (t === "disabled") return { type: "disabled" };
  // enabled / adaptive / anything else truthy => enabled.
  if (t) return { type: "enabled" };
  return undefined;
}

function anthropicToOpenAIBody(
  body: Json,
  opts: RequestTranslationOptions,
): Record<string, unknown> {
  const targetModel = opts.targetModel;
  const messages: Json[] = [];

  // Anthropic puts the system prompt at the top level (string or block array);
  // OpenAI wants it as a leading {role:"system"} message.
  if (body.system) {
    messages.push({ role: "system", content: flattenContent(body.system) });
  }

  for (const m of body.messages ?? []) {
    messages.push(...anthropicMessageToOpenAI(m));
  }

  // ── Tool-loop consistency cleanup ──
  // The SDK can replay a denied tool_use such that the same tool_call_id shows
  // up more than once, or a tool_result arrives with no matching assistant
  // tool_call. The backend rejects both. Keep only the first tool message per
  // id, and only those whose id was announced by a preceding assistant call.
  const announced = new Set<string>(); // tool_call ids emitted by some assistant turn
  const seenToolMsg = new Set<string>(); // tool result ids already emitted
  const cleaned: Json[] = [];
  for (const msg of messages) {
    if (msg.role === "assistant" && Array.isArray(msg.tool_calls)) {
      // De-dup tool_calls ACROSS turns: a denied tool_use can be replayed with
      // the same id in a later assistant turn. Keep only ids not seen before.
      const fresh = msg.tool_calls.filter((tc: Json) => tc.id && !announced.has(tc.id));
      for (const tc of fresh) announced.add(tc.id);
      if (fresh.length) {
        cleaned.push({ ...msg, tool_calls: fresh });
      } else if (msg.content) {
        // all tool_calls were dupes — keep any text, drop the empty tool_calls
        cleaned.push({ role: "assistant", content: msg.content });
      }
      // else: pure duplicate assistant turn with no text -> drop entirely
    } else if (msg.role === "tool") {
      const id = msg.tool_call_id;
      if (!id || !announced.has(id) || seenToolMsg.has(id)) continue; // drop orphan/dup
      seenToolMsg.add(id);
      cleaned.push(msg);
    } else {
      cleaned.push(msg);
    }
  }

  // Pass through the inbound stream flag. The Claude Agent SDK sets stream:true
  // when it wants a streamed (SSE) response; the gateway honours stream:true and
  // replies with OpenAI SSE. When streaming we also opt into usage on the final
  // chunk(s) via stream_options.include_usage (matches the AgentScope enforcer),
  // otherwise the OpenAI stream omits usage entirely.
  const streaming = body.stream === true;
  const out: Record<string, unknown> = {
    model: targetModel, // rewrite Claude model name -> gateway/backend model
    messages: cleaned,
    stream: streaming,
  };
  if (streaming) {
    out.stream_options = { include_usage: true };
  }

  if (Array.isArray(body.tools) && body.tools.length) {
    out.tools = anthropicToolsToOpenAI(body.tools);
  }

  if (body.max_tokens != null) out.max_tokens = body.max_tokens;
  if (body.temperature != null) out.temperature = body.temperature;
  if (body.top_p != null) out.top_p = body.top_p;
  if (body.stop_sequences != null) out.stop = body.stop_sequences;

  // ── request-level feature passthrough (whitelist-extended) ──
  // thinking: the SDK emits Anthropic {type:"enabled",budget_tokens:N} when
  // maxThinkingTokens is set. Collapse to DeepSeek's {type:"enabled"|"disabled"}.
  const thinking = mapThinking(body.thinking);
  if (thinking) out.thinking = thinking;

  // reasoning_effort: carried via the trusted snapshot (no SDK wire path). The
  // worker has already clamped it to high/max (DeepSeek's only accepted values).
  if (opts.reasoningEffort) out.reasoning_effort = opts.reasoningEffort;

  // max_tokens (model output cap): the SDK has no Options field for it, so the
  // requested cap arrives via the snapshot and OVERRIDES whatever model-default
  // max_tokens the SDK put on the inbound body.
  if (opts.maxOutputTokens != null) out.max_tokens = opts.maxOutputTokens;

  // structured outputs: emit a response_format only when a schema is configured;
  // otherwise leave response_format absent (no change). Two emit modes:
  //   - default (jsonSchemaFallback unset/false): constrained decoding via
  //     {type:"json_schema",strict} — self-hosted vLLM/SGLang. Schema forwarded
  //     verbatim (no rewriting of its structure).
  //   - fallback (jsonSchemaFallback true): the upstream rejected json_schema
  //     (e.g. DeepSeek official API), so downgrade to {type:"json_object"} and
  //     append a schema-hint system message so the model still emits conformant
  //     JSON. The schema itself is NOT sent in response_format here.
  if (opts.structuredOutputSchema != null) {
    if (opts.jsonSchemaFallback) {
      out.response_format = { type: "json_object" };
      cleaned.push(buildJsonSchemaHintMessage(opts.structuredOutputSchema));
    } else {
      out.response_format = {
        type: "json_schema",
        json_schema: { name: "response", schema: opts.structuredOutputSchema, strict: true },
      };
    }
  }

  return out;
}

// ── OpenAI → Anthropic response mapping ─────────────────────────────────────
function openAIToAnthropicResponse(oa: Json, requestedModel: string): Record<string, unknown> {
  const choice = oa?.choices?.[0] ?? {};
  const msg = choice.message ?? {};
  const usage = oa?.usage ?? {};

  const content: Json[] = [];
  // DeepSeek returns chain-of-thought in `reasoning_content`; translate it into
  // an Anthropic thinking content block placed BEFORE the text block so the SDK
  // (and event_translator) surface a thinking group ahead of the reply.
  if (msg.reasoning_content) {
    content.push({ type: "thinking", thinking: String(msg.reasoning_content) });
  }
  if (msg.content) content.push({ type: "text", text: String(msg.content) });
  // De-dup tool_calls by id: the backend can return the SAME id twice in one
  // response. Emitting two tool_use blocks with the same id makes the next
  // round-trip produce a duplicate tool_call_id the backend rejects.
  const rawCalls = msg.tool_calls ?? [];
  const seenIds = new Set<string>();
  const toolCalls = rawCalls.filter((tc: Json) => {
    if (!tc.id || seenIds.has(tc.id)) return false;
    seenIds.add(tc.id);
    return true;
  });
  for (const tc of toolCalls) {
    let input: Json = {};
    try {
      input = JSON.parse(tc.function?.arguments ?? "{}");
    } catch {
      input = { _raw: tc.function?.arguments }; // tolerate malformed args
    }
    content.push({ type: "tool_use", id: tc.id, name: tc.function?.name, input });
  }
  if (!content.length) content.push({ type: "text", text: "" });

  const stopReason = toolCalls.length || choice.finish_reason === "tool_calls" ? "tool_use" : "end_turn";

  return {
    id: oa?.id ?? `msg_${Date.now()}`,
    type: "message",
    role: "assistant",
    model: requestedModel,
    content,
    stop_reason: stopReason,
    stop_sequence: null,
    usage: {
      input_tokens: usage.prompt_tokens ?? 0,
      output_tokens: usage.completion_tokens ?? 0,
      // DeepSeek prefix-cache observability: the OpenAI-shaped gateway response
      // carries `prompt_cache_hit_tokens` at the top of `usage` (the count of
      // prompt tokens served from DeepSeek's automatic prefix cache). Map it
      // onto Anthropic's `cache_read_input_tokens` (same semantics: reused /
      // cache-hit input tokens) so the SDK and downstream can observe it.
      // Failure-open: absent upstream field => 0.
      cache_read_input_tokens: usage.prompt_cache_hit_tokens ?? 0,
    },
  };
}

// ── OpenAI SSE → Anthropic SSE streaming translation ────────────────────────
// The gateway streams OpenAI chat.completion.chunk frames; the SDK expects the
// Anthropic Messages streaming event sequence. This translator consumes the
// gateway's web ReadableStream, reframes partial network reads on `\n\n`
// boundaries, and emits Anthropic SSE events in protocol order.

/** Write one Anthropic SSE event (`event: <type>\ndata: <json>\n\n`). */
function writeSse(res: http.ServerResponse, type: string, data: unknown): void {
  res.write(`event: ${type}\ndata: ${JSON.stringify(data)}\n\n`);
}

// Per-tool-call accumulator. OpenAI streams tool_calls incrementally keyed by an
// `index`; we map each distinct OpenAI index to a sequential Anthropic content
// block index and remember the assigned id/name so we never emit a block twice.
interface ToolBlockState {
  anthropicIndex: number; // the Anthropic content block index for this tool call
  id: string;
  name: string;
  started: boolean; // content_block_start already emitted
}

/**
 * Drive the OpenAI→Anthropic SSE translation. Reads the gateway streaming body,
 * writes Anthropic SSE events to `res`, and always closes the response cleanly
 * (message_stop) so the SDK reader never hangs. Failure-open: any read/parse
 * error terminates the stream gracefully.
 */
async function streamOpenAIToAnthropic(
  body: ReadableStream<Uint8Array> | null,
  res: http.ServerResponse,
  requestedModel: string,
): Promise<void> {
  // message_start first — synthesize an id; usage starts at zero and is updated
  // by the closing message_delta once the final chunk reports it.
  const messageId = `msg_${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
  writeSse(res, "message_start", {
    type: "message_start",
    message: {
      id: messageId,
      type: "message",
      role: "assistant",
      model: requestedModel,
      content: [],
      stop_reason: null,
      stop_sequence: null,
      usage: { input_tokens: 0, output_tokens: 0 },
    },
  });

  // Block bookkeeping. Text occupies one block (lazily opened on first token);
  // each OpenAI tool_call index occupies its own block. nextBlockIndex hands out
  // monotonically increasing Anthropic indices in first-appearance order.
  let nextBlockIndex = 0;
  let textBlockIndex = -1; // -1 until the first text delta opens it
  let textStarted = false;
  let thinkingBlockIndex = -1; // -1 until the first reasoning_content delta opens it
  let thinkingStarted = false;
  const toolBlocks = new Map<number, ToolBlockState>(); // OpenAI index -> state
  const seenToolIds = new Set<string>(); // de-dup tool_call ids (same hygiene as JSON path)
  let sawToolCall = false;

  // Usage carried from the terminal chunk(s) (stream_options.include_usage).
  let usageOut: { output_tokens: number; input_tokens?: number; cache_read_input_tokens?: number } = {
    output_tokens: 0,
  };
  let finishReason: string | null = null;

  const closeOpenBlocks = (): void => {
    if (thinkingStarted && thinkingBlockIndex >= 0) {
      writeSse(res, "content_block_stop", { type: "content_block_stop", index: thinkingBlockIndex });
      thinkingStarted = false;
    }
    if (textStarted && textBlockIndex >= 0) {
      writeSse(res, "content_block_stop", { type: "content_block_stop", index: textBlockIndex });
      textStarted = false;
    }
    for (const tb of toolBlocks.values()) {
      if (tb.started) {
        writeSse(res, "content_block_stop", { type: "content_block_stop", index: tb.anthropicIndex });
        tb.started = false;
      }
    }
  };

  const finish = (): void => {
    closeOpenBlocks();
    const stopReason =
      sawToolCall || finishReason === "tool_calls" ? "tool_use" : "end_turn";
    const usage: Record<string, number> = { output_tokens: usageOut.output_tokens };
    if (usageOut.input_tokens != null) usage.input_tokens = usageOut.input_tokens;
    if (usageOut.cache_read_input_tokens != null) {
      usage.cache_read_input_tokens = usageOut.cache_read_input_tokens;
    }
    writeSse(res, "message_delta", {
      type: "message_delta",
      delta: { stop_reason: stopReason, stop_sequence: null },
      usage,
    });
    writeSse(res, "message_stop", { type: "message_stop" });
    res.end();
  };

  // Translate one parsed OpenAI chunk into zero or more Anthropic SSE events.
  const handleChunk = (chunk: Json): void => {
    if (chunk?.usage) {
      const u = chunk.usage;
      usageOut = {
        output_tokens: u.completion_tokens ?? usageOut.output_tokens ?? 0,
        input_tokens: u.prompt_tokens ?? usageOut.input_tokens,
        cache_read_input_tokens: u.prompt_cache_hit_tokens ?? usageOut.cache_read_input_tokens,
      };
    }
    const choice = chunk?.choices?.[0];
    if (!choice) return;
    if (choice.finish_reason) finishReason = choice.finish_reason;
    const delta = choice.delta;
    if (!delta) return;

    // THINKING fragments (DeepSeek `reasoning_content`) → one thinking block,
    // opened lazily on first appearance. DeepSeek streams reasoning_content
    // before content, so this block naturally precedes the text block; the SDK
    // then surfaces a thinking group ahead of the reply (see event_translator).
    if (typeof delta.reasoning_content === "string" && delta.reasoning_content.length > 0) {
      if (!thinkingStarted) {
        thinkingBlockIndex = nextBlockIndex++;
        writeSse(res, "content_block_start", {
          type: "content_block_start",
          index: thinkingBlockIndex,
          content_block: { type: "thinking", thinking: "" },
        });
        thinkingStarted = true;
      }
      writeSse(res, "content_block_delta", {
        type: "content_block_delta",
        index: thinkingBlockIndex,
        delta: { type: "thinking_delta", thinking: delta.reasoning_content },
      });
    }

    // TEXT fragments → one text block (index assigned on first appearance).
    if (typeof delta.content === "string" && delta.content.length > 0) {
      if (!textStarted) {
        textBlockIndex = nextBlockIndex++;
        writeSse(res, "content_block_start", {
          type: "content_block_start",
          index: textBlockIndex,
          content_block: { type: "text", text: "" },
        });
        textStarted = true;
      }
      writeSse(res, "content_block_delta", {
        type: "content_block_delta",
        index: textBlockIndex,
        delta: { type: "text_delta", text: delta.content },
      });
    }

    // TOOL CALL fragments → one block per OpenAI tool_call index.
    if (Array.isArray(delta.tool_calls)) {
      for (const tc of delta.tool_calls) {
        const oaIndex = tc.index ?? 0;
        let state = toolBlocks.get(oaIndex);
        if (!state) {
          // First appearance of this OpenAI index. id/name arrive in the first
          // fragment for the call; de-dup ids so we never open two blocks for
          // the same tool_call id (same hygiene as the non-streaming path).
          const id = tc.id ?? "";
          if (id && seenToolIds.has(id)) {
            // Already opened a block for this id under a different index — map
            // this index to that nothing (ignore further fragments for it).
            toolBlocks.set(oaIndex, { anthropicIndex: -1, id, name: "", started: false });
            continue;
          }
          if (id) seenToolIds.add(id);
          state = {
            anthropicIndex: nextBlockIndex++,
            id,
            name: tc.function?.name ?? "",
            started: false,
          };
          toolBlocks.set(oaIndex, state);
        }
        if (state.anthropicIndex < 0) continue; // suppressed duplicate id

        if (!state.started) {
          // Backfill id/name if they only arrived after the first fragment.
          if (!state.id && tc.id) {
            if (seenToolIds.has(tc.id)) {
              state.anthropicIndex = -1;
              continue;
            }
            state.id = tc.id;
            seenToolIds.add(tc.id);
          }
          if (!state.name && tc.function?.name) state.name = tc.function.name;
          sawToolCall = true;
          writeSse(res, "content_block_start", {
            type: "content_block_start",
            index: state.anthropicIndex,
            content_block: { type: "tool_use", id: state.id, name: state.name, input: {} },
          });
          state.started = true;
        }

        const argFragment = tc.function?.arguments;
        if (typeof argFragment === "string" && argFragment.length > 0) {
          writeSse(res, "content_block_delta", {
            type: "content_block_delta",
            index: state.anthropicIndex,
            delta: { type: "input_json_delta", partial_json: argFragment },
          });
        }
      }
    }
  };

  if (!body) {
    finish();
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line. Network reads may split a
      // frame mid-line, so keep the trailing incomplete chunk in `buffer`.
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const rawLine of frame.split("\n")) {
          const line = rawLine.replace(/\r$/, "");
          if (!line || line.startsWith(":")) continue; // blank / keep-alive comment
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          if (payload === "[DONE]") {
            finish();
            return;
          }
          let parsed: Json;
          try {
            parsed = JSON.parse(payload);
          } catch {
            continue; // tolerate a malformed frame rather than abort the stream
          }
          handleChunk(parsed);
        }
      }
    }
    // Flush any trailing buffered frame that lacked a terminating blank line.
    const tail = buffer.trim();
    if (tail) {
      for (const rawLine of tail.split("\n")) {
        const line = rawLine.replace(/\r$/, "").trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === "[DONE]") continue;
        try {
          handleChunk(JSON.parse(payload));
        } catch {
          /* ignore */
        }
      }
    }
    finish();
  } catch (err) {
    // Failure-open: terminate the SSE cleanly so the SDK reader does not hang.
    console.error(`[adapter] stream read failed: ${(err as Error)?.message ?? String(err)}`);
    try {
      finish();
    } catch {
      try {
        res.end();
      } catch {
        /* ignore */
      }
    }
  } finally {
    try {
      reader.releaseLock();
    } catch {
      /* ignore */
    }
  }
}

// ── HTTP plumbing ────────────────────────────────────────────────────────────
function readBody(req: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res: http.ServerResponse, status: number, payload: unknown): void {
  const data = JSON.stringify(payload);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(data);
}

/**
 * In-process gateway adapter. Construct with the target model (and optionally a
 * gateway URL / port), then `start()` to bind and `stop()` to shut down.
 */
export class GatewayAdapter {
  private readonly gatewayUrl: string;
  private readonly targetModel: string;
  private readonly foundationToken?: string;
  private readonly tenantId?: string;
  private readonly userId?: string;
  private readonly virtualKey?: string;
  private readonly brokerLease?: string;
  private readonly maxOutputTokens?: number;
  private readonly reasoningEffort?: string;
  private readonly structuredOutputSchema?: Json;
  private readonly requestedPort: number;
  private server: http.Server | null = null;
  private boundPort = 0;
  /**
   * Process-local memory: once the upstream is observed to reject json_schema
   * response_format (probe-on-error), every subsequent request in THIS worker
   * process skips straight to the json_object fallback instead of failing the
   * json_schema probe again. The worker is short-lived (one run per process) so
   * in-process memory suffices; no cross-run persistence is needed. Only ever
   * set, never cleared — a capability gap does not come and go within one run.
   */
  private jsonSchemaUnsupported = false;

  constructor(opts: GatewayAdapterOptions) {
    this.gatewayUrl = (opts.gatewayUrl ?? DEFAULT_GATEWAY_URL).replace(/\/+$/, "");
    this.targetModel = opts.targetModel;
    this.foundationToken = opts.foundationToken;
    this.tenantId = opts.tenantId;
    this.userId = opts.userId;
    this.virtualKey = opts.virtualKey;
    this.brokerLease = opts.brokerLease;
    this.maxOutputTokens = opts.maxOutputTokens;
    this.reasoningEffort = opts.reasoningEffort;
    this.structuredOutputSchema = opts.structuredOutputSchema;
    this.requestedPort = opts.port ?? 0;
  }

  /** Start the HTTP server. Resolves with the actual bound port. */
  start(): Promise<number> {
    if (this.server) return Promise.resolve(this.boundPort);
    this.server = http.createServer((req, res) => {
      void this.handle(req, res);
    });
    return new Promise<number>((resolve, reject) => {
      const server = this.server;
      if (!server) {
        reject(new Error("adapter server not initialized"));
        return;
      }
      server.once("error", reject);
      server.listen(this.requestedPort, () => {
        const addr = server.address();
        if (addr && typeof addr === "object") {
          this.boundPort = addr.port;
        }
        console.log(
          `[adapter] listening on http://localhost:${this.boundPort} -> gateway ${this.gatewayUrl} (model=${this.targetModel})`,
        );
        resolve(this.boundPort);
      });
    });
  }

  /** Gracefully close the server. */
  stop(): Promise<void> {
    const server = this.server;
    if (!server) return Promise.resolve();
    this.server = null;
    return new Promise<void>((resolve) => {
      server.close(() => resolve());
    });
  }

  /** Actual bound port (0 until started). */
  get port(): number {
    return this.boundPort;
  }

  private async handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    // Strip query string — the SDK appends `?beta=true` to /v1/messages.
    const pathname = (req.url ?? "").split("?")[0];

    if (req.method === "POST" && pathname === "/v1/messages") {
      await this.handleMessages(req, res);
      return;
    }

    if (req.method === "GET" && pathname === "/healthz") {
      sendJson(res, 200, { status: "ok" });
      return;
    }

    sendJson(res, 404, {
      type: "error",
      error: { type: "not_found", message: `no route for ${req.method} ${req.url}` },
    });
  }

  private async handleMessages(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    // F.1: the inbound SDK bearer carries no identity in production. Require it
    // to be non-empty (the SDK always sets ANTHROPIC_AUTH_TOKEN) but ignore the
    // value. Real identity comes from process.env below.
    const auth = req.headers["authorization"];
    if (!auth || typeof auth !== "string" || !/^Bearer\s+\S/i.test(auth.trim())) {
      console.warn("[adapter] 401 — missing/empty Authorization bearer from SDK");
      sendJson(res, 401, {
        type: "error",
        error: { type: "authentication_error", message: "missing bearer token" },
      });
      return;
    }

    // Gateway identity from the trusted adapter snapshot (NOT from the SDK bearer).
    const foundationToken =
      this.foundationToken ?? process.env.FOUNDATION_API_TOKEN ?? "";
    const tenantId = this.tenantId ?? process.env.FOUNDATION_TENANT_ID ?? "";
    const userId = this.userId ?? process.env.FOUNDATION_USER_ID ?? "";
    const virtualKey = this.virtualKey ?? process.env.LLM_VIRTUAL_KEY ?? "";
    const brokerLease = this.brokerLease ?? process.env.TOOL_BROKER_LEASE ?? "";

    let inbound: Json;
    try {
      inbound = JSON.parse((await readBody(req)) || "{}");
    } catch {
      sendJson(res, 400, {
        type: "error",
        error: { type: "invalid_request_error", message: "bad JSON body" },
      });
      return;
    }

    const requestedModel: string = inbound.model ?? "unknown";
    const streaming = inbound.stream === true;
    const target = `${this.gatewayUrl}/chat/completions`;
    const gwHeaders: Record<string, string> = {
      "content-type": "application/json",
      // The three headers the gateway hard-requires, sourced from env.
      authorization: `Bearer ${foundationToken}`,
      "x-tenant-id": tenantId,
      "x-user-id": userId,
      "x-llm-virtual-key": virtualKey,
      ...(brokerLease ? { "x-tool-broker-lease": brokerLease } : {}),
    };

    // Build the OpenAI body for the current structured-output mode. When this
    // adapter has already learned the upstream rejects json_schema, start in
    // fallback mode straight away (no wasted probe).
    const buildBody = (jsonSchemaFallback: boolean): Record<string, unknown> =>
      anthropicToOpenAIBody(inbound, {
        targetModel: this.targetModel,
        maxOutputTokens: this.maxOutputTokens,
        reasoningEffort: this.reasoningEffort,
        structuredOutputSchema: this.structuredOutputSchema,
        jsonSchemaFallback,
      });

    const callGateway = (body: Record<string, unknown>): Promise<Response> =>
      fetch(target, { method: "POST", headers: gwHeaders, body: JSON.stringify(body) });

    try {
      let gwResp = await callGateway(buildBody(this.jsonSchemaUnsupported));

      // ── Structured-output probe-and-downgrade ──
      // Only relevant when a schema is configured and we are NOT already in
      // fallback mode. If the upstream rejected the json_schema response_format
      // (capability gap, not a transient error), remember it process-wide,
      // downgrade to json_object + schema-hint, and retry ONCE. Unrelated 4xx /
      // 429 / 5xx are left untouched and flow to the normal error path below.
      // We only consume the body on a non-2xx response, so a successful
      // streaming body is never drained here.
      if (
        !gwResp.ok &&
        this.structuredOutputSchema != null &&
        !this.jsonSchemaUnsupported
      ) {
        const probeBody = await gwResp.text().catch(() => "");
        if (isJsonSchemaUnsupportedError(gwResp.status, probeBody)) {
          console.warn(
            "[adapter] upstream rejected json_schema response_format; falling back to json_object",
          );
          this.jsonSchemaUnsupported = true;
          gwResp = await callGateway(buildBody(true));
        } else {
          // Not a json_schema capability gap — reconstruct a Response carrying
          // the already-read body so the normal error path can surface it.
          gwResp = new Response(probeBody, {
            status: gwResp.status,
            headers: { "content-type": gwResp.headers.get("content-type") ?? "application/json" },
          });
        }
      }

      // ── Streaming path: forward the gateway SSE as translated Anthropic SSE ──
      if (streaming) {
        if (!gwResp.ok) {
          // Surface the gateway failure as an Anthropic SSE `error` event so the
          // SDK's stream reader observes a clean terminal event instead of a
          // hang or a content-type mismatch.
          const rawText = await gwResp.text().catch(() => "");
          console.error(
            `[adapter] gateway error (stream) status=${gwResp.status} body=${rawText.slice(0, 500)}`,
          );
          res.writeHead(200, {
            "content-type": "text/event-stream",
            "cache-control": "no-cache",
            connection: "keep-alive",
          });
          writeSse(res, "error", {
            type: "error",
            error: { type: "api_error", message: `gateway ${gwResp.status}: ${rawText.slice(0, 500)}` },
          });
          res.end();
          return;
        }
        res.writeHead(200, {
          "content-type": "text/event-stream",
          "cache-control": "no-cache",
          connection: "keep-alive",
        });
        await streamOpenAIToAnthropic(
          gwResp.body as ReadableStream<Uint8Array> | null,
          res,
          requestedModel,
        );
        return;
      }

      // ── Non-streaming path: single JSON response (existing behaviour) ──
      const rawText = await gwResp.text();
      if (!gwResp.ok) {
        console.error(`[adapter] gateway error status=${gwResp.status} body=${rawText.slice(0, 500)}`);
        sendJson(res, gwResp.status, {
          type: "error",
          error: { type: "api_error", message: `gateway ${gwResp.status}: ${rawText.slice(0, 500)}` },
        });
        return;
      }

      let oa: Json;
      try {
        oa = JSON.parse(rawText);
      } catch {
        console.error(`[adapter] gateway returned non-JSON: ${rawText.slice(0, 200)}`);
        sendJson(res, 502, {
          type: "error",
          error: { type: "api_error", message: "gateway returned non-JSON" },
        });
        return;
      }

      sendJson(res, 200, openAIToAnthropicResponse(oa, requestedModel));
    } catch (err) {
      const message = (err as Error)?.message ?? String(err);
      console.error(`[adapter] forward failed: ${message}`);
      // If we already started streaming SSE (headers sent), we cannot switch to
      // a JSON error; close the stream cleanly so the SDK reader unblocks.
      if (res.headersSent) {
        try {
          if (!res.writableEnded) {
            writeSse(res, "error", {
              type: "error",
              error: { type: "api_error", message: `adapter forward failed: ${message}` },
            });
            res.end();
          }
        } catch {
          /* ignore */
        }
        return;
      }
      sendJson(res, 502, {
        type: "error",
        error: { type: "api_error", message: `adapter forward failed: ${message}` },
      });
    }
  }
}
