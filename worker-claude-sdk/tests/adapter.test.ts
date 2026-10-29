/**
 * adapter.test.ts — GatewayAdapter contract tests.
 *
 * We start a real adapter on a random port, mock global.fetch to capture what
 * the adapter forwards to the gateway and to return a canned OpenAI response,
 * then assert the Anthropic⇄OpenAI translation, header injection, de-dup, and
 * /healthz behaviour.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayAdapter, gatewayErrorReason } from "../src/adapter.js";

interface CapturedRequest {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

let adapter: GatewayAdapter;
let baseUrl: string;
let captured: CapturedRequest | null;
let nextOpenAIResponse: unknown;
// When set, the gateway mock returns a text/event-stream body built from these
// raw SSE frames (each already newline-framed, e.g. "data: {...}\n\n").
let nextStreamFrames: string[] | null;
// When set, the mock emits any configured frames and then errors the response
// body.  This models a gateway stream that is truncated mid-tool-call.
let nextStreamError: Error | null;
// Optional non-2xx status to simulate a gateway error in streaming mode.
let nextGatewayStatus: number | null;

/**
 * Install a fetch mock that intercepts ONLY the adapter's outbound gateway
 * call (http://gateway.test/...) and records it, returning a canned OpenAI
 * response. All other fetches — notably the test's own calls to the real
 * adapter server — pass through to the real fetch.
 */
function installFetchMock(): void {
  captured = null;
  const realFetch = global.fetch.bind(global);
  global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    const urlStr = String(url);
    if (!urlStr.startsWith("http://gateway.test")) {
      return realFetch(url, init);
    }
    const headers: Record<string, string> = {};
    const h = init?.headers as Record<string, string> | undefined;
    if (h) for (const [k, v] of Object.entries(h)) headers[k.toLowerCase()] = v;
    captured = {
      url: urlStr,
      headers,
      body: JSON.parse((init?.body as string) ?? "{}"),
    };

    if (nextGatewayStatus != null) {
      return new Response(JSON.stringify({ error: "boom" }), {
        status: nextGatewayStatus,
        headers: { "content-type": "application/json" },
      });
    }

    // Streaming mode: return an OpenAI text/event-stream body. We deliberately
    // split frames across multiple ReadableStream reads (and even mid-frame) to
    // exercise the adapter's cross-read buffering.
    if (nextStreamFrames) {
      const encoder = new TextEncoder();
      const frames = nextStreamFrames;
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          for (const frame of frames) controller.enqueue(encoder.encode(frame));
          if (nextStreamError) controller.error(nextStreamError);
          else controller.close();
        },
      });
      return new Response(stream, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }

    return new Response(JSON.stringify(nextOpenAIResponse), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
}

/** Parsed Anthropic SSE event. */
interface SseEvent {
  type: string;
  data: Record<string, unknown>;
}

/** Read a `text/event-stream` Response body fully and parse Anthropic events. */
async function readSse(resp: Response): Promise<SseEvent[]> {
  const text = await resp.text();
  const events: SseEvent[] = [];
  for (const frame of text.split("\n\n")) {
    const trimmed = frame.trim();
    if (!trimmed) continue;
    let type = "";
    let data = "";
    for (const line of trimmed.split("\n")) {
      if (line.startsWith("event:")) type = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (!type) continue;
    events.push({ type, data: data ? (JSON.parse(data) as Record<string, unknown>) : {} });
  }
  return events;
}

/** POST an Anthropic /v1/messages body through the adapter. */
async function postMessages(body: unknown): Promise<Response> {
  return fetch(`${baseUrl}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer sdk-ignored-token" },
    body: JSON.stringify(body),
  });
}

beforeEach(async () => {
  process.env.FOUNDATION_API_TOKEN = "tok-foundation";
  process.env.FOUNDATION_TENANT_ID = "tnt-abc";
  process.env.FOUNDATION_USER_ID = "usr-abc";
  process.env.LLM_VIRTUAL_KEY = "vk-xyz";
  process.env.TOOL_BROKER_LEASE = "v1.payload.signature";

  nextOpenAIResponse = {
    id: "cmpl-1",
    choices: [{ message: { content: "hello back" }, finish_reason: "stop" }],
    usage: { prompt_tokens: 10, completion_tokens: 4 },
  };
  nextStreamFrames = null;
  nextStreamError = null;
  nextGatewayStatus = null;
  installFetchMock();

  adapter = new GatewayAdapter({
    gatewayUrl: "http://gateway.test/api/llm-gateway/v1",
    targetModel: "deepseek-chat",
    port: 0,
  });
  const port = await adapter.start();
  baseUrl = `http://127.0.0.1:${port}`;
});

afterEach(async () => {
  await adapter.stop();
  vi.restoreAllMocks();
  delete process.env.FOUNDATION_API_TOKEN;
  delete process.env.FOUNDATION_TENANT_ID;
  delete process.env.FOUNDATION_USER_ID;
  delete process.env.LLM_VIRTUAL_KEY;
  delete process.env.TOOL_BROKER_LEASE;
});

describe("header injection (F.1)", () => {
  it("injects the three gateway headers from env, ignoring the SDK bearer", async () => {
    await postMessages({ model: "claude-3-5-sonnet", messages: [{ role: "user", content: "hi" }] });

    expect(captured).not.toBeNull();
    expect(captured!.url).toBe("http://gateway.test/api/llm-gateway/v1/chat/completions");
    expect(captured!.headers["authorization"]).toBe("Bearer tok-foundation");
    expect(captured!.headers["x-tenant-id"]).toBe("tnt-abc");
    expect(captured!.headers["x-user-id"]).toBe("usr-abc");
    expect(captured!.headers["x-llm-virtual-key"]).toBe("vk-xyz");
    expect(captured!.headers["x-tool-broker-lease"]).toBe("v1.payload.signature");
  });

  it("rewrites the inbound Claude model to the target model", async () => {
    await postMessages({ model: "claude-3-5-sonnet", messages: [{ role: "user", content: "hi" }] });
    expect(captured!.body.model).toBe("deepseek-chat");
  });
});

describe("Anthropic -> OpenAI request conversion", () => {
  it("converts tools to OpenAI functions", async () => {
    await postMessages({
      model: "claude",
      messages: [{ role: "user", content: "hi" }],
      tools: [
        {
          name: "Bash",
          description: "run a shell command",
          input_schema: { type: "object", properties: { command: { type: "string" } } },
        },
      ],
    });

    const tools = captured!.body.tools as Array<Record<string, unknown>>;
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      type: "function",
      function: {
        name: "Bash",
        description: "run a shell command",
        parameters: { type: "object", properties: { command: { type: "string" } } },
      },
    });
  });

  it("converts an assistant tool_use into an OpenAI tool_calls message", async () => {
    await postMessages({
      model: "claude",
      messages: [
        { role: "user", content: "run it" },
        {
          role: "assistant",
          content: [
            { type: "text", text: "sure" },
            { type: "tool_use", id: "toolu_1", name: "Bash", input: { command: "ls" } },
          ],
        },
      ],
    });

    const messages = captured!.body.messages as Array<Record<string, unknown>>;
    const assistant = messages.find((m) => m.role === "assistant")!;
    expect(assistant.content).toBe("sure");
    const toolCalls = assistant.tool_calls as Array<Record<string, unknown>>;
    expect(toolCalls).toHaveLength(1);
    expect(toolCalls[0]).toMatchObject({
      id: "toolu_1",
      type: "function",
      function: { name: "Bash", arguments: JSON.stringify({ command: "ls" }) },
    });
  });

  it("converts a user tool_result into an OpenAI {role:tool} message", async () => {
    await postMessages({
      model: "claude",
      messages: [
        {
          role: "assistant",
          content: [{ type: "tool_use", id: "toolu_9", name: "Bash", input: {} }],
        },
        {
          role: "user",
          content: [
            { type: "tool_result", tool_use_id: "toolu_9", content: "exit 0" },
          ],
        },
      ],
    });

    const messages = captured!.body.messages as Array<Record<string, unknown>>;
    const toolMsg = messages.find((m) => m.role === "tool")!;
    expect(toolMsg).toMatchObject({ role: "tool", tool_call_id: "toolu_9", content: "exit 0" });
  });

  it("de-dups repeated tool_use ids within one assistant message (request side)", async () => {
    await postMessages({
      model: "claude",
      messages: [
        {
          role: "assistant",
          content: [
            { type: "tool_use", id: "dup_1", name: "Bash", input: { command: "a" } },
            { type: "tool_use", id: "dup_1", name: "Bash", input: { command: "a" } },
          ],
        },
      ],
    });

    const messages = captured!.body.messages as Array<Record<string, unknown>>;
    const assistant = messages.find((m) => m.role === "assistant")!;
    const toolCalls = assistant.tool_calls as Array<Record<string, unknown>>;
    expect(toolCalls).toHaveLength(1);
    expect(toolCalls[0].id).toBe("dup_1");
  });
});

describe("OpenAI -> Anthropic response conversion", () => {
  it("converts OpenAI tool_calls into Anthropic tool_use with stop_reason=tool_use", async () => {
    nextOpenAIResponse = {
      id: "cmpl-2",
      choices: [
        {
          message: {
            content: "calling tool",
            tool_calls: [
              { id: "call_1", type: "function", function: { name: "Bash", arguments: '{"command":"ls"}' } },
            ],
          },
          finish_reason: "tool_calls",
        },
      ],
      usage: { prompt_tokens: 5, completion_tokens: 7 },
    };

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "go" }] });
    const out = (await resp.json()) as Record<string, unknown>;

    expect(out.stop_reason).toBe("tool_use");
    const content = out.content as Array<Record<string, unknown>>;
    const toolUse = content.find((b) => b.type === "tool_use")!;
    expect(toolUse).toMatchObject({ type: "tool_use", id: "call_1", name: "Bash", input: { command: "ls" } });
    expect(out.usage).toMatchObject({ input_tokens: 5, output_tokens: 7 });
  });

  it("de-dups repeated tool_call_ids in the OpenAI response (response side)", async () => {
    nextOpenAIResponse = {
      id: "cmpl-3",
      choices: [
        {
          message: {
            content: null,
            tool_calls: [
              { id: "call_dup", type: "function", function: { name: "Bash", arguments: "{}" } },
              { id: "call_dup", type: "function", function: { name: "Bash", arguments: "{}" } },
            ],
          },
          finish_reason: "tool_calls",
        },
      ],
      usage: {},
    };

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "go" }] });
    const out = (await resp.json()) as Record<string, unknown>;
    const content = out.content as Array<Record<string, unknown>>;
    const toolUses = content.filter((b) => b.type === "tool_use");
    expect(toolUses).toHaveLength(1);
  });

  it("maps DeepSeek prompt_cache_hit_tokens onto Anthropic cache_read_input_tokens", async () => {
    nextOpenAIResponse = {
      id: "cmpl-cache",
      choices: [{ message: { content: "cached" }, finish_reason: "stop" }],
      usage: {
        prompt_tokens: 5,
        completion_tokens: 10,
        total_tokens: 15,
        prompt_cache_hit_tokens: 3,
        prompt_cache_miss_tokens: 2,
        prompt_tokens_details: { cached_tokens: 3 },
      },
    };

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    const out = (await resp.json()) as Record<string, unknown>;

    expect(out.usage).toMatchObject({
      input_tokens: 5,
      output_tokens: 10,
      cache_read_input_tokens: 3,
    });
  });

  it("defaults cache_read_input_tokens to 0 when the upstream omits cache fields", async () => {
    nextOpenAIResponse = {
      id: "cmpl-nocache",
      choices: [{ message: { content: "x" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 8, completion_tokens: 2 },
    };

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    const out = (await resp.json()) as Record<string, unknown>;
    expect(out.usage).toMatchObject({ input_tokens: 8, output_tokens: 2, cache_read_input_tokens: 0 });
  });

  it("returns end_turn for a plain text completion", async () => {
    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    const out = (await resp.json()) as Record<string, unknown>;
    expect(out.stop_reason).toBe("end_turn");
    const content = out.content as Array<Record<string, unknown>>;
    expect(content[0]).toMatchObject({ type: "text", text: "hello back" });
  });
});

describe("streaming (Anthropic SSE passthrough)", () => {
  it("forwards stream:true and stream_options.include_usage to the gateway", async () => {
    nextStreamFrames = ["data: [DONE]\n\n"];
    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    await readSse(resp); // drain

    expect(captured!.body.stream).toBe(true);
    expect(captured!.body.stream_options).toMatchObject({ include_usage: true });
  });

  it("keeps stream:false (no stream_options) when stream is absent", async () => {
    await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    expect(captured!.body.stream).toBe(false);
    expect(captured!.body.stream_options).toBeUndefined();
  });

  it("translates OpenAI text chunks into per-token Anthropic text_delta events", async () => {
    nextStreamFrames = [
      'data: {"id":"c1","choices":[{"index":0,"delta":{"role":"assistant","content":"Hel"}}]}\n\n',
      'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"lo "}}]}\n\n',
      'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"world"}}]}\n\n',
      'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
      'data: {"id":"c1","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude-3-5-sonnet",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    expect(resp.headers.get("content-type")).toContain("text/event-stream");
    const events = await readSse(resp);
    const types = events.map((e) => e.type);

    expect(types[0]).toBe("message_start");
    expect((events[0].data.message as Record<string, unknown>).model).toBe("claude-3-5-sonnet");
    expect(types).toContain("content_block_start");
    expect(types[types.length - 1]).toBe("message_stop");

    // The three text fragments must arrive as text_delta in order.
    const textDeltas = events
      .filter((e) => e.type === "content_block_delta")
      .map((e) => (e.data.delta as Record<string, unknown>).text);
    expect(textDeltas).toEqual(["Hel", "lo ", "world"]);

    // content_block_start (text) precedes the deltas; content_block_stop follows.
    const startIdx = types.indexOf("content_block_start");
    const stopIdx = types.indexOf("content_block_stop");
    const firstDeltaIdx = types.indexOf("content_block_delta");
    expect(startIdx).toBeLessThan(firstDeltaIdx);
    expect(stopIdx).toBeGreaterThan(firstDeltaIdx);
    expect((events[startIdx].data.content_block as Record<string, unknown>).type).toBe("text");

    // message_delta carries stop_reason + usage from the final chunk.
    const msgDelta = events.find((e) => e.type === "message_delta")!;
    expect((msgDelta.data.delta as Record<string, unknown>).stop_reason).toBe("end_turn");
    expect(msgDelta.data.usage).toMatchObject({ output_tokens: 3, input_tokens: 12 });
  });

  it("translates streamed tool_calls into Anthropic tool_use blocks", async () => {
    nextStreamFrames = [
      // leading text
      'data: {"choices":[{"index":0,"delta":{"content":"calling"}}]}\n\n',
      // tool_call index 0: id+name in first fragment, then argument fragments
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"Bash","arguments":"{\\"comm"}}]}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"and\\":\\"ls\\"}"}}]}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
      'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":9,"prompt_cache_hit_tokens":2}}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "go" }],
    });
    const events = await readSse(resp);

    // Text block uses index 0; tool block uses the next sequential index 1.
    const toolStart = events.find(
      (e) =>
        e.type === "content_block_start" &&
        (e.data.content_block as Record<string, unknown>).type === "tool_use",
    )!;
    expect(toolStart).toBeDefined();
    const toolBlock = toolStart.data.content_block as Record<string, unknown>;
    expect(toolBlock).toMatchObject({ type: "tool_use", id: "call_1", name: "Bash" });
    expect(toolStart.data.index).toBe(1);

    // input_json_delta fragments arrive in order and reassemble valid JSON.
    const argFragments = events
      .filter(
        (e) =>
          e.type === "content_block_delta" &&
          (e.data.delta as Record<string, unknown>).type === "input_json_delta",
      )
      .map((e) => (e.data.delta as Record<string, unknown>).partial_json as string);
    expect(argFragments).toEqual(['{"comm', 'and":"ls"}']);
    expect(JSON.parse(argFragments.join(""))).toEqual({ command: "ls" });

    // stop_reason=tool_use, usage maps completion/prompt/cache tokens.
    const msgDelta = events.find((e) => e.type === "message_delta")!;
    expect((msgDelta.data.delta as Record<string, unknown>).stop_reason).toBe("tool_use");
    expect(msgDelta.data.usage).toMatchObject({
      output_tokens: 9,
      input_tokens: 5,
      cache_read_input_tokens: 2,
    });
  });

  it("surfaces a truncated gateway stream as an error instead of a fake tool-use completion", async () => {
    nextStreamFrames = [
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_cut","type":"function","function":{"name":"Write","arguments":"{\\"file_path\\":\\"/workspace/a.m\\","}}]}}]}\n\n',
    ];
    nextStreamError = new Error("upstream stream reset");

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "write a large file" }],
    });
    const events = await readSse(resp);
    const types = events.map((event) => event.type);

    expect(types).toContain("error");
    expect(types).not.toContain("message_delta");
    expect(types).not.toContain("message_stop");
    const error = events.find((event) => event.type === "error")!;
    expect(error.data).toMatchObject({
      type: "error",
      error: {
        type: "api_error",
        message: "gateway stream interrupted before completion",
      },
    });
  });

  it("preserves text fragment order even when frames are split across reads", async () => {
    // Split a single SSE frame across multiple ReadableStream reads to exercise
    // the cross-read buffering on \n\n boundaries.
    nextStreamFrames = [
      'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}',
      "\n\n",
      'data: {"choices":[{"index":0,"de',
      'lta":{"content":"b"}}]}\n',
      "\n",
      'data: {"choices":[{"index":0,"delta":{"content":"c"},"finish_reason":"stop"}]}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    const events = await readSse(resp);
    const textDeltas = events
      .filter((e) => e.type === "content_block_delta")
      .map((e) => (e.data.delta as Record<string, unknown>).text);
    expect(textDeltas).toEqual(["a", "b", "c"]);
  });

  it("de-dups tool_call ids across streamed fragments", async () => {
    // The gateway erroneously streams two tool_calls with the SAME id under two
    // different OpenAI indices. Only one Anthropic tool_use block must open.
    nextStreamFrames = [
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"dup","type":"function","function":{"name":"Bash","arguments":"{}"}}]}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":1,"id":"dup","type":"function","function":{"name":"Bash","arguments":"{}"}}]}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "go" }],
    });
    const events = await readSse(resp);
    const toolStarts = events.filter(
      (e) =>
        e.type === "content_block_start" &&
        (e.data.content_block as Record<string, unknown>).type === "tool_use",
    );
    expect(toolStarts).toHaveLength(1);
    expect((toolStarts[0].data.content_block as Record<string, unknown>).id).toBe("dup");
  });

  it("emits an Anthropic SSE error event on gateway non-2xx in streaming mode", async () => {
    nextGatewayStatus = 503;
    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    expect(resp.headers.get("content-type")).toContain("text/event-stream");
    const events = await readSse(resp);
    expect(events.some((e) => e.type === "error")).toBe(true);
    const errEvent = events.find((e) => e.type === "error")!;
    expect((errEvent.data.error as Record<string, unknown>).type).toBe("api_error");
  });

  it("tolerates empty deltas and keep-alive comments in the stream", async () => {
    nextStreamFrames = [
      ": keep-alive\n\n",
      'data: {"choices":[{"index":0,"delta":{}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
      "data: [DONE]\n\n",
    ];
    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "hi" }],
    });
    const events = await readSse(resp);
    const textDeltas = events
      .filter((e) => e.type === "content_block_delta")
      .map((e) => (e.data.delta as Record<string, unknown>).text);
    expect(textDeltas).toEqual(["ok"]);
    expect(events[events.length - 1].type).toBe("message_stop");
  });
});

describe("thinking — request side", () => {
  it("maps Anthropic thinking{type:enabled,budget_tokens} to DeepSeek thinking{type:enabled}", async () => {
    await postMessages({
      model: "claude",
      thinking: { type: "enabled", budget_tokens: 2048 },
      messages: [{ role: "user", content: "think" }],
    });
    expect(captured!.body.thinking).toEqual({ type: "enabled" });
  });

  it("maps Anthropic thinking{type:adaptive} to enabled", async () => {
    await postMessages({
      model: "claude",
      thinking: { type: "adaptive" },
      messages: [{ role: "user", content: "think" }],
    });
    expect(captured!.body.thinking).toEqual({ type: "enabled" });
  });

  it("maps Anthropic thinking{type:disabled} to disabled", async () => {
    await postMessages({
      model: "claude",
      thinking: { type: "disabled" },
      messages: [{ role: "user", content: "no think" }],
    });
    expect(captured!.body.thinking).toEqual({ type: "disabled" });
  });

  it("omits thinking entirely when the request has none", async () => {
    await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    expect(captured!.body.thinking).toBeUndefined();
  });
});

describe("thinking — response side", () => {
  it("non-streaming: reasoning_content becomes a leading thinking content block", async () => {
    nextOpenAIResponse = {
      id: "cmpl-think",
      choices: [
        {
          message: { reasoning_content: "let me reason", content: "the answer" },
          finish_reason: "stop",
        },
      ],
      usage: { prompt_tokens: 5, completion_tokens: 6 },
    };

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "q" }] });
    const out = (await resp.json()) as Record<string, unknown>;
    const content = out.content as Array<Record<string, unknown>>;

    // thinking block precedes the text block.
    expect(content[0]).toMatchObject({ type: "thinking", thinking: "let me reason" });
    expect(content[1]).toMatchObject({ type: "text", text: "the answer" });
  });

  it("non-streaming: no thinking block when reasoning_content is absent", async () => {
    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    const out = (await resp.json()) as Record<string, unknown>;
    const content = out.content as Array<Record<string, unknown>>;
    expect(content.some((b) => b.type === "thinking")).toBe(false);
  });

  it("streaming: delta.reasoning_content becomes thinking block start/delta/stop before text", async () => {
    nextStreamFrames = [
      'data: {"choices":[{"index":0,"delta":{"reasoning_content":"step 1 "}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"reasoning_content":"step 2"}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"content":"final"}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
      'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":8}}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "q" }],
    });
    const events = await readSse(resp);

    // A thinking content_block_start opens at index 0 (before the text block).
    const thinkingStart = events.find(
      (e) =>
        e.type === "content_block_start" &&
        (e.data.content_block as Record<string, unknown>).type === "thinking",
    )!;
    expect(thinkingStart).toBeDefined();
    expect(thinkingStart.data.index).toBe(0);

    // thinking_delta fragments arrive in order.
    const thinkingDeltas = events
      .filter(
        (e) =>
          e.type === "content_block_delta" &&
          (e.data.delta as Record<string, unknown>).type === "thinking_delta",
      )
      .map((e) => (e.data.delta as Record<string, unknown>).thinking);
    expect(thinkingDeltas).toEqual(["step 1 ", "step 2"]);

    // The text block opens AFTER the thinking block (next index).
    const textStart = events.find(
      (e) =>
        e.type === "content_block_start" &&
        (e.data.content_block as Record<string, unknown>).type === "text",
    )!;
    expect(textStart.data.index).toBe(1);

    // Both blocks are closed.
    const stops = events.filter((e) => e.type === "content_block_stop").map((e) => e.data.index);
    expect(stops).toContain(0);
    expect(stops).toContain(1);
    expect(events[events.length - 1].type).toBe("message_stop");
  });

  // The CLI emits a block's consolidated message only at its content_block_stop,
  // so a block left open until the end of the response delays it. Each block is
  // closed as soon as the next one opens.
  it("streaming: closes the thinking block before the text opens, and the text before a tool", async () => {
    nextStreamFrames = [
      'data: {"choices":[{"index":0,"delta":{"reasoning_content":"plan"}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"content":"Checking."}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"Bash","arguments":"{}"}}]}}]}\n\n',
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
      "data: [DONE]\n\n",
    ];

    const resp = await postMessages({
      model: "claude",
      stream: true,
      messages: [{ role: "user", content: "q" }],
    });
    const events = await readSse(resp);
    const pos = (type: string, index: number) =>
      events.findIndex((e) => e.type === type && e.data.index === index);

    // index 0 = thinking, 1 = text, 2 = tool_use
    expect(pos("content_block_stop", 0)).toBeGreaterThan(-1);
    expect(pos("content_block_stop", 0)).toBeLessThan(pos("content_block_start", 1));
    expect(pos("content_block_stop", 1)).toBeLessThan(pos("content_block_start", 2));
    // Each block is closed exactly once.
    for (const i of [0, 1, 2]) {
      expect(events.filter((e) => e.type === "content_block_stop" && e.data.index === i)).toHaveLength(1);
    }
  });
});

describe("tool strict passthrough", () => {
  it("passes tool strict:true onto the OpenAI function definition", async () => {
    await postMessages({
      model: "claude",
      messages: [{ role: "user", content: "hi" }],
      tools: [
        {
          name: "Lookup",
          description: "strict tool",
          strict: true,
          input_schema: { type: "object", properties: { q: { type: "string" } } },
        },
      ],
    });
    const tools = captured!.body.tools as Array<Record<string, unknown>>;
    expect((tools[0].function as Record<string, unknown>).strict).toBe(true);
  });

  it("omits strict when the tool does not request it", async () => {
    await postMessages({
      model: "claude",
      messages: [{ role: "user", content: "hi" }],
      tools: [
        {
          name: "Plain",
          description: "plain tool",
          input_schema: { type: "object", properties: {} },
        },
      ],
    });
    const tools = captured!.body.tools as Array<Record<string, unknown>>;
    expect((tools[0].function as Record<string, unknown>).strict).toBeUndefined();
  });
});

describe("snapshot features (effort / max_tokens / structured outputs)", () => {
  // These three request-level features have NO SDK Options/wire path, so they
  // are carried in the adapter's trusted snapshot. Spin up a dedicated adapter
  // configured with them and assert they land on the OpenAI body.
  async function withFeatureAdapter(
    features: {
      reasoningEffort?: string;
      maxOutputTokens?: number;
      structuredOutputSchema?: unknown;
    },
    fn: (post: (body: unknown) => Promise<Response>) => Promise<void>,
  ): Promise<void> {
    const a = new GatewayAdapter({
      gatewayUrl: "http://gateway.test/api/llm-gateway/v1",
      targetModel: "deepseek-chat",
      port: 0,
      ...features,
    });
    const port = await a.start();
    const url = `http://127.0.0.1:${port}`;
    try {
      await fn((body: unknown) =>
        fetch(`${url}/v1/messages`, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: "Bearer x" },
          body: JSON.stringify(body),
        }),
      );
    } finally {
      await a.stop();
    }
  }

  it("emits reasoning_effort from the snapshot", async () => {
    await withFeatureAdapter({ reasoningEffort: "high" }, async (post) => {
      await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(captured!.body.reasoning_effort).toBe("high");
    });
  });

  it("max_tokens from the snapshot OVERRIDES the inbound model-default max_tokens", async () => {
    await withFeatureAdapter({ maxOutputTokens: 4096 }, async (post) => {
      // The SDK sets a model-default max_tokens on the inbound body; the snapshot
      // output cap must win.
      await post({ model: "claude", max_tokens: 8192, messages: [{ role: "user", content: "hi" }] });
      expect(captured!.body.max_tokens).toBe(4096);
    });
  });

  it("emits a json_schema response_format with the schema forwarded verbatim + strict:true", async () => {
    const schema = { type: "object", properties: { name: { type: "string" } }, required: ["name"] };
    await withFeatureAdapter({ structuredOutputSchema: schema }, async (post) => {
      await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      const rf = captured!.body.response_format as Record<string, unknown>;
      expect(rf.type).toBe("json_schema");
      const js = rf.json_schema as Record<string, unknown>;
      expect(js.strict).toBe(true);
      expect(js.schema).toEqual(schema); // verbatim
    });
  });

  it("emits NO response_format when no schema is configured", async () => {
    await withFeatureAdapter({}, async (post) => {
      await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(captured!.body.response_format).toBeUndefined();
    });
  });
});

describe("structured outputs — probe-and-downgrade fallback", () => {
  // A scripted gateway: each entry is the Response (or factory) returned for the
  // Nth outbound call to http://gateway.test. Records every captured request so
  // we can assert what the adapter sent on attempt #1 vs the retry.
  interface ScriptStep {
    status?: number;
    json?: unknown;
    frames?: string[]; // text/event-stream body
    body?: string; // raw error body (for non-2xx)
  }

  let scriptedCaptures: CapturedRequest[];

  function installScriptedFetchMock(steps: ScriptStep[]): void {
    scriptedCaptures = [];
    let call = 0;
    const realFetch = global.fetch.bind(global);
    global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      const urlStr = String(url);
      if (!urlStr.startsWith("http://gateway.test")) return realFetch(url, init);
      const headers: Record<string, string> = {};
      const h = init?.headers as Record<string, string> | undefined;
      if (h) for (const [k, v] of Object.entries(h)) headers[k.toLowerCase()] = v;
      scriptedCaptures.push({ url: urlStr, headers, body: JSON.parse((init?.body as string) ?? "{}") });

      const step = steps[Math.min(call, steps.length - 1)];
      call += 1;
      if (step.frames) {
        const encoder = new TextEncoder();
        const stream = new ReadableStream<Uint8Array>({
          start(controller) {
            for (const f of step.frames!) controller.enqueue(encoder.encode(f));
            controller.close();
          },
        });
        return new Response(stream, { status: step.status ?? 200, headers: { "content-type": "text/event-stream" } });
      }
      if (step.status && step.status >= 400) {
        return new Response(step.body ?? JSON.stringify(step.json ?? { error: "err" }), {
          status: step.status,
          headers: { "content-type": "application/json" },
        });
      }
      return new Response(JSON.stringify(step.json ?? nextOpenAIResponse), {
        status: step.status ?? 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
  }

  const schema = { type: "object", properties: { name: { type: "string" } }, required: ["name"] };

  async function withSchemaAdapter(
    fn: (post: (body: unknown) => Promise<Response>) => Promise<void>,
  ): Promise<void> {
    const a = new GatewayAdapter({
      gatewayUrl: "http://gateway.test/api/llm-gateway/v1",
      targetModel: "deepseek-chat",
      port: 0,
      structuredOutputSchema: schema,
    });
    const port = await a.start();
    const url = `http://127.0.0.1:${port}`;
    try {
      await fn((body: unknown) =>
        fetch(`${url}/v1/messages`, {
          method: "POST",
          headers: { "content-type": "application/json", authorization: "Bearer x" },
          body: JSON.stringify(body),
        }),
      );
    } finally {
      await a.stop();
    }
  }

  it("json_schema accepted: one request, no downgrade", async () => {
    installScriptedFetchMock([{ status: 200, json: nextOpenAIResponse }]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      const out = (await resp.json()) as Record<string, unknown>;
      expect(scriptedCaptures).toHaveLength(1);
      const rf = scriptedCaptures[0].body.response_format as Record<string, unknown>;
      expect(rf.type).toBe("json_schema");
      expect((out.content as Array<Record<string, unknown>>)[0]).toMatchObject({ type: "text", text: "hello back" });
    });
  });

  it("downgrades to json_object + schema hint and retries on a json_schema rejection (non-streaming)", async () => {
    installScriptedFetchMock([
      {
        status: 400,
        body: JSON.stringify({
          error: { message: "response_format type 'json_schema' is unsupported", type: "invalid_request_error" },
        }),
      },
      { status: 200, json: nextOpenAIResponse },
    ]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(resp.status).toBe(200);
      const out = (await resp.json()) as Record<string, unknown>;
      expect((out.content as Array<Record<string, unknown>>)[0]).toMatchObject({ type: "text", text: "hello back" });

      // Two calls: probe (json_schema) then retry (json_object).
      expect(scriptedCaptures).toHaveLength(2);
      expect((scriptedCaptures[0].body.response_format as Record<string, unknown>).type).toBe("json_schema");
      expect((scriptedCaptures[1].body.response_format as Record<string, unknown>).type).toBe("json_object");

      // The retry injects a schema-hint system message mentioning json + schema.
      const messages = scriptedCaptures[1].body.messages as Array<Record<string, unknown>>;
      const hint = messages[messages.length - 1];
      expect(hint.role).toBe("system");
      expect(String(hint.content).toLowerCase()).toContain("json");
      expect(String(hint.content)).toContain('"properties"'); // schema spelled out
    });
  });

  it("process-memory: after a downgrade, the next request goes straight to json_object (no re-probe)", async () => {
    installScriptedFetchMock([
      { status: 400, body: JSON.stringify({ error: { message: "unknown response_format type" } }) },
      { status: 200, json: nextOpenAIResponse },
      { status: 200, json: nextOpenAIResponse },
    ]);
    await withSchemaAdapter(async (post) => {
      await post({ model: "claude", messages: [{ role: "user", content: "first" }] }); // probe+retry => 2 calls
      await post({ model: "claude", messages: [{ role: "user", content: "second" }] }); // direct json_object => 1 call

      expect(scriptedCaptures).toHaveLength(3);
      // 3rd call (the second request) skips json_schema entirely.
      expect((scriptedCaptures[2].body.response_format as Record<string, unknown>).type).toBe("json_object");
      // …and it was a single attempt, not a probe+retry pair.
      const secondRequestCalls = scriptedCaptures.slice(2);
      expect(secondRequestCalls).toHaveLength(1);
    });
  });

  it("does NOT downgrade on a 429 (rate limit) — normal error path", async () => {
    installScriptedFetchMock([{ status: 429, body: JSON.stringify({ error: "response_format rate limit" }) }]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(resp.status).toBe(429);
      expect(scriptedCaptures).toHaveLength(1); // no retry
      expect((scriptedCaptures[0].body.response_format as Record<string, unknown>).type).toBe("json_schema");
    });
  });

  it("does NOT downgrade on a 500 — normal error path", async () => {
    installScriptedFetchMock([{ status: 500, body: "internal error referencing json_schema in a stack trace" }]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(resp.status).toBe(500);
      expect(scriptedCaptures).toHaveLength(1);
    });
  });

  it("does NOT downgrade on an unrelated 400 (no response_format/json_schema field named)", async () => {
    installScriptedFetchMock([
      { status: 400, body: JSON.stringify({ error: { message: "temperature must be <= 2" } }) },
    ]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(resp.status).toBe(400);
      expect(scriptedCaptures).toHaveLength(1); // no retry — unrelated 400
    });
  });

  it("does NOT downgrade when a 400 echoes 'json_schema' without a rejection hint", async () => {
    // A generic 400 that merely contains the literal "json_schema" (e.g. it
    // echoed back the request) but no unsupported/unknown/invalid hint must not
    // be misclassified as a capability gap.
    installScriptedFetchMock([
      { status: 400, body: JSON.stringify({ ok: false, received: { response_format: { type: "json_schema" } } }) },
    ]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", messages: [{ role: "user", content: "hi" }] });
      expect(resp.status).toBe(400);
      expect(scriptedCaptures).toHaveLength(1);
    });
  });

  it("streaming downgrade: probe rejects json_schema, retry streams json_object normally", async () => {
    installScriptedFetchMock([
      { status: 400, body: JSON.stringify({ error: { message: "response_format json_schema not supported" } }) },
      {
        status: 200,
        frames: [
          'data: {"choices":[{"index":0,"delta":{"content":"{\\"name\\":"}}]}\n\n',
          'data: {"choices":[{"index":0,"delta":{"content":"\\"x\\"}"}}]}\n\n',
          'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
          "data: [DONE]\n\n",
        ],
      },
    ]);
    await withSchemaAdapter(async (post) => {
      const resp = await post({ model: "claude", stream: true, messages: [{ role: "user", content: "hi" }] });
      expect(resp.headers.get("content-type")).toContain("text/event-stream");
      const events = await readSse(resp);

      // Two gateway calls: streaming probe (json_schema) then streaming retry (json_object).
      expect(scriptedCaptures).toHaveLength(2);
      expect(scriptedCaptures[0].body.stream).toBe(true);
      expect((scriptedCaptures[0].body.response_format as Record<string, unknown>).type).toBe("json_schema");
      expect(scriptedCaptures[1].body.stream).toBe(true);
      expect((scriptedCaptures[1].body.response_format as Record<string, unknown>).type).toBe("json_object");

      // The downgraded stream produces the expected Anthropic events + JSON text.
      expect(events[0].type).toBe("message_start");
      expect(events[events.length - 1].type).toBe("message_stop");
      const text = events
        .filter((e) => e.type === "content_block_delta")
        .map((e) => (e.data.delta as Record<string, unknown>).text)
        .join("");
      expect(text).toBe('{"name":"x"}');
    });
  });

  it("no schema configured: plain request is unaffected (no response_format, no retry on 400)", async () => {
    // Reuse the base (non-scripted) mock from beforeEach but force a 400 to prove
    // the no-schema path never enters the downgrade logic.
    nextGatewayStatus = 400;
    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });
    expect(resp.status).toBe(400);
    expect(captured!.body.response_format).toBeUndefined();
  });
});

describe("misc", () => {
  it("serves /healthz", async () => {
    const resp = await fetch(`${baseUrl}/healthz`);
    expect(resp.status).toBe(200);
    expect(await resp.json()).toEqual({ status: "ok" });
  });

  it("rejects a missing bearer with 401", async () => {
    const resp = await fetch(`${baseUrl}/v1/messages`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ model: "claude", messages: [] }),
    });
    expect(resp.status).toBe(401);
  });
});

describe("gateway error redaction", () => {
  // Upstream errors routinely echo the request; none of it may reach a log
  // line or the SDK-facing error, which becomes the run's error message.
  const PERSONAL = "Jane Doe, born 1980-01-01, jane@example.com";

  /** Make the gateway answer every call with `body` / `status`. */
  function gatewayAnswers(status: number, body: string): void {
    const previous = global.fetch;
    global.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) =>
      String(url).startsWith("http://gateway.test")
        ? new Response(body, { status, headers: { "content-type": "application/json" } })
        : previous(url, init),
    ) as unknown as typeof fetch;
  }

  function captureLogs(): () => string {
    const spies = (["error", "warn", "log", "info"] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => {}),
    );
    return () => spies.flatMap((spy) => spy.mock.calls.flat().map(String)).join("\n");
  }

  it.each([false, true])("keeps an echoing error body out of logs and errors (stream=%s)", async (stream) => {
    const logs = captureLogs();
    gatewayAnswers(
      400,
      JSON.stringify({
        error: { message: `invalid request: ${PERSONAL}`, type: "invalid_request_error", code: null },
      }),
    );

    const resp = await postMessages({
      model: "claude",
      stream,
      messages: [{ role: "user", content: PERSONAL }],
    });
    const text = await resp.text();

    expect(text).toContain("gateway 400: invalid_request_error");
    expect(text).not.toContain("Jane Doe");
    expect(logs()).toContain("status=400 reason=invalid_request_error");
    expect(logs()).not.toContain("Jane Doe");
  });

  it("logs only the size of a non-JSON success body", async () => {
    const logs = captureLogs();
    gatewayAnswers(200, `<html>${PERSONAL}</html>`);

    const resp = await postMessages({ model: "claude", messages: [{ role: "user", content: "hi" }] });

    expect(resp.status).toBe(502);
    expect(await resp.text()).not.toContain("Jane Doe");
    expect(logs()).toMatch(/non-JSON status=200 bytes=\d+/);
    expect(logs()).not.toContain("Jane Doe");
  });

  it("derives the reason from plain tokens only, else from the status", () => {
    const body = (error: unknown): string => JSON.stringify({ error });
    expect(gatewayErrorReason(400, body({ code: "context_length_exceeded", type: "x" }))).toBe(
      "context_length_exceeded",
    );
    expect(gatewayErrorReason(429, body({ code: 429, type: "rate_limit_error" }))).toBe("rate_limit_error");
    expect(gatewayErrorReason(503, body("boom"))).toBe("boom");
    expect(gatewayErrorReason(400, body({ code: PERSONAL, type: `bad: ${PERSONAL}` }))).toBe("bad_request");
    expect(gatewayErrorReason(502, `<html>${PERSONAL}</html>`)).toBe("bad_gateway");
    expect(gatewayErrorReason(418, "")).toBe("http_418");
    expect(gatewayErrorReason(500, body("x".repeat(65)))).toBe("upstream_error");
  });
});
