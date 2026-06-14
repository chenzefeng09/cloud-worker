/**
 * adapter.test.ts — GatewayAdapter contract tests.
 *
 * We start a real adapter on a random port, mock global.fetch to capture what
 * the adapter forwards to the gateway and to return a canned OpenAI response,
 * then assert the Anthropic⇄OpenAI translation, header injection, de-dup, and
 * /healthz behaviour.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayAdapter } from "../src/adapter.js";

interface CapturedRequest {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

let adapter: GatewayAdapter;
let baseUrl: string;
let captured: CapturedRequest | null;
let nextOpenAIResponse: unknown;

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
    return new Response(JSON.stringify(nextOpenAIResponse), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as unknown as typeof fetch;
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
