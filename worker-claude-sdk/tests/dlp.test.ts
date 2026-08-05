/**
 * dlp.test.ts — DLP PreToolUse client contract tests.
 *
 * Mocks global.fetch (mirroring adapter.test.ts) to capture the request the
 * client sends to `${dlpUrl}/core/scan` and to return canned DLP verdicts, then
 * asserts allow/deny and the fail-OPEN posture (DLP unset / non-2xx / network
 * error / timeout all allow).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { scanToolCall } from "../src/dlp.js";

interface CapturedRequest {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

let captured: CapturedRequest | null;

/**
 * Install a fetch mock that records the outbound DLP request and returns a
 * canned Response. `responder` decides the Response (or throws to simulate a
 * network/timeout error).
 */
function installFetchMock(
  responder: () => Response | Promise<Response>,
): void {
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
  ) as unknown as typeof fetch;
}

function jsonResponse(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  // Silence the fail-open WARN logs; they are expected on the error-path tests.
  vi.spyOn(console, "warn").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("scanToolCall", () => {
  it("allows when DLP returns action=ALLOW and sends the right request", async () => {
    installFetchMock(() => jsonResponse({ action: "ALLOW", max_sensitivity: null, hits: [] }));

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Read",
      toolInput: { file_path: "/workspace/inputs/a.txt" },
      toolUseId: "tu_123",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("allow");
    expect(global.fetch).toHaveBeenCalledTimes(1);

    // Request shape contract.
    expect(captured?.url).toBe("http://dlp.test/core/scan");
    // The broker's _require_auth 401s before checking the lease unless the
    // Foundation bearer is present (mirrors the MCP-registry fetch) — this is
    // the regression guard for the DLP-scan-401 bug.
    expect(captured?.headers["authorization"]).toBe("Bearer tok-foundation");
    expect(captured?.headers["x-tenant-id"]).toBe("tnt-abc");
    expect(captured?.headers["content-type"]).toBe("application/json");
    expect(captured?.body.resource_type).toBe("tool_call");
    // resource_id folds in tool name + tool_use_id.
    expect(captured?.body.resource_id).toBe("Read:tu_123");
    // content is the JSON-stringified tool input.
    expect(captured?.body.content).toBe(
      JSON.stringify({ file_path: "/workspace/inputs/a.txt" }),
    );
    // No broker lease header when none supplied.
    expect(captured?.headers["x-tool-broker-lease"]).toBeUndefined();
  });

  it("forwards the broker lease header when supplied (broker mode)", async () => {
    installFetchMock(() => jsonResponse({ action: "ALLOW" }));

    await scanToolCall({
      dlpUrl: "http://broker.test/dlp",
      toolName: "Bash",
      toolInput: { command: "ls" },
      toolUseId: "tu_9",
      headers: {
        apiToken: "tok-foundation",
        tenantId: "tnt-abc",
        brokerLease: "v1.payload.sig",
      },
    });

    // Broker mode forwards both the bearer (for _require_auth) and the lease.
    expect(captured?.headers["authorization"]).toBe("Bearer tok-foundation");
    expect(captured?.headers["x-tool-broker-lease"]).toBe("v1.payload.sig");
  });

  it("denies when DLP returns a non-ALLOW action, with action + sensitivity in the reason", async () => {
    installFetchMock(() =>
      jsonResponse({
        action: "QUARANTINE",
        max_sensitivity: "L4",
        hits: [{ rule: "ssn" }],
      }),
    );

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Write",
      toolInput: { content: "123-45-6789" },
      toolUseId: "tu_7",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("deny");
    expect(result.reason).toContain("QUARANTINE");
    expect(result.reason).toContain("L4");
    expect(result.reason).toContain("Write");
  });

  it("fails open (allow) when DLP returns a non-2xx status", async () => {
    installFetchMock(() => jsonResponse({ error: "down" }, 503));

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Read",
      toolInput: {},
      toolUseId: "tu_1",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("allow");
    expect(result.reason).toContain("503");
  });

  it("fails open (allow) when fetch rejects (network error)", async () => {
    installFetchMock(() => {
      throw new TypeError("fetch failed");
    });

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Read",
      toolInput: {},
      toolUseId: "tu_2",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("allow");
  });

  it("fails open (allow) when DLP returns an unparseable body", async () => {
    installFetchMock(
      () =>
        new Response("<html>not json</html>", {
          status: 200,
          headers: { "content-type": "text/html" },
        }),
    );

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Read",
      toolInput: {},
      toolUseId: "tu_3",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("allow");
  });

  it("fails open (allow) without any fetch when DLP_URL is empty", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const result = await scanToolCall({
      dlpUrl: "",
      toolName: "Read",
      toolInput: { x: 1 },
      toolUseId: "tu_4",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    });

    expect(result.decision).toBe("allow");
    expect(result.reason).toContain("not configured");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("fails open (allow) on timeout, and wires the AbortSignal", async () => {
    // Simulate the AbortController firing: the mock honours the passed signal and
    // rejects with an AbortError the instant it aborts — no real 2s sleep.
    installFetchMock((): Promise<Response> => {
      return new Promise((_resolve, reject) => {
        const signal = (global.fetch as unknown as { mock: { calls: unknown[][] } })
          .mock.calls[0][1] as RequestInit;
        const sig = signal.signal as AbortSignal;
        // Assert the timeout signal is actually wired into the request.
        expect(sig).toBeInstanceOf(AbortSignal);
        if (sig.aborted) {
          reject(new DOMException("aborted", "AbortError"));
          return;
        }
        sig.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    });

    const result = await scanToolCall({
      dlpUrl: "http://dlp.test",
      toolName: "Read",
      toolInput: {},
      toolUseId: "tu_5",
      headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
      // Inject a tiny timeout so the AbortSignal fires almost immediately.
      timeoutMs: 5,
    });

    expect(result.decision).toBe("allow");
  });
});
