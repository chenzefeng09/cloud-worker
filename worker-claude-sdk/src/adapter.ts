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
function anthropicToolsToOpenAI(tools: Json[]): Json[] {
  return (tools ?? [])
    .filter((t: Json) => t && t.name && t.input_schema)
    .map((t: Json) => ({
      type: "function",
      function: { name: t.name, description: t.description ?? "", parameters: t.input_schema },
    }));
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

function anthropicToOpenAIBody(body: Json, targetModel: string): Record<string, unknown> {
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

  const out: Record<string, unknown> = {
    model: targetModel, // rewrite Claude model name -> gateway/backend model
    messages: cleaned,
    // Non-streaming for now. SSE passthrough is a later phase.
    stream: false,
  };

  if (Array.isArray(body.tools) && body.tools.length) {
    out.tools = anthropicToolsToOpenAI(body.tools);
  }

  if (body.max_tokens != null) out.max_tokens = body.max_tokens;
  if (body.temperature != null) out.temperature = body.temperature;
  if (body.top_p != null) out.top_p = body.top_p;
  if (body.stop_sequences != null) out.stop = body.stop_sequences;

  return out;
}

// ── OpenAI → Anthropic response mapping ─────────────────────────────────────
function openAIToAnthropicResponse(oa: Json, requestedModel: string): Record<string, unknown> {
  const choice = oa?.choices?.[0] ?? {};
  const msg = choice.message ?? {};
  const usage = oa?.usage ?? {};

  const content: Json[] = [];
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
  private readonly requestedPort: number;
  private server: http.Server | null = null;
  private boundPort = 0;

  constructor(opts: GatewayAdapterOptions) {
    this.gatewayUrl = (opts.gatewayUrl ?? DEFAULT_GATEWAY_URL).replace(/\/+$/, "");
    this.targetModel = opts.targetModel;
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

    // Gateway identity from the trusted process env (NOT from the SDK bearer).
    const foundationToken = process.env.FOUNDATION_API_TOKEN ?? "";
    const tenantId = process.env.FOUNDATION_TENANT_ID ?? "";
    const virtualKey = process.env.LLM_VIRTUAL_KEY ?? "";

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
    const openaiBody = anthropicToOpenAIBody(inbound, this.targetModel);
    const target = `${this.gatewayUrl}/chat/completions`;

    try {
      const gwResp = await fetch(target, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          // The three headers the gateway hard-requires, sourced from env.
          authorization: `Bearer ${foundationToken}`,
          "x-tenant-id": tenantId,
          "x-llm-virtual-key": virtualKey,
        },
        body: JSON.stringify(openaiBody),
      });

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
      sendJson(res, 502, {
        type: "error",
        error: { type: "api_error", message: `adapter forward failed: ${message}` },
      });
    }
  }
}
