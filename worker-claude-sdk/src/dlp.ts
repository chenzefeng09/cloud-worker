/**
 * dlp.ts — Foundation-DLP client for the worker's PreToolUse hook.
 *
 * Mirrors the platform's `dlp_guard.py` (session-orchestrator):
 *   - POST `${dlpUrl}/core/scan` with `{content, resource_type, resource_id}`.
 *   - The DLP response is `{action, max_sensitivity, hits}`. `action === "ALLOW"`
 *     is clean (allow); any other action is a hit (deny).
 *   - `X-Tenant-Id` header is required; identity headers mirror the worker's
 *     authenticated MCP-registry fetch (conditional broker lease included).
 *   - 2-second timeout (matches dlp_guard's `_DLP_TIMEOUT = 2.0`).
 *
 * Policy is **fail-OPEN**: DLP unset / unreachable / timeout / non-2xx /
 * unparseable → ALLOW the tool call (log a WARN). A DLP outage must never brick
 * the agent. Only a successful 2xx parse with `action !== "ALLOW"` denies.
 *
 * Dependency-free: uses global fetch + AbortSignal, same as the rest of the
 * worker.
 */

/** DLP scan timeout — matches dlp_guard's `_DLP_TIMEOUT = 2.0` (seconds). */
const DLP_TIMEOUT_MS = 2000;

/** The verdict the PreToolUse hook turns into a permissionDecision. */
export interface DlpDecision {
  decision: "allow" | "deny";
  reason: string;
}

/** Identity headers forwarded to DLP (mirrors the worker's MCP-registry fetch). */
export interface DlpIdentityHeaders {
  /** Foundation API token; forwarded as `authorization: Bearer <token>`. The
   * broker's `_require_auth` rejects with 401 before checking the lease when this
   * is absent, so it must always be present (mirrors the MCP-registry fetch). */
  apiToken: string;
  tenantId: string;
  /** Present only in broker mode; forwarded as `x-tool-broker-lease` when set. */
  brokerLease?: string;
}

export interface ScanToolCallArgs {
  /** DLP base URL. Empty/unset → fail-open allow (no fetch). */
  dlpUrl: string;
  /** The tool being invoked (PreToolUseHookInput.tool_name). */
  toolName: string;
  /** The tool's arguments (PreToolUseHookInput.tool_input); JSON-serialized for `content`. */
  toolInput: unknown;
  /** The SDK tool_use_id; folded into `resource_id` for traceability. */
  toolUseId: string;
  /** Identity headers (tenant + conditional broker lease). */
  headers: DlpIdentityHeaders;
  /** Injectable timeout for tests; defaults to 2s. */
  timeoutMs?: number;
}

/** Shape of the DLP `/core/scan` response (only fields we read). */
interface DlpScanResponse {
  action?: unknown;
  max_sensitivity?: unknown;
  hits?: unknown;
}

/**
 * Scan a tool call against Foundation-DLP and return an allow/deny decision.
 *
 * Fail-OPEN at every branch: an empty `dlpUrl`, a network error, a timeout, a
 * non-2xx status, or an unparseable body all resolve to `{decision:"allow"}`
 * (a WARN is logged for the error branches). Only a successful 2xx parse whose
 * `action !== "ALLOW"` returns `{decision:"deny"}`, with a reason naming the
 * action and max_sensitivity.
 */
export async function scanToolCall(args: ScanToolCallArgs): Promise<DlpDecision> {
  const { dlpUrl, toolName, toolInput, toolUseId, headers } = args;

  // fail-OPEN: nothing to call when DLP is not wired into this run.
  if (!dlpUrl) {
    return { decision: "allow", reason: "DLP not configured" };
  }

  // content = the serialized tool arguments; resource ties the scan to the call.
  let content: string;
  try {
    content = JSON.stringify(toolInput ?? null);
  } catch {
    // Circular/unserializable input: scan a best-effort string rather than throw.
    content = String(toolInput);
  }
  const resourceId = `${toolName}:${toolUseId}`;
  const url = `${dlpUrl.replace(/\/+$/, "")}/core/scan`;

  let resp: Response;
  try {
    resp = await fetch(url, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${headers.apiToken}`,
        "x-tenant-id": headers.tenantId,
        ...(headers.brokerLease
          ? { "x-tool-broker-lease": headers.brokerLease }
          : {}),
      },
      body: JSON.stringify({
        content,
        resource_type: "tool_call",
        resource_id: resourceId,
      }),
      signal: AbortSignal.timeout(args.timeoutMs ?? DLP_TIMEOUT_MS),
    });
  } catch (err) {
    // Network error / timeout (AbortError) → fail-OPEN.
    console.warn(
      `[worker] DLP scan unreachable for ${resourceId} (fail-open, allowing): ${(err as Error).message}`,
    );
    return { decision: "allow", reason: "DLP unreachable (fail-open)" };
  }

  if (!resp.ok) {
    console.warn(
      `[worker] DLP scan returned HTTP ${resp.status} for ${resourceId} (fail-open, allowing)`,
    );
    return { decision: "allow", reason: `DLP HTTP ${resp.status} (fail-open)` };
  }

  let body: DlpScanResponse;
  try {
    body = (await resp.json()) as DlpScanResponse;
  } catch {
    console.warn(
      `[worker] DLP scan response unparseable for ${resourceId} (fail-open, allowing)`,
    );
    return { decision: "allow", reason: "DLP unparseable (fail-open)" };
  }

  const action = typeof body.action === "string" ? body.action : "";
  if (action === "ALLOW") {
    return { decision: "allow", reason: "DLP clean" };
  }

  // Any non-ALLOW action is a hit → deny.
  const sensitivity =
    typeof body.max_sensitivity === "string" && body.max_sensitivity
      ? body.max_sensitivity
      : "unknown";
  const reason =
    `DLP blocked ${toolName} (action=${action || "?"}, max_sensitivity=${sensitivity})`;
  console.warn(`[worker] DLP denied tool call ${resourceId}: ${reason}`);
  return { decision: "deny", reason };
}
