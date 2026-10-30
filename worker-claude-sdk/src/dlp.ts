/**
 * dlp.ts — Foundation-DLP client for the worker's PreToolUse hook.
 *
 * Every tool call is scanned before it runs:
 *   - POST `${DLP_URL}/core/scan` with `{content, resource_type: "tool_call",
 *     resource_id: "<tool>:<tool_use_id>"}`, where `content` is the
 *     JSON-serialized tool input.
 *   - Headers: `authorization: Bearer <FOUNDATION_API_TOKEN>`, `x-tenant-id`,
 *     plus `x-tool-broker-lease` in broker mode (the tool-broker /dlp façade
 *     checks the lease and re-injects tenant/user from it).
 *   - DLP answers `{action, max_sensitivity, hits}`. The worker is not a
 *     SERVICE caller, so DLP only returns a verdict and never writes a
 *     classification label for this scan.
 *   - 2-second timeout (same as dlp_guard's `_DLP_TIMEOUT = 2.0`).
 *
 * Decision table (the session-orchestrator's `dlp_guard.py` shares the failure
 * policy; only tool calls are blocked on a hit):
 *
 *   DLP_URL unset ............................ allow  (DLP disabled for the
 *                                                     deployment; logged once
 *                                                     at startup)
 *   2xx and action === "ALLOW" ............... allow  (no hit)
 *   2xx and action === "SCAN_REQUIRED" ....... allow  (an L2/L3 hit, e.g. an
 *                                                     email address or phone
 *                                                     number; logged as a
 *                                                     warning with its rules)
 *   2xx and action === "QUARANTINE" .......... deny   (an L4+ hit)
 *   scan cannot complete: network error,
 *   timeout, non-2xx, unparseable body, a
 *   body without a string `action` or with
 *   one the contract does not define, or a
 *   tool input that cannot be serialized:
 *     DLP_FAILURE_MODE=closed (default) ...... deny   (the reason says the scan
 *                                                     could not complete)
 *     DLP_FAILURE_MODE=open .................. allow  (console.error for every
 *                                                     such call: never silent)
 *
 * An unrecognised DLP_FAILURE_MODE value falls back to "closed".
 *
 * Dependency-free: uses global fetch + AbortSignal, same as the rest of the
 * worker.
 */

/** DLP scan timeout — matches dlp_guard's `_DLP_TIMEOUT = 2.0` (seconds). */
const DLP_TIMEOUT_MS = 2000;

/** At most this many distinct rule names are quoted in a hit's deny reason. */
const MAX_RULE_NAMES_IN_REASON = 3;

/** What to do when a scan cannot complete. */
export type DlpFailureMode = "open" | "closed";

/** The deployment's DLP settings, resolved once from the worker env. */
export interface DlpPolicy {
  /** DLP base URL. Empty means DLP is disabled for this deployment. */
  url: string;
  failureMode: DlpFailureMode;
  /** The raw DLP_FAILURE_MODE when it was neither "open" nor "closed". */
  invalidFailureMode?: string;
}

/** Resolve DLP_URL / DLP_FAILURE_MODE into a policy (default: closed). */
export function resolveDlpPolicy(env: {
  DLP_URL: string;
  DLP_FAILURE_MODE: string;
}): DlpPolicy {
  const url = env.DLP_URL.trim();
  const raw = env.DLP_FAILURE_MODE.trim().toLowerCase();
  if (raw === "open") return { url, failureMode: "open" };
  if (raw === "" || raw === "closed") return { url, failureMode: "closed" };
  return { url, failureMode: "closed", invalidFailureMode: env.DLP_FAILURE_MODE };
}

/** Log the effective DLP policy. Call once, at worker startup. */
export function logDlpPolicy(policy: DlpPolicy): void {
  if (policy.invalidFailureMode !== undefined) {
    console.warn(
      `[worker] DLP_FAILURE_MODE=${JSON.stringify(policy.invalidFailureMode)} is neither "open" nor "closed"; using "closed"`,
    );
  }
  if (!policy.url) {
    console.log("[worker] DLP disabled: DLP_URL is not set, so tool calls are not scanned");
    return;
  }
  console.log(
    policy.failureMode === "open"
      ? "[worker] DLP enabled (DLP_FAILURE_MODE=open): a tool call whose scan cannot complete runs UNSCANNED, and each one is logged as an error"
      : "[worker] DLP enabled (DLP_FAILURE_MODE=closed): a tool call whose scan cannot complete is blocked",
  );
}

/** The verdict the PreToolUse hook turns into a permissionDecision. */
export interface DlpDecision {
  decision: "allow" | "deny";
  /** Shown to the model and the user when `decision === "deny"`. */
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
  /** DLP base URL. Empty → DLP disabled → allow without any fetch. */
  dlpUrl: string;
  /** Policy for a scan that cannot complete. Defaults to "closed". */
  failureMode?: DlpFailureMode;
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

/** Why a scan could not complete: `cause` is user/model-facing, `detail` is for logs. */
export interface ScanFailure {
  cause: string;
  detail: string;
}

/**
 * Apply the failure mode to a tool call whose scan could not complete. Closed
 * denies with a reason fit for the model and the user; open allows, but logs
 * the unscanned call as an error every time.
 */
export function decideUnscanned(
  failureMode: DlpFailureMode,
  toolName: string,
  resourceId: string,
  failure: ScanFailure,
): DlpDecision {
  if (failureMode === "open") {
    console.error(
      `[worker] DLP scan could not complete for ${resourceId} (${failure.detail}); ` +
        "ALLOWING the tool call UNSCANNED because DLP_FAILURE_MODE=open",
    );
    return { decision: "allow", reason: `DLP scan unavailable (${failure.cause}); allowed unscanned` };
  }
  console.error(
    `[worker] DLP scan could not complete for ${resourceId} (${failure.detail}); ` +
      "denying the tool call (DLP_FAILURE_MODE=closed)",
  );
  return {
    decision: "deny",
    reason:
      `Blocked: the data-loss-prevention (DLP) scan of this ${toolName || "tool"} call ` +
      `could not be completed (${failure.cause}), and tool calls are not run unscanned. ` +
      "This is a platform problem, not a problem with the request. " +
      "Try again later, and tell the user if it keeps failing.",
  };
}

/** Distinct rule names from DLP hits, for the deny reason. */
function ruleNames(hits: unknown): string[] {
  if (!Array.isArray(hits)) return [];
  const names: string[] = [];
  for (const hit of hits) {
    const name = (hit as { rule_name?: unknown } | null)?.rule_name;
    if (typeof name === "string" && name && !names.includes(name)) names.push(name);
  }
  return names;
}

/**
 * Short summary of a non-2xx response for the logs: the DLP problem `code`, or
 * the `detail` of a tool-broker rejection (lease missing/expired, path not
 * allowed), so operators can tell which hop refused the scan.
 */
async function describeHttpFailure(resp: Response): Promise<string> {
  let why = "";
  try {
    const body = (await resp.json()) as { code?: unknown; detail?: unknown } | null;
    const text = typeof body?.code === "string" ? body.code : body?.detail;
    if (typeof text === "string" && text) why = ` ${text.slice(0, 120)}`;
  } catch {
    // Not JSON: the status alone is enough.
  }
  return `HTTP ${resp.status}${why}`;
}

/**
 * Scan a tool call against Foundation-DLP and return an allow/deny decision,
 * following the decision table in the module header. Never throws.
 */
export async function scanToolCall(args: ScanToolCallArgs): Promise<DlpDecision> {
  const { dlpUrl, toolName, toolInput, toolUseId, headers } = args;
  const failureMode = args.failureMode ?? "closed";

  // DLP disabled for this deployment (logged once at startup, not per call).
  if (!dlpUrl) {
    return { decision: "allow", reason: "DLP disabled" };
  }

  const resourceId = `${toolName}:${toolUseId}`;

  // content = the serialized tool arguments; resource ties the scan to the call.
  let content: string;
  try {
    content = JSON.stringify(toolInput ?? null);
  } catch (err) {
    // Unserializable input cannot be scanned faithfully.
    return decideUnscanned(failureMode, toolName, resourceId, {
      cause: "the tool input could not be prepared for scanning",
      detail: `serialize: ${(err as Error).message}`,
    });
  }
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
    const name = (err as Error)?.name;
    const timedOut = name === "TimeoutError" || name === "AbortError";
    return decideUnscanned(failureMode, toolName, resourceId, {
      cause: timedOut
        ? "the DLP service did not answer in time"
        : "the DLP service could not be reached",
      detail: `${name ?? "Error"}: ${(err as Error)?.message ?? String(err)}`,
    });
  }

  if (!resp.ok) {
    return decideUnscanned(failureMode, toolName, resourceId, {
      cause: `the DLP service returned HTTP ${resp.status}`,
      detail: await describeHttpFailure(resp),
    });
  }

  let body: unknown;
  try {
    body = await resp.json();
  } catch {
    body = undefined;
  }
  const action =
    body !== null && typeof body === "object"
      ? (body as { action?: unknown }).action
      : undefined;
  if (typeof action !== "string" || !action) {
    return decideUnscanned(failureMode, toolName, resourceId, {
      cause: "the DLP service returned an unreadable response",
      detail: "2xx response without a string `action`",
    });
  }

  if (action === "ALLOW") {
    return { decision: "allow", reason: "DLP clean" };
  }

  const { max_sensitivity: maxSensitivity, hits } = body as {
    max_sensitivity?: unknown;
    hits?: unknown;
  };
  const sensitivity =
    typeof maxSensitivity === "string" && maxSensitivity ? maxSensitivity : "unknown";
  const names = ruleNames(hits).slice(0, MAX_RULE_NAMES_IN_REASON);

  if (action === "SCAN_REQUIRED") {
    // An L2/L3 hit (an email address, a phone number, …) does not block: only
    // QUARANTINE does. The warning keeps the flagged call traceable.
    console.warn(
      `[worker] DLP flagged tool call ${resourceId}: action=${action} max_sensitivity=${sensitivity}` +
        ` rules=${names.join(", ") || "-"}; allowed (only QUARANTINE blocks)`,
    );
    return { decision: "allow", reason: `DLP flagged ${sensitivity} data; allowed` };
  }

  if (action !== "QUARANTINE") {
    // Not a verdict the scan contract defines (ALLOW / SCAN_REQUIRED /
    // QUARANTINE), so the worker cannot tell what DLP meant.
    return decideUnscanned(failureMode, toolName, resourceId, {
      cause: "the DLP service returned a verdict the worker does not recognise",
      detail: `2xx response with unknown action ${JSON.stringify(action.slice(0, 40))}`,
    });
  }

  // QUARANTINE: an L4+ hit → deny.
  const matched = names.length
    ? `matches sensitive-data rules (${names.join(", ")})`
    : "contains data classified as sensitive";
  console.warn(
    `[worker] DLP denied tool call ${resourceId}: action=${action} max_sensitivity=${sensitivity}`,
  );
  return {
    decision: "deny",
    reason:
      `Blocked by data-loss-prevention (DLP) policy: the ${toolName || "tool"} input ${matched}; ` +
      `sensitivity ${sensitivity}, action ${action}. ` +
      "Remove or mask that data and try again, or ask the user how to proceed.",
  };
}
