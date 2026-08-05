/**
 * web_fetch.test.ts — gateway endpoint joining + error-code mapping for the
 * in-process `mcp__webfetch__web_fetch` tool.
 *
 * Mocks global.fetch to capture the outbound gateway request and return canned
 * Responses, asserting the endpoint/header/body wire shape and the agent-readable
 * string vocabulary (which mirrors the AgentScope reference tool.py).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { runWebFetch, webFetchEndpoint } from "../src/worker.js";
import type { Env } from "../src/task.js";

function makeEnv(over: Partial<Env> = {}): Env {
  return {
    FOUNDATION_API_URL: "",
    FOUNDATION_API_TOKEN: "tok",
    FOUNDATION_TENANT_ID: "ten",
    FOUNDATION_USER_ID: "usr",
    FOUNDATION_SERVICE_SCOPES: "web.fetch",
    LLM_GATEWAY_URL: "",
    LLM_VIRTUAL_KEY: "",
    LLM_MODEL: "",
    S3_ENDPOINT: "",
    S3_BUCKET: "",
    S3_ACCESS_KEY_ID: "",
    S3_SECRET_ACCESS_KEY: "",
    S3_REGION: "",
    REDIS_URL: "",
    RUN_ID: "",
    MCP_REGISTRY_URL: "",
    SKILL_STORE_URL: "",
    WEB_FETCH_GATEWAY_URL: "http://wf:9000",
    TOOL_BROKER_URL: "",
    TOOL_BROKER_LEASE: "",
    SANDBOX_EGRESS_MODE: "",
    DLP_URL: "",
    OTEL_EXPORTER_OTLP_ENDPOINT: "",
    ...over,
  };
}

interface Captured {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}
let captured: Captured | null;

function installFetchMock(responder: () => Response | Promise<Response>): void {
  captured = null;
  global.fetch = vi.fn(
    async (url: string | URL | Request, init?: RequestInit) => {
      const headers: Record<string, string> = {};
      const h = init?.headers as Record<string, string> | undefined;
      if (h) for (const [k, v] of Object.entries(h)) headers[k.toLowerCase()] = v;
      captured = {
        url: String(url),
        headers,
        body: JSON.parse((init?.body as string) ?? "{}"),
      };
      return responder();
    },
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ) as any;
}

function jsonResponse(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("webFetchEndpoint", () => {
  it("appends only /fetch when base already ends in /api/web-fetch/v1 (broker)", () => {
    const base =
      "http://tool-broker:8024/api/tool-broker/v1/web-fetch/api/web-fetch/v1";
    expect(webFetchEndpoint(base)).toBe(`${base}/fetch`);
  });

  it("appends the full suffix for a plain base", () => {
    expect(webFetchEndpoint("http://wf:9000")).toBe(
      "http://wf:9000/api/web-fetch/v1/fetch",
    );
  });
});

describe("runWebFetch", () => {
  it("posts to the joined endpoint with identity headers + body, renders success", async () => {
    installFetchMock(() =>
      jsonResponse(200, {
        url: "https://x.test",
        status_code: 200,
        content_type: "text/html",
        body: "hello",
        truncated: false,
      }),
    );
    const out = await runWebFetch(makeEnv(), "https://x.test", undefined);
    expect(captured?.url).toBe("http://wf:9000/api/web-fetch/v1/fetch");
    expect(captured?.body).toEqual({ url: "https://x.test", max_bytes: 1048576 });
    expect(captured?.headers["authorization"]).toBe("Bearer tok");
    expect(captured?.headers["x-tenant-id"]).toBe("ten");
    expect(captured?.headers["x-user-id"]).toBe("usr");
    expect(captured?.headers["x-service-scopes"]).toBe("web.fetch");
    expect(out).toBe("[WebFetch] https://x.test — HTTP 200 (text/html)\n\nhello");
  });

  it("appends the truncation marker when truncated", async () => {
    installFetchMock(() =>
      jsonResponse(200, {
        status_code: 200,
        content_type: "text/plain",
        body: "abc",
        truncated: true,
      }),
    );
    const out = await runWebFetch(makeEnv(), "https://y.test", 10);
    expect(captured?.body).toEqual({ url: "https://y.test", max_bytes: 10 });
    expect(out).toContain("[... response truncated at limit ...]");
  });

  it("maps 403 to a [WebFetch blocked] message with detail", async () => {
    installFetchMock(() => jsonResponse(403, { detail: "policy denied" }));
    const out = await runWebFetch(makeEnv(), "https://z.test", undefined);
    expect(out).toBe("[WebFetch blocked] https://z.test: policy denied");
  });

  it("maps 504 to a gateway-timeout message", async () => {
    installFetchMock(() => jsonResponse(504, {}));
    const out = await runWebFetch(makeEnv(), "https://t.test", undefined);
    expect(out).toContain("[WebFetch error] Gateway timed out while fetching https://t.test");
  });

  it("maps other >=400 to a generic gateway-HTTP message", async () => {
    installFetchMock(() => jsonResponse(500, {}));
    const out = await runWebFetch(makeEnv(), "https://e.test", undefined);
    expect(out).toBe("[WebFetch error] Gateway returned HTTP 500 for https://e.test");
  });

  it("surfaces a body.error from a 2xx response", async () => {
    installFetchMock(() => jsonResponse(200, { error: "dns failure" }));
    const out = await runWebFetch(makeEnv(), "https://d.test", undefined);
    expect(out).toBe("[WebFetch error] https://d.test: dns failure");
  });

  it("returns a readable error when the gateway is unreachable", async () => {
    installFetchMock(() => {
      throw new Error("ECONNREFUSED");
    });
    const out = await runWebFetch(makeEnv(), "https://n.test", undefined);
    expect(out).toContain("[WebFetch error] Could not reach gateway");
  });

  it("omits identity headers that are empty", async () => {
    installFetchMock(() => jsonResponse(200, { status_code: 200, body: "" }));
    await runWebFetch(
      makeEnv({ FOUNDATION_USER_ID: "", FOUNDATION_SERVICE_SCOPES: "" }),
      "https://h.test",
      undefined,
    );
    expect(captured?.headers["x-user-id"]).toBeUndefined();
    expect(captured?.headers["x-service-scopes"]).toBeUndefined();
  });
});
