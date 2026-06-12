/**
 * adapter.ts — thin Anthropic ⇄ OpenAI bridge for the claude-sdk-spike.
 *
 * Purpose (spike): the Claude Agent SDK speaks the Anthropic Messages protocol
 * and authenticates with a single `Authorization: Bearer <token>`. The existing
 * llm-gateway speaks ONLY the OpenAI chat-completions protocol and demands three
 * headers (Authorization + X-Tenant-Id + X-LLM-Virtual-Key). This server sits in
 * between and does the minimal translation:
 *
 *   SDK ──Anthropic POST /v1/messages──▶ adapter ──OpenAI POST /chat/completions──▶ llm-gateway ──▶ DeepSeek
 *
 * It is deliberately tiny and non-production: no streaming, no retries, no schema
 * validation framework. It exists to prove the three spike claims (see README).
 *
 * Reads env: LLM_GATEWAY_URL, ADAPTER_TARGET_MODEL, ADAPTER_PORT.
 */

import http from "node:http";
import { Buffer } from "node:buffer";

// ── Config ────────────────────────────────────────────────────────────────
const PORT = Number(process.env.ADAPTER_PORT ?? 8788);
const GATEWAY_URL = (process.env.LLM_GATEWAY_URL ?? "http://localhost:8011/api/llm-gateway/v1").replace(/\/+$/, "");
// The SDK sends a Claude model name; the gateway only knows the registered
// DeepSeek model. We rewrite every inbound model to this.
const TARGET_MODEL = process.env.ADAPTER_TARGET_MODEL ?? "deepseek-chat";

// ── Token scheme (spike-only) ───────────────────────────────────────────────
// Bearer token is base64( JSON ) of { tenant_id, virtual_key, upstream_token }.
// upstream_token is the real Bearer the gateway expects. This lets a single
// Anthropic-style bearer carry all three pieces of identity the gateway needs.
// TODO(spike): production should not stuff the upstream creds into the client
// token; resolve tenant/virtual_key/upstream_token server-side from a session.
interface ResolvedAuth {
  tenant_id: string;
  virtual_key: string;
  upstream_token: string;
}

function decodeBearer(authHeader: string | undefined): ResolvedAuth | null {
  if (!authHeader) return null;
  const m = /^Bearer\s+(.+)$/i.exec(authHeader.trim());
  if (!m) return null;
  try {
    const json = Buffer.from(m[1], "base64").toString("utf8");
    const obj = JSON.parse(json);
    if (!obj.tenant_id || !obj.virtual_key || !obj.upstream_token) return null;
    return obj as ResolvedAuth;
  } catch {
    return null;
  }
}

// ── Anthropic → OpenAI message mapping ──────────────────────────────────────
// Anthropic message content may be a plain string OR an array of content blocks
// ({type:"text",text}, plus tool_use/tool_result/image we don't handle here).
// We flatten to a single OpenAI string. Spike scope = text only.
function flattenContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((block: any) => {
        if (block?.type === "text" && typeof block.text === "string") return block.text;
        // tool_result blocks carry their payload under `content`.
        if (block?.type === "tool_result") return flattenContent(block.content);
        // TODO(spike): images / tool_use blocks are dropped — fine for this spike.
        return "";
      })
      .filter(Boolean)
      .join("\n");
  }
  return "";
}

// ── Tool schema mapping: Anthropic tools -> OpenAI functions ────────────────
// Anthropic: { name, description, input_schema }
// OpenAI:    { type:"function", function:{ name, description, parameters } }
function anthropicToolsToOpenAI(tools: any[]): any[] {
  return (tools ?? [])
    .filter((t: any) => t && t.name && t.input_schema)
    .map((t: any) => ({
      type: "function",
      function: { name: t.name, description: t.description ?? "", parameters: t.input_schema },
    }));
}

// One Anthropic message may expand into several OpenAI messages, because an
// assistant turn carrying tool_use blocks becomes an assistant message with
// `tool_calls`, and each Anthropic tool_result block becomes its own OpenAI
// `{role:"tool"}` message keyed by tool_call_id. This is what makes a multi-turn
// tool loop survive the round-trip — without it, the hook-deny follow-up turn
// loses the tool linkage and the backend errors.
function anthropicMessageToOpenAI(m: any): any[] {
  const content = m.content;
  if (typeof content === "string") return [{ role: m.role, content }];
  if (!Array.isArray(content)) return [{ role: m.role, content: "" }];

  if (m.role === "assistant") {
    const text = content.filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n");
    // De-dup tool_use blocks by id WITHIN this assistant message. After a hook
    // deny, the SDK can replay the denied tool_use twice in the same turn; two
    // tool_calls with the same id makes DeepSeek 400 ("Duplicate value for
    // 'tool_call_id'"). Keep the first occurrence of each id.
    const seen = new Set<string>();
    const toolUses = content.filter(
      (b: any) => b?.type === "tool_use" && b.id && !seen.has(b.id) && seen.add(b.id),
    );
    const msg: any = { role: "assistant", content: text || null };
    if (toolUses.length) {
      msg.tool_calls = toolUses.map((b: any) => ({
        id: b.id,
        type: "function",
        function: { name: b.name, arguments: JSON.stringify(b.input ?? {}) },
      }));
    }
    return [msg];
  }

  // user role: tool_result blocks -> OpenAI {role:"tool"} messages; plain text stays a user msg.
  const out: any[] = [];
  const toolResults = content.filter((b: any) => b?.type === "tool_result");
  for (const tr of toolResults) {
    out.push({ role: "tool", tool_call_id: tr.tool_use_id, content: flattenContent(tr.content) });
  }
  const text = content.filter((b: any) => b?.type === "text").map((b: any) => b.text).join("\n");
  if (text) out.push({ role: "user", content: text });
  return out.length ? out : [{ role: "user", content: "" }];
}

function anthropicToOpenAIBody(body: any): Record<string, unknown> {
  const messages: any[] = [];

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
  // tool_call. OpenAI/DeepSeek rejects both ("Duplicate value for 'tool_call_id'"
  // / "tool_call_id not found"). Keep only the first tool message per id, and
  // only those whose id was announced by a preceding assistant tool_call.
  const announced = new Set<string>(); // tool_call ids already emitted by some assistant turn
  const seenToolMsg = new Set<string>(); // tool result ids already emitted
  const cleaned: any[] = [];
  for (const msg of messages) {
    if (msg.role === "assistant" && Array.isArray(msg.tool_calls)) {
      // De-dup tool_calls ACROSS turns: a denied tool_use can be replayed with
      // the same id in a later assistant turn. Keep only ids not seen before.
      const fresh = msg.tool_calls.filter((tc: any) => tc.id && !announced.has(tc.id));
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

  if (process.env.ADAPTER_DEBUG === "1") {
    console.log("[adapter][debug] cleaned messages:", JSON.stringify(cleaned.map((m: any) => ({
      role: m.role,
      tool_calls: (m.tool_calls ?? []).map((t: any) => t.id),
      tool_call_id: m.tool_call_id,
      text: typeof m.content === "string" ? m.content.slice(0, 30) : undefined,
    }))));
  }

  const out: Record<string, unknown> = {
    model: TARGET_MODEL, // rewrite Claude model name -> gateway/DeepSeek model
    messages: cleaned,
    // Spike: force non-streaming regardless of what the SDK requested.
    // TODO(spike): implement SSE passthrough (OpenAI delta -> Anthropic
    // message_start/content_block_delta/message_stop events).
    stream: false,
  };

  // Tool definitions (enables real function calling on v4-pro).
  if (Array.isArray(body.tools) && body.tools.length) {
    out.tools = anthropicToolsToOpenAI(body.tools);
  }

  // Pass through common sampling params when present.
  if (body.max_tokens != null) out.max_tokens = body.max_tokens;
  if (body.temperature != null) out.temperature = body.temperature;
  if (body.top_p != null) out.top_p = body.top_p;
  if (body.stop_sequences != null) out.stop = body.stop_sequences;

  return out;
}

// ── OpenAI → Anthropic response mapping ─────────────────────────────────────
function openAIToAnthropicResponse(oa: any, requestedModel: string): Record<string, unknown> {
  const choice = oa?.choices?.[0] ?? {};
  const msg = choice.message ?? {};
  const usage = oa?.usage ?? {};

  // Build Anthropic content blocks: text first, then one tool_use per OpenAI tool_call.
  const content: any[] = [];
  if (msg.content) content.push({ type: "text", text: String(msg.content) });
  // De-dup tool_calls by id: v4-pro/DeepSeek can return the SAME tool_call id
  // twice in one response. Emitting two tool_use blocks with the same id makes
  // the next round-trip produce a duplicate tool_call_id that DeepSeek rejects.
  const rawCalls = msg.tool_calls ?? [];
  const seenIds = new Set<string>();
  const toolCalls = rawCalls.filter((tc: any) => {
    if (!tc.id || seenIds.has(tc.id)) return false;
    seenIds.add(tc.id);
    return true;
  });
  for (const tc of toolCalls) {
    let input: any = {};
    try {
      input = JSON.parse(tc.function?.arguments ?? "{}");
    } catch {
      input = { _raw: tc.function?.arguments }; // tolerate malformed args
    }
    content.push({ type: "tool_use", id: tc.id, name: tc.function?.name, input });
  }
  if (!content.length) content.push({ type: "text", text: "" });

  // stop_reason: tool_use when the model wants a tool, else end_turn.
  const stopReason = toolCalls.length || choice.finish_reason === "tool_calls" ? "tool_use" : "end_turn";

  return {
    id: oa?.id ?? `msg_spike_${Date.now()}`,
    type: "message",
    role: "assistant",
    model: requestedModel,
    content,
    stop_reason: stopReason,
    stop_sequence: null,
    usage: {
      input_tokens: usage.prompt_tokens ?? 0,
      output_tokens: usage.completion_tokens ?? 0,
    },
  };
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

const server = http.createServer(async (req, res) => {
  // Strip query string — the SDK appends `?beta=true` to /v1/messages.
  const pathname = (req.url ?? "").split("?")[0];

  // Anthropic Messages inbound endpoint. The SDK (via ANTHROPIC_BASE_URL) hits
  // `${base}/v1/messages` (sometimes with a `?beta=true` query param).
  if (req.method === "POST" && pathname === "/v1/messages") {
    const auth = decodeBearer(req.headers["authorization"] as string | undefined);
    if (!auth) {
      console.warn("[adapter] 401 — could not decode tenant/virtual_key/upstream_token from Authorization bearer");
      return sendJson(res, 401, { type: "error", error: { type: "authentication_error", message: "invalid spike bearer token" } });
    }
    // ── EVIDENCE LINE #1: tenant resolved from token ──
    console.log(`[adapter] resolved tenant=${auth.tenant_id} virtual_key=${auth.virtual_key} from token`);

    let inbound: any;
    try {
      inbound = JSON.parse((await readBody(req)) || "{}");
    } catch {
      return sendJson(res, 400, { type: "error", error: { type: "invalid_request_error", message: "bad JSON body" } });
    }

    const requestedModel: string = inbound.model ?? "unknown";

    // ── TEST HOOK (spike only): force a Bash tool_use so the PreToolUse hook
    // has a real tool call to intercept, independent of whether the backend
    // model emits proper tool_use blocks. Enable with ADAPTER_FORCE_BASH=1.
    // We only force it on the FIRST turn (no prior tool_result in the history),
    // so after the hook denies, the next turn falls through to the real model.
    // TODO(spike): remove once a tool-calling-capable backend is wired.
    if (process.env.ADAPTER_FORCE_BASH === "1") {
      const hasToolResult = (inbound.messages ?? []).some(
        (m: any) => Array.isArray(m.content) && m.content.some((b: any) => b?.type === "tool_result"),
      );
      if (!hasToolResult) {
        console.log("[adapter] ADAPTER_FORCE_BASH=1 — returning synthetic Bash tool_use (to exercise PreToolUse hook)");
        return sendJson(res, 200, {
          id: `msg_spike_force_${Date.now()}`,
          type: "message",
          role: "assistant",
          model: requestedModel,
          content: [
            { type: "text", text: "I'll run that for you." },
            { type: "tool_use", id: `toolu_spike_${Date.now()}`, name: "Bash", input: { command: "echo hello" } },
          ],
          stop_reason: "tool_use",
          stop_sequence: null,
          usage: { input_tokens: 5, output_tokens: 5 },
        });
      }
    }

    const openaiBody = anthropicToOpenAIBody(inbound);
    const target = `${GATEWAY_URL}/chat/completions`;

    // ── EVIDENCE LINE #2: forwarding to gateway ──
    console.log(`[adapter] forwarding to gateway model=${TARGET_MODEL} (inbound model=${requestedModel}) -> ${target}`);

    try {
      const gwResp = await fetch(target, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          // The three headers the gateway hard-requires:
          authorization: `Bearer ${auth.upstream_token}`,
          "x-tenant-id": auth.tenant_id,
          "x-llm-virtual-key": auth.virtual_key,
        },
        body: JSON.stringify(openaiBody),
      });

      const rawText = await gwResp.text();
      if (!gwResp.ok) {
        console.error(`[adapter] gateway error status=${gwResp.status} body=${rawText.slice(0, 500)}`);
        // Wrap as an Anthropic-style error so the SDK surfaces something sane.
        return sendJson(res, gwResp.status, {
          type: "error",
          error: { type: "api_error", message: `gateway ${gwResp.status}: ${rawText.slice(0, 500)}` },
        });
      }

      let oa: any;
      try {
        oa = JSON.parse(rawText);
      } catch {
        console.error(`[adapter] gateway returned non-JSON: ${rawText.slice(0, 200)}`);
        return sendJson(res, 502, { type: "error", error: { type: "api_error", message: "gateway returned non-JSON" } });
      }

      const anthropicResp = openAIToAnthropicResponse(oa, requestedModel);
      console.log(`[adapter] ok — returned ${(anthropicResp as any).usage.output_tokens} output tokens to SDK`);
      return sendJson(res, 200, anthropicResp);
    } catch (err: any) {
      console.error(`[adapter] forward failed: ${err?.message ?? err}`);
      return sendJson(res, 502, { type: "error", error: { type: "api_error", message: `adapter forward failed: ${err?.message ?? err}` } });
    }
  }

  // Convenience health endpoint for the adapter itself.
  if (req.method === "GET" && req.url === "/healthz") {
    return sendJson(res, 200, { status: "ok", service: "claude-sdk-spike-adapter" });
  }

  sendJson(res, 404, { type: "error", error: { type: "not_found", message: `no route for ${req.method} ${req.url}` } });
});

server.listen(PORT, () => {
  console.log(`[adapter] listening on http://localhost:${PORT}`);
  console.log(`[adapter] -> gateway base: ${GATEWAY_URL}`);
  console.log(`[adapter] -> rewriting model to: ${TARGET_MODEL}`);
  console.log(`[adapter] POST /v1/messages to use; GET /healthz to ping`);
});
