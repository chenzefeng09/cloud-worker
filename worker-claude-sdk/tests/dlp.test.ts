/**
 * dlp.test.ts — DLP PreToolUse client contract + decision-table tests.
 *
 * Mocks global.fetch (mirroring adapter.test.ts) to capture the request the
 * client sends to `${dlpUrl}/core/scan` and to return canned DLP verdicts, then
 * asserts the decision table in dlp.ts:
 *
 *   DLP_URL unset                         → allow, no fetch
 *   2xx action=ALLOW                      → allow
 *   2xx action=SCAN_REQUIRED (L2/L3 hit)  → allow + console.warn (rules only)
 *   2xx action=QUARANTINE (L4+ hit)       → deny
 *   2xx any other action, or
 *   scan cannot complete                  → closed (default): deny
 *                                           open: allow + console.error
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockInstance,
} from "vitest";

import {
  logDlpPolicy,
  resolveDlpPolicy,
  scanToolCall,
  type DlpFailureMode,
  type ScanToolCallArgs,
} from "../src/dlp.js";

interface CapturedRequest {
  url: string;
  headers: Record<string, string>;
  body: Record<string, unknown>;
}

let captured: CapturedRequest | null;
type ConsoleSpy = MockInstance<Parameters<typeof console.log>, void>;
let warnSpy: ConsoleSpy;
let errorSpy: ConsoleSpy;
let logSpy: ConsoleSpy;

/**
 * Install a fetch mock that records the outbound DLP request and returns a
 * canned Response. `responder` decides the Response (or throws to simulate a
 * network/timeout error).
 */
function installFetchMock(
  responder: (init?: RequestInit) => Response | Promise<Response>,
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
      return responder(init);
    },
  ) as unknown as typeof fetch;
}

function jsonResponse(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** Default scan args; override per test. */
function args(over: Partial<ScanToolCallArgs> = {}): ScanToolCallArgs {
  return {
    dlpUrl: "http://dlp.test",
    toolName: "Read",
    toolInput: {},
    toolUseId: "tu_1",
    headers: { apiToken: "tok-foundation", tenantId: "tnt-abc" },
    ...over,
  };
}

/** The exact 403 the DLP service returned to every worker scan before the fix. */
const DLP_DENIED_403 = {
  type: "https://errors.foundation.example.com/foundation/dlp/policy/dlp_denied",
  status: 403,
  code: "FOUNDATION.DLP.POLICY.DLP_DENIED",
  detail: "Classification and DLP-rule management requires SERVICE or ADMIN authority.",
};

beforeEach(() => {
  warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
  logSpy = vi.spyOn(console, "log").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("scanToolCall: verdicts", () => {
  it("allows when DLP returns action=ALLOW and sends the right request", async () => {
    installFetchMock(() => jsonResponse({ action: "ALLOW", max_sensitivity: "L1", hits: [] }));

    const result = await scanToolCall(
      args({ toolInput: { file_path: "/workspace/inputs/a.txt" }, toolUseId: "tu_123" }),
    );

    expect(result.decision).toBe("allow");
    expect(global.fetch).toHaveBeenCalledTimes(1);

    // Request shape contract.
    expect(captured?.url).toBe("http://dlp.test/core/scan");
    // The broker's _require_auth 401s before checking the lease unless the
    // Foundation bearer is present (mirrors the MCP-registry fetch).
    expect(captured?.headers["authorization"]).toBe("Bearer tok-foundation");
    expect(captured?.headers["x-tenant-id"]).toBe("tnt-abc");
    expect(captured?.headers["content-type"]).toBe("application/json");
    // The worker never asserts an actor: DLP must treat it as an untrusted
    // caller (verdict only, no classification write).
    expect(captured?.headers["x-actor"]).toBeUndefined();
    expect(captured?.body.resource_type).toBe("tool_call");
    // resource_id folds in tool name + tool_use_id.
    expect(captured?.body.resource_id).toBe("Read:tu_123");
    // content is the JSON-stringified tool input.
    expect(captured?.body.content).toBe(
      JSON.stringify({ file_path: "/workspace/inputs/a.txt" }),
    );
    // No broker lease header when none supplied.
    expect(captured?.headers["x-tool-broker-lease"]).toBeUndefined();
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("strips trailing slashes from the DLP base URL", async () => {
    installFetchMock(() => jsonResponse({ action: "ALLOW" }));
    await scanToolCall(args({ dlpUrl: "http://broker.test/dlp/api/dlp/v1//" }));
    expect(captured?.url).toBe("http://broker.test/dlp/api/dlp/v1/core/scan");
  });

  it("forwards the broker lease header when supplied (broker mode)", async () => {
    installFetchMock(() => jsonResponse({ action: "ALLOW" }));

    await scanToolCall(
      args({
        dlpUrl: "http://broker.test/dlp",
        toolName: "Bash",
        toolInput: { command: "ls" },
        headers: {
          apiToken: "tok-foundation",
          tenantId: "tnt-abc",
          brokerLease: "v1.payload.sig",
        },
      }),
    );

    // Broker mode forwards both the bearer (for _require_auth) and the lease.
    expect(captured?.headers["authorization"]).toBe("Bearer tok-foundation");
    expect(captured?.headers["x-tool-broker-lease"]).toBe("v1.payload.sig");
  });

  it("allows an L2/L3 hit (SCAN_REQUIRED) and logs it with its rules, not the data", async () => {
    installFetchMock(() =>
      jsonResponse({
        action: "SCAN_REQUIRED",
        max_sensitivity: "L3",
        hits: [
          { rule_id: "r1", rule_name: "中国手机号", sensitivity: "L3" },
          { rule_id: "r2", rule_name: "邮箱", sensitivity: "L2" },
          { rule_id: "r3", rule_name: "邮箱", sensitivity: "L2" },
        ],
      }),
    );

    const result = await scanToolCall(
      args({ toolName: "Write", toolInput: { content: "13800138000 a@b.co" } }),
    );

    expect(result.decision).toBe("allow");
    expect(warnSpy).toHaveBeenCalledTimes(1);
    const logged = String(warnSpy.mock.calls[0][0]);
    expect(logged).toContain("Write:");
    expect(logged).toContain("SCAN_REQUIRED");
    expect(logged).toContain("L3");
    expect(logged).toContain("中国手机号, 邮箱");
    expect(logged).not.toContain("13800138000");
    expect(errorSpy).not.toHaveBeenCalled();
  });

  it("denies a QUARANTINE hit, naming the tool, action, sensitivity and matched rules", async () => {
    installFetchMock(() =>
      jsonResponse({
        action: "QUARANTINE",
        max_sensitivity: "L4",
        hits: [
          { rule_id: "r1", rule_name: "身份证号", sensitivity: "L4" },
          { rule_id: "r2", rule_name: "邮箱", sensitivity: "L2" },
          { rule_id: "r3", rule_name: "邮箱", sensitivity: "L2" },
        ],
      }),
    );

    const result = await scanToolCall(
      args({ toolName: "Write", toolInput: { content: "110101199003071234 a@b.co" } }),
    );

    expect(result.decision).toBe("deny");
    expect(result.reason).toContain("Write");
    expect(result.reason).toContain("QUARANTINE");
    expect(result.reason).toContain("L4");
    // Distinct rule names, so the model knows what to remove.
    expect(result.reason).toContain("身份证号, 邮箱");
    expect(result.reason).not.toContain("邮箱, 邮箱");
  });

  it("treats an action the contract does not define as a scan that cannot complete", async () => {
    installFetchMock(() => jsonResponse({ action: "REDACT", max_sensitivity: "L3" }));

    const closed = await scanToolCall(args({ toolName: "Write" }));
    expect(closed.decision).toBe("deny");
    expect(closed.reason).toContain("does not recognise");

    const open = await scanToolCall(args({ toolName: "Write", failureMode: "open" }));
    expect(open.decision).toBe("allow");
    expect(String(errorSpy.mock.calls.at(-1)?.[0])).toContain("REDACT");
  });

  it("denies a hit without rule names using a generic description", async () => {
    installFetchMock(() =>
      jsonResponse({ action: "QUARANTINE", max_sensitivity: "L4", hits: [{ rule: "ssn" }] }),
    );

    const result = await scanToolCall(args({ toolName: "Write" }));

    expect(result.decision).toBe("deny");
    expect(result.reason).toContain("QUARANTINE");
    expect(result.reason).toContain("L4");
    expect(result.reason).toContain("classified as sensitive");
  });

  it("a hit is denied even in open mode (open only covers scans that cannot complete)", async () => {
    installFetchMock(() => jsonResponse({ action: "QUARANTINE", max_sensitivity: "L4" }));
    const result = await scanToolCall(args({ failureMode: "open" }));
    expect(result.decision).toBe("deny");
  });

  it("allows without any fetch when DLP_URL is empty (DLP disabled), in either mode", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    for (const failureMode of ["open", "closed"] as DlpFailureMode[]) {
      const result = await scanToolCall(args({ dlpUrl: "", failureMode, toolInput: { x: 1 } }));
      expect(result.decision).toBe("allow");
      expect(result.reason).toContain("disabled");
    }
    expect(fetchSpy).not.toHaveBeenCalled();
    // "Disabled" is logged once at startup (logDlpPolicy), never per call.
    expect(logSpy).not.toHaveBeenCalled();
    expect(warnSpy).not.toHaveBeenCalled();
    expect(errorSpy).not.toHaveBeenCalled();
  });
});

describe("scanToolCall: a scan that cannot complete", () => {
  /** Each way a scan can fail, with the user-facing cause it must report. */
  const failures: Array<{
    name: string;
    install: () => void;
    cause: string;
    timeoutMs?: number;
  }> = [
    {
      name: "non-2xx (the pre-fix 403 DLP_DENIED)",
      install: () => installFetchMock(() => jsonResponse(DLP_DENIED_403, 403)),
      cause: "HTTP 403",
    },
    {
      name: "non-2xx with a non-JSON body",
      install: () =>
        installFetchMock(() => new Response("Bad Gateway", { status: 502 })),
      cause: "HTTP 502",
    },
    {
      name: "network error",
      install: () =>
        installFetchMock(() => {
          throw new TypeError("fetch failed");
        }),
      cause: "could not be reached",
    },
    {
      name: "timeout",
      // Honour the passed AbortSignal and reject the instant it fires — no
      // real 2s sleep (timeoutMs is injected below).
      install: () =>
        installFetchMock(
          (init) =>
            new Promise<Response>((_resolve, reject) => {
              const sig = init?.signal as AbortSignal;
              expect(sig).toBeInstanceOf(AbortSignal);
              const fire = () =>
                reject(sig.reason ?? new DOMException("aborted", "AbortError"));
              if (sig.aborted) fire();
              else sig.addEventListener("abort", fire);
            }),
        ),
      cause: "did not answer in time",
      timeoutMs: 5,
    },
    {
      name: "unparseable 2xx body",
      install: () =>
        installFetchMock(
          () =>
            new Response("<html>not json</html>", {
              status: 200,
              headers: { "content-type": "text/html" },
            }),
        ),
      cause: "unreadable response",
    },
    {
      name: "2xx body without an action",
      install: () => installFetchMock(() => jsonResponse({ hits: [] })),
      cause: "unreadable response",
    },
    {
      name: "2xx JSON null body",
      install: () => installFetchMock(() => jsonResponse(null)),
      cause: "unreadable response",
    },
    {
      name: "2xx body with a non-string action",
      install: () => installFetchMock(() => jsonResponse({ action: 1 })),
      cause: "unreadable response",
    },
  ];

  for (const f of failures) {
    it(`${f.name}: closed (the default) denies with a clear reason`, async () => {
      f.install();

      // failureMode omitted on purpose: the default must be closed.
      const result = await scanToolCall(
        args({ toolName: "Bash", toolUseId: "tu_9", timeoutMs: f.timeoutMs }),
      );

      expect(result.decision).toBe("deny");
      expect(result.reason).toContain("Bash");
      expect(result.reason).toContain("could not be completed");
      expect(result.reason).toContain(f.cause);
      // Never leak the internal endpoint or credentials to the model/user.
      expect(result.reason).not.toContain("dlp.test");
      expect(result.reason).not.toContain("tok-foundation");
      expect(errorSpy).toHaveBeenCalledTimes(1);
      expect(String(errorSpy.mock.calls[0][0])).toContain("Bash:tu_9");
      expect(String(errorSpy.mock.calls[0][0])).toContain("DLP_FAILURE_MODE=closed");
    });

    it(`${f.name}: open allows, but logs the unscanned call as an error`, async () => {
      f.install();

      const result = await scanToolCall(
        args({ toolName: "Bash", toolUseId: "tu_9", failureMode: "open", timeoutMs: f.timeoutMs }),
      );

      expect(result.decision).toBe("allow");
      expect(errorSpy).toHaveBeenCalledTimes(1);
      const line = String(errorSpy.mock.calls[0][0]);
      expect(line).toContain("Bash:tu_9");
      expect(line).toContain("UNSCANNED");
      expect(line).toContain("DLP_FAILURE_MODE=open");
    });
  }

  it("logs the DLP problem code for a non-2xx, so the cause is visible to operators", async () => {
    installFetchMock(() => jsonResponse(DLP_DENIED_403, 403));
    await scanToolCall(args());
    expect(String(errorSpy.mock.calls[0][0])).toContain(
      "HTTP 403 FOUNDATION.DLP.POLICY.DLP_DENIED",
    );
  });

  it("logs a tool-broker rejection's detail (no problem code)", async () => {
    installFetchMock(() =>
      jsonResponse({ detail: "tool-broker lease expired" }, 403),
    );
    const result = await scanToolCall(args());
    expect(result.decision).toBe("deny");
    expect(String(errorSpy.mock.calls[0][0])).toContain(
      "HTTP 403 tool-broker lease expired",
    );
    // The broker's internal detail stays in the logs, not in the user-facing reason.
    expect(result.reason).not.toContain("lease");
  });

  it("an unserializable tool input is not scanned as a placeholder: closed denies, no fetch", async () => {
    const fetchSpy = vi.fn();
    global.fetch = fetchSpy as unknown as typeof fetch;

    const closed = await scanToolCall(args({ toolInput: { n: 1n } }));
    const open = await scanToolCall(args({ toolInput: { n: 1n }, failureMode: "open" }));

    expect(closed.decision).toBe("deny");
    expect(closed.reason).toContain("could not be prepared for scanning");
    expect(open.decision).toBe("allow");
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(errorSpy).toHaveBeenCalledTimes(2);
  });
});

describe("resolveDlpPolicy", () => {
  it("defaults to closed when DLP_FAILURE_MODE is unset", () => {
    expect(resolveDlpPolicy({ DLP_URL: "http://dlp", DLP_FAILURE_MODE: "" })).toEqual({
      url: "http://dlp",
      failureMode: "closed",
    });
  });

  it("accepts open/closed case-insensitively and trims whitespace", () => {
    expect(resolveDlpPolicy({ DLP_URL: " http://dlp ", DLP_FAILURE_MODE: " OPEN " })).toEqual({
      url: "http://dlp",
      failureMode: "open",
    });
    expect(
      resolveDlpPolicy({ DLP_URL: "http://dlp", DLP_FAILURE_MODE: "Closed" }).failureMode,
    ).toBe("closed");
  });

  it("falls back to closed on an unrecognised value and records it", () => {
    const policy = resolveDlpPolicy({ DLP_URL: "http://dlp", DLP_FAILURE_MODE: "opne" });
    expect(policy.failureMode).toBe("closed");
    expect(policy.invalidFailureMode).toBe("opne");
  });

  it("a blank DLP_URL means DLP is disabled", () => {
    expect(resolveDlpPolicy({ DLP_URL: "  ", DLP_FAILURE_MODE: "" }).url).toBe("");
  });
});

describe("logDlpPolicy", () => {
  it("says DLP is disabled when DLP_URL is unset", () => {
    logDlpPolicy({ url: "", failureMode: "closed" });
    expect(logSpy).toHaveBeenCalledTimes(1);
    expect(String(logSpy.mock.calls[0][0])).toContain("DLP disabled");
  });

  it("states the failure mode when DLP is enabled", () => {
    logDlpPolicy({ url: "http://dlp", failureMode: "closed" });
    logDlpPolicy({ url: "http://dlp", failureMode: "open" });
    expect(String(logSpy.mock.calls[0][0])).toContain("DLP_FAILURE_MODE=closed");
    expect(String(logSpy.mock.calls[1][0])).toContain("DLP_FAILURE_MODE=open");
    expect(String(logSpy.mock.calls[1][0])).toContain("UNSCANNED");
  });

  it("warns about an unrecognised failure mode", () => {
    logDlpPolicy({ url: "http://dlp", failureMode: "closed", invalidFailureMode: "opne" });
    expect(warnSpy).toHaveBeenCalledTimes(1);
    expect(String(warnSpy.mock.calls[0][0])).toContain('"opne"');
  });
});
