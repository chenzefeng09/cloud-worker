/**
 * redis_state.ts — failure-open Redis checkpoint + token stream for one run.
 *
 * Field-for-field port of the Python runtime_worker redis_state.py so a Claude
 * SDK run and an AgentScope run write identical hot-state that the scheduler
 * and the orchestrator SSE bridge consume the same way.
 *
 * Key schema (must stay stable across deploys — the scheduler reads these):
 *   agent:run:{run_id}:state  -> JSON blob (string) with TTL STATE_TTL_SECONDS
 *   agent:run:{run_id}:tokens -> Redis Stream of interleaved
 *                                delta | thinking_delta | thinking | tool_call |
 *                                tool_result | pending_confirmation |
 *                                pending_question | error | end
 *
 * Token stream guarantees (what the orchestrator SSE bridge relies on):
 *   - every write refreshes the stream TTL (MULTI XADD+EXPIRE on ioredis, the
 *     `expire` field on every broker xadd), so the key never outlives its TTL
 *     and never expires while the run is still writing; a pause extends it to
 *     the paused TTL so the pending entries survive the human wait;
 *   - adjacent `delta` (and adjacent `thinking_delta`) chunks are coalesced for
 *     up to COALESCE_WINDOW_MS / COALESCE_MAX_CHARS; any other entry flushes
 *     the buffer first, so the relative order of entries is preserved;
 *   - at most one `tool_call` entry per tool_call_id;
 *   - exactly one terminal `end` entry `{type:"end", text:"", status}` per
 *     process (later pushes are dropped).
 *
 * Transport abstraction
 * ---------------------
 * The class is oblivious to whether it writes to foundation Redis directly
 * (ioredis) or through the tool-broker `/state/v1` façade (broker egress mode,
 * the production default). Both implement the same {@link StateTransport}
 * surface — get / set / xadd / close — so the public method bodies are
 * transport-agnostic and produce byte-identical Redis state either way.
 *
 * Failure-open: every method swallows transport errors with a console.warn so a
 * Redis/broker outage never fails the run. When neither REDIS_URL nor a broker
 * lease is present the client is a no-op (transport === null).
 */

import { Redis } from "ioredis";

/** Keep state for 30 min after a terminal write so the scheduler can poll it. */
const STATE_TTL_SECONDS = 1800;

/**
 * A PAUSED run waits on a human, so its state + paused-blob keys live far longer
 * than a terminal run's 30 min — up to 7 days (matches the Python
 * PAUSED_TTL_SECONDS). After this window the orchestrator treats the pause as
 * abandoned.
 */
const PAUSED_TTL_SECONDS = 7 * 24 * 3600;

/**
 * Approximate cap on the token stream. MAXLEN ~ trims the OLDEST entries, which
 * for a late subscriber replaying from 0 means losing the start of the answer,
 * so the cap is a safety valve sized well above a real run (deltas are
 * coalesced, so a long answer is a few thousand entries, not one per token).
 */
const TOKEN_STREAM_MAXLEN = 100_000;

/** Max time a delta / thinking_delta chunk waits in the coalescing buffer. */
const COALESCE_WINDOW_MS = 50;

/** Flush the coalescing buffer as soon as it holds this many characters. */
const COALESCE_MAX_CHARS = 512;

/** Terminal status carried by the `end` stream entry. */
export type TerminalStatus = "completed" | "failed" | "paused" | "cancelled";

/** Stream entry types whose adjacent chunks are merged into one entry. */
type CoalescibleType = "delta" | "thinking_delta";

/** Tunables for {@link RedisStateClient} (tests disable coalescing). */
export interface RedisStateOptions {
  /** Coalescing window in ms; 0 (or less) writes every chunk immediately. */
  coalesceWindowMs?: number;
  /** Flush the buffer once it holds at least this many characters. */
  coalesceMaxChars?: number;
}

/** Truncate oversized tool results (matches Python _TOOL_RESULT_MAX_BYTES). */
const TOOL_RESULT_MAX_BYTES = 8 * 1024;

const KEY_PREFIX = "agent:run";

/** The state blob shape — keys aligned with Python write_step(). */
export interface CheckpointState {
  run_id: string;
  step: number;
  plan: string;
  cursor: string;
  last_event_seq: number;
  status: string;
  updated_step: number;
}

/**
 * Minimal transport surface RedisStateClient drives. Mirrors the subset of the
 * redis client the Python class touches (get / set(ex=) / xadd(maxlen=) /
 * close) so the broker and direct-Redis paths are interchangeable.
 *
 * Every method is failure-open: an implementation must swallow its own errors
 * (logging a warning) and never throw out to the caller.
 */
export interface StateTransport {
  /** GET key. Returns the stored string or null (absent/error). */
  get(key: string): Promise<string | null>;
  /** SET key value EX ttlSec. */
  set(key: string, value: string, ttlSec: number): Promise<void>;
  /**
   * XADD the token stream with approximate MAXLEN trimming, folding the TTL in.
   * The token stream key is owned by the transport (reconstructed broker-side
   * from run_id), so callers pass only the fields, not a key.
   */
  xadd(
    fields: Record<string, string>,
    maxlen: number,
    ttlSec: number,
  ): Promise<void>;
  /**
   * Optional: re-set the token stream TTL without appending an entry. A
   * transport without it (broker: no standalone expire endpoint) relies on the
   * TTL folded into the next xadd instead.
   */
  expireTokenStream?(ttlSec: number): Promise<void>;
  /** Release the underlying connection (best-effort). */
  close(): Promise<void>;
}

/**
 * Direct ioredis transport — talks to foundation Redis over the wire: SET … EX,
 * and XADD … MAXLEN ~ N + EXPIRE in one MULTI on EVERY push, so the stream TTL
 * is refreshed by each write and the key never exists without a TTL.
 */
class IoredisTransport implements StateTransport {
  private readonly runId: string;
  private readonly tokenStreamKey: string;
  private readonly client: Redis;

  constructor(runId: string, client: Redis) {
    this.runId = runId;
    this.tokenStreamKey = `${KEY_PREFIX}:${runId}:tokens`;
    this.client = client;
  }

  async get(key: string): Promise<string | null> {
    try {
      return await this.client.get(key);
    } catch (err) {
      console.warn(
        `[redis_state] read failed for run ${this.runId}: ${(err as Error).message}`,
      );
      return null;
    }
  }

  async set(key: string, value: string, ttlSec: number): Promise<void> {
    try {
      await this.client.set(key, value, "EX", ttlSec);
    } catch (err) {
      console.warn(
        `[redis_state] write failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  async xadd(
    fields: Record<string, string>,
    maxlen: number,
    ttlSec: number,
  ): Promise<void> {
    try {
      const pairs: string[] = [];
      for (const [k, v] of Object.entries(fields)) {
        pairs.push(k, v);
      }
      // MULTI / XADD key MAXLEN ~ <n> * field value ... / EXPIRE key ttl / EXEC
      // — one round-trip, and the entry never lands without the TTL refresh.
      const results = await this.client
        .multi()
        .xadd(this.tokenStreamKey, "MAXLEN", "~", maxlen, "*", ...pairs)
        .expire(this.tokenStreamKey, ttlSec)
        .exec();
      if (results === null) {
        throw new Error("MULTI discarded");
      }
      for (const [err] of results) {
        if (err) throw err;
      }
    } catch (err) {
      console.warn(
        `[redis_state] token push (${fields.type}) failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  async expireTokenStream(ttlSec: number): Promise<void> {
    try {
      await this.client.expire(this.tokenStreamKey, ttlSec);
    } catch (err) {
      console.warn(
        `[redis_state] token stream expire failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  async close(): Promise<void> {
    try {
      await this.client.quit();
    } catch {
      // best-effort
    }
  }
}

/**
 * Broker-backed transport for *broker egress mode* (the production default).
 *
 * In broker mode the sandbox has no direct network route to foundation Redis;
 * the scheduler blanks REDIS_URL and injects TOOL_BROKER_URL + a per-run lease.
 * This transport translates get / set / xadd into HTTP calls on the tool-broker
 * `/state/v1` façade, producing byte-identical Redis state on the far side.
 *
 * Mirrors the Python `_BrokerTransport`:
 *   - kind ∈ {"state","paused"} is derived from the key suffix (set/get only).
 *   - xadd folds the TTL into every call via the `expire` field, so a separate
 *     expire() round-trip is unnecessary (the broker EXPIRE is idempotent).
 *   - the token stream key is reconstructed broker-side from run_id; the body
 *     carries only run_id + fields (no key).
 *   - failure-open: every method swallows HTTP errors after a console.warn.
 */
class BrokerTransport implements StateTransport {
  /** Canonical key suffixes mapped to the façade `kind` value (set/get). */
  private static readonly SET_KINDS = ["state", "paused"] as const;

  private readonly runId: string;
  private readonly base: string;
  private readonly headers: Record<string, string>;
  private readonly timeoutMs: number;

  constructor(opts: {
    runId: string;
    brokerUrl: string;
    tenantId: string;
    userId: string;
    token: string;
    lease: string;
    timeoutMs?: number;
  }) {
    this.runId = opts.runId;
    this.base = opts.brokerUrl.replace(/\/+$/, "");
    this.timeoutMs = opts.timeoutMs ?? 10_000;
    this.headers = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${opts.token}`,
      "X-Tenant-Id": opts.tenantId,
      "X-User-Id": opts.userId,
      "X-Tool-Broker-Lease": opts.lease,
    };
  }

  private kindForKey(key: string): string | null {
    for (const kind of BrokerTransport.SET_KINDS) {
      if (key.endsWith(`:${kind}`)) return kind;
    }
    return null;
  }

  async get(key: string): Promise<string | null> {
    const kind = this.kindForKey(key);
    if (kind === null) return null;
    try {
      const url = new URL(`${this.base}/state/v1/get`);
      url.searchParams.set("run_id", this.runId);
      url.searchParams.set("kind", kind);
      const resp = await fetch(url, {
        method: "GET",
        headers: this.headers,
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
      const body = (await resp.json()) as { value?: string | null };
      return body.value ?? null;
    } catch (err) {
      console.warn(
        `[redis_state] broker state get failed for run ${this.runId}: ${(err as Error).message}`,
      );
      return null;
    }
  }

  async set(key: string, value: string, ttlSec: number): Promise<void> {
    const kind = this.kindForKey(key);
    if (kind === null) return;
    try {
      const resp = await fetch(`${this.base}/state/v1/set`, {
        method: "POST",
        headers: this.headers,
        body: JSON.stringify({
          run_id: this.runId,
          kind,
          value,
          ttl: Math.trunc(ttlSec),
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (err) {
      console.warn(
        `[redis_state] broker state set failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  async xadd(
    fields: Record<string, string>,
    maxlen: number,
    ttlSec: number,
  ): Promise<void> {
    try {
      const stringFields: Record<string, string> = {};
      for (const [k, v] of Object.entries(fields)) {
        stringFields[String(k)] = String(v);
      }
      const resp = await fetch(`${this.base}/state/v1/xadd`, {
        method: "POST",
        headers: this.headers,
        body: JSON.stringify({
          run_id: this.runId,
          fields: stringFields,
          maxlen: Math.trunc(maxlen),
          // Fold the TTL into every xadd so the stream key always carries a TTL
          // in a single round-trip (the broker EXPIRE is idempotent and cheap).
          expire: Math.trunc(ttlSec),
        }),
        signal: AbortSignal.timeout(this.timeoutMs),
      });
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status}`);
      }
    } catch (err) {
      console.warn(
        `[redis_state] broker state xadd failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  async close(): Promise<void> {
    // No persistent connection to release (HTTP via global fetch).
  }
}

/**
 * The paused-blob shape written to `agent:run:{run_id}:paused` (and read by the
 * orchestrator runs.py:511-569 / 764). The orchestrator never inspects
 * `agent_state` beyond passing it through, so its inner shape is ours to define
 * (we store our replay transcript). `reply_id` is echoed back but not required to
 * hold a specific value.
 */
export interface PausedBlob {
  kind: "confirm" | "question";
  reply_id: string;
  tool_calls: Array<{ id: string; name: string; args: string }>;
  question: string | null;
  agent_state: unknown;
  paused_at: string;
}

export class RedisStateClient {
  private readonly runId: string;
  private readonly stateKey: string;
  private readonly pausedKey: string;
  private transport: StateTransport | null = null;
  private step = 0;

  /** TTL folded into every stream write; markPaused() raises it. */
  private streamTtlSec = STATE_TTL_SECONDS;
  private readonly coalesceWindowMs: number;
  private readonly coalesceMaxChars: number;
  /** Coalescing buffer: one entry type at a time. */
  private bufferedType: CoalescibleType | null = null;
  private bufferedText = "";
  private flushTimer: ReturnType<typeof setTimeout> | null = null;
  /** Serialises stream writes so coalesced and direct entries keep their order. */
  private writeChain: Promise<void> = Promise.resolve();
  /** Set once the terminal `end` entry is queued; later stream pushes drop. */
  private ended = false;
  private closed = false;
  private readonly pushedToolCallIds = new Set<string>();

  /**
   * @param runId      stable run identifier (scheduler injects RUN_ID).
   * @param redisUrl   redis://[:password@]host:port/db. Empty/undefined =>
   *                   direct Redis disabled; falls back to `transport` if given.
   * @param transport  optional pre-built transport (broker mode). When supplied
   *                   and `redisUrl` is empty, the client routes through it.
   * @param options    coalescing tunables (defaults: 50 ms / 512 chars).
   */
  constructor(
    runId: string,
    redisUrl: string | undefined,
    transport?: StateTransport | null,
    options?: RedisStateOptions,
  ) {
    this.runId = runId;
    this.stateKey = `${KEY_PREFIX}:${runId}:state`;
    this.pausedKey = `${KEY_PREFIX}:${runId}:paused`;
    this.coalesceWindowMs = options?.coalesceWindowMs ?? COALESCE_WINDOW_MS;
    this.coalesceMaxChars = options?.coalesceMaxChars ?? COALESCE_MAX_CHARS;

    if (!runId) {
      console.warn("[redis_state] RUN_ID unset; hot-state disabled");
      return;
    }

    if (!redisUrl) {
      // No direct Redis route. In broker egress mode a broker transport is
      // supplied by fromEnv(); otherwise every method stays a no-op.
      this.transport = transport ?? null;
      return;
    }

    try {
      const client = new Redis(redisUrl, {
        connectTimeout: 2000,
        commandTimeout: 2000,
        // Do not retry forever / spam reconnects — fail open quickly.
        maxRetriesPerRequest: 1,
        // Suppress ioredis's own unhandled 'error' event crashing the process;
        // we handle failures per-command and log.
        lazyConnect: false,
        enableOfflineQueue: true,
      });
      // ioredis emits 'error' asynchronously; without a listener Node would
      // treat it as an unhandled exception. Log and keep failing open.
      client.on("error", (err: Error) => {
        console.warn(
          `[redis_state] redis connection error for run ${this.runId}: ${err.message}`,
        );
      });
      this.transport = new IoredisTransport(runId, client);
    } catch (err) {
      console.warn(
        `[redis_state] redis unavailable for run ${this.runId}; hot-state disabled: ${(err as Error).message}`,
      );
      this.transport = null;
    }
  }

  // ── checkpoint state ───────────────────────────────────────────────────────

  /** Return the existing state blob, or null when absent/error. */
  async readCheckpoint(): Promise<CheckpointState | null> {
    if (this.transport === null) return null;
    const raw = await this.transport.get(this.stateKey);
    if (raw === null) return null;
    try {
      return JSON.parse(raw) as CheckpointState;
    } catch (err) {
      console.warn(
        `[redis_state] checkpoint parse failed for run ${this.runId}: ${(err as Error).message}`,
      );
      return null;
    }
  }

  /** Upsert the state blob with the latest step counter + metadata. */
  async writeStep(opts?: {
    plan?: string;
    cursor?: string;
    status?: string;
  }): Promise<void> {
    if (this.transport === null) return;
    this.step += 1;
    const state: CheckpointState = {
      run_id: this.runId,
      step: this.step,
      plan: opts?.plan ?? "",
      cursor: opts?.cursor ?? "",
      last_event_seq: this.step,
      status: opts?.status ?? "RUNNING",
      updated_step: this.step,
    };
    await this.transport.set(
      this.stateKey,
      JSON.stringify(state),
      STATE_TTL_SECONDS,
    );
  }

  /** Mark status=COMPLETED, keeping the key alive for the TTL. */
  async markCompleted(): Promise<void> {
    await this.writeTerminal("COMPLETED");
  }

  /** Mark status=FAILED, keeping the key alive for the TTL. */
  async markFailed(): Promise<void> {
    await this.writeTerminal("FAILED");
  }

  // ── HITL pause ─────────────────────────────────────────────────────────────

  /**
   * Write the paused blob to `agent:run:{run_id}:paused` with the 7-day TTL.
   * The orchestrator reads this to surface the pending confirmation/question and
   * to build the resume payload; `agent_state` round-trips back to us untouched.
   * Failure-open (the transport swallows its own errors).
   */
  async writePaused(blob: PausedBlob): Promise<void> {
    if (this.transport === null) return;
    await this.transport.set(
      this.pausedKey,
      JSON.stringify(blob),
      PAUSED_TTL_SECONDS,
    );
  }

  /**
   * Mark status=PAUSED, re-setting the state key with the long (7-day) TTL so a
   * paused run survives the human-confirmation window. Mirrors writeTerminal but
   * with the paused TTL. Also extends the TOKEN STREAM to the paused TTL (the
   * pending_* entries the orchestrator replays live there): every later stream
   * write carries the paused TTL, and ioredis re-EXPIREs the stream right away.
   * Failure-open.
   */
  async markPaused(): Promise<void> {
    if (this.transport === null) return;
    this.streamTtlSec = PAUSED_TTL_SECONDS;
    const transport = this.transport;
    if (transport.expireTokenStream) {
      // Queued behind in-flight stream writes so none of them can shrink the
      // TTL back afterwards (each write reads streamTtlSec when it runs).
      this.flushBuffer();
      await this.enqueueWrite(() =>
        transport.expireTokenStream!(PAUSED_TTL_SECONDS),
      );
    }
    let state: CheckpointState;
    const raw = await this.transport.get(this.stateKey);
    if (raw) {
      try {
        state = JSON.parse(raw) as CheckpointState;
      } catch {
        state = this.blankState("PAUSED");
      }
    } else {
      state = this.blankState("PAUSED");
    }
    state.status = "PAUSED";
    await this.transport.set(
      this.stateKey,
      JSON.stringify(state),
      PAUSED_TTL_SECONDS,
    );
  }

  /**
   * Append a type=pending_confirmation token entry — one per pending MCP tool
   * call awaiting approval. The worker follows the pending entries with
   * `end(status="paused")`; the resume leg (a new process) appends to the same
   * stream. Field shape matches the Python contract (redis_state.py example
   * `mcp__github__search_code`).
   */
  async pushPendingConfirmation(
    toolCallId: string,
    name: string,
    args: string,
  ): Promise<void> {
    await this.xadd({
      type: "pending_confirmation",
      tool_call_id: toolCallId,
      name,
      args,
    });
  }

  /** Append a type=pending_question token entry for an ask_user pause. */
  async pushPendingQuestion(
    toolCallId: string,
    question: string,
  ): Promise<void> {
    await this.xadd({
      type: "pending_question",
      tool_call_id: toolCallId,
      question,
    });
  }

  // ── token stream ─────────────────────────────────────────────────────────

  /**
   * Append a type=delta entry (assistant answer text). Adjacent chunks are
   * coalesced (see COALESCE_WINDOW_MS); the returned promise may resolve before
   * the chunk is written — {@link flush} / any non-delta push writes it.
   */
  async pushToken(text: string): Promise<void> {
    await this.pushCoalesced("delta", text);
  }

  /** Append a type=thinking_delta entry (live reasoning chunk), coalesced. */
  async pushThinkingDelta(text: string): Promise<void> {
    await this.pushCoalesced("thinking_delta", text);
  }

  /**
   * Append a type=error entry with a short, user-safe message (no stack
   * traces, paths, hostnames or keys — the caller sanitises). Pushed before the
   * terminal `end(status="failed")`.
   */
  async pushError(message: string): Promise<void> {
    await this.xadd({ type: "error", message });
  }

  /**
   * Append the terminal type=end entry; consumers stop when they see it. Only
   * the first call per process writes (exactly one `end`); every stream push
   * after it is dropped. Flushes the coalescing buffer first.
   */
  async pushTokenEnd(status: TerminalStatus = "completed"): Promise<void> {
    if (this.transport === null || this.ended || this.closed) return;
    this.flushBuffer();
    const written = this.enqueueStreamWrite({ type: "end", text: "", status });
    // Mark synchronously so a push racing this one cannot land after `end`.
    this.ended = true;
    await written;
  }

  /**
   * Append a type=tool_call entry. args is the complete JSON args string. At
   * most one entry per tool_call_id: a repeat for an already-pushed id is
   * dropped (the SDK can surface the same call twice).
   */
  async pushToolCall(
    toolCallId: string,
    name: string,
    args: string,
  ): Promise<void> {
    if (this.pushedToolCallIds.has(toolCallId)) return;
    this.pushedToolCallIds.add(toolCallId);
    await this.xadd({
      type: "tool_call",
      tool_call_id: toolCallId,
      name,
      args,
    });
  }

  /** Append a type=tool_result entry. Large results are truncated. */
  async pushToolResult(
    toolCallId: string,
    name: string,
    result: string,
    state: string,
  ): Promise<void> {
    const truncated = truncateUtf8(result, TOOL_RESULT_MAX_BYTES);
    await this.xadd({
      type: "tool_result",
      tool_call_id: toolCallId,
      name,
      result: truncated,
      state,
    });
  }

  /** Append a type=thinking entry. */
  async pushThinking(text: string): Promise<void> {
    await this.xadd({ type: "thinking", text });
  }

  // ── internals ──────────────────────────────────────────────────────────────

  private async writeTerminal(status: string): Promise<void> {
    if (this.transport === null) return;
    let state: CheckpointState;
    const raw = await this.transport.get(this.stateKey);
    if (raw) {
      try {
        state = JSON.parse(raw) as CheckpointState;
      } catch {
        state = this.blankState(status);
      }
    } else {
      state = this.blankState(status);
    }
    state.status = status;
    await this.transport.set(
      this.stateKey,
      JSON.stringify(state),
      STATE_TTL_SECONDS,
    );
  }

  private blankState(status: string): CheckpointState {
    return {
      run_id: this.runId,
      step: this.step,
      plan: "",
      cursor: "",
      last_event_seq: this.step,
      status,
      updated_step: this.step,
    };
  }

  /**
   * Append one non-coalescible stream entry with approximate MAXLEN trimming +
   * TTL refresh. Flushes the coalescing buffer first so entries keep their
   * order. Dropped after the terminal `end`. Failure-open (the transport
   * swallows its own errors).
   */
  private async xadd(fields: Record<string, string>): Promise<void> {
    if (this.transport === null || this.ended || this.closed) return;
    this.flushBuffer();
    await this.enqueueStreamWrite(fields);
  }

  private async pushCoalesced(
    type: CoalescibleType,
    text: string,
  ): Promise<void> {
    if (this.transport === null || this.ended || this.closed) return;
    if (!text) return;
    if (this.coalesceWindowMs <= 0) {
      await this.enqueueStreamWrite({ type, text });
      return;
    }
    if (this.bufferedType !== null && this.bufferedType !== type) {
      this.flushBuffer();
    }
    this.bufferedType = type;
    this.bufferedText += text;
    if (this.bufferedText.length >= this.coalesceMaxChars) {
      await this.flushBuffer();
      return;
    }
    if (this.flushTimer === null) {
      this.flushTimer = setTimeout(() => {
        this.flushTimer = null;
        void this.flushBuffer();
      }, this.coalesceWindowMs);
      // Never keep the process alive just to flush a chunk; close() flushes.
      this.flushTimer.unref?.();
    }
  }

  /** Queue the buffered chunk (if any) for writing; resolves once written. */
  private flushBuffer(): Promise<void> {
    if (this.flushTimer !== null) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (this.bufferedType === null) return this.writeChain;
    const fields = { type: this.bufferedType, text: this.bufferedText };
    this.bufferedType = null;
    this.bufferedText = "";
    return this.enqueueStreamWrite(fields);
  }

  private enqueueStreamWrite(fields: Record<string, string>): Promise<void> {
    const transport = this.transport;
    if (transport === null) return Promise.resolve();
    // streamTtlSec is read when the write RUNS, so a write queued before
    // markPaused() still carries the paused TTL.
    return this.enqueueWrite(() =>
      transport.xadd(fields, TOKEN_STREAM_MAXLEN, this.streamTtlSec),
    );
  }

  private enqueueWrite(op: () => Promise<void>): Promise<void> {
    const next = this.writeChain.then(op).catch((err: unknown) => {
      console.warn(
        `[redis_state] token stream write failed for run ${this.runId}: ${(err as Error).message}`,
      );
    });
    this.writeChain = next;
    return next;
  }

  /** Write any buffered delta chunk and wait for every queued stream write. */
  async flush(): Promise<void> {
    if (this.transport === null) return;
    await this.flushBuffer();
    await this.writeChain;
  }

  /** Flush buffered writes, then close the underlying connection. */
  async close(): Promise<void> {
    if (this.transport === null || this.closed) return;
    await this.flush();
    this.closed = true;
    await this.transport.close();
  }
}

/** Truncate a string to at most maxBytes UTF-8 bytes, appending a marker. */
function truncateUtf8(s: string, maxBytes: number): string {
  const buf = Buffer.from(s, "utf8");
  if (buf.length <= maxBytes) return s;
  return buf.subarray(0, maxBytes).toString("utf8") + "…[truncated]";
}

/**
 * Build a broker-backed transport when in broker egress mode, else null.
 *
 * Broker mode is detected by TOOL_BROKER_URL being set while the direct
 * REDIS_URL is blank (the scheduler blanks REDIS_URL and injects
 * TOOL_BROKER_URL + TOOL_BROKER_LEASE for broker-mode sandboxes). Returns null
 * (no-op state) if the URL or lease is missing — the run continues without
 * hot-state rather than crashing.
 */
function maybeBrokerTransport(runId: string): StateTransport | null {
  const brokerUrl = (process.env.TOOL_BROKER_URL ?? "").trim();
  const lease = (process.env.TOOL_BROKER_LEASE ?? "").trim();
  if (!brokerUrl || !lease) {
    return null;
  }
  return new BrokerTransport({
    runId,
    brokerUrl,
    tenantId: process.env.FOUNDATION_TENANT_ID ?? "",
    userId: process.env.FOUNDATION_USER_ID ?? "",
    token: process.env.FOUNDATION_API_TOKEN ?? "",
    lease,
  });
}

/**
 * Build a client from RUN_ID / REDIS_URL in the environment.
 *
 * When REDIS_URL is set, hot-state goes directly to foundation Redis. When it
 * is blank but TOOL_BROKER_URL + TOOL_BROKER_LEASE are present (broker egress
 * mode), hot-state routes through the tool-broker /state/v1 façade. Otherwise
 * the client is a no-op.
 */
export function fromEnv(): RedisStateClient {
  const runId = process.env.RUN_ID ?? "";
  const redisUrl = process.env.REDIS_URL || undefined;
  const transport = redisUrl ? null : maybeBrokerTransport(runId);
  return new RedisStateClient(runId, redisUrl, transport);
}

// Exported for tests.
export {
  BrokerTransport,
  IoredisTransport,
  maybeBrokerTransport,
  PAUSED_TTL_SECONDS,
};
