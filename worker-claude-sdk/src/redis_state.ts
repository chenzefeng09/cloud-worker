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
 *                                delta | end | tool_call | tool_result | thinking
 *
 * Failure-open: every method swallows Redis errors with a console.warn so a
 * Redis outage never fails the run. When REDIS_URL is unset the client is a
 * no-op (constructed with redisUrl === "").
 */

import { Redis } from "ioredis";

/** Keep state for 30 min after a terminal write so the scheduler can poll it. */
const STATE_TTL_SECONDS = 1800;

/** Approximate cap on the token stream (matches Python _TOKEN_STREAM_MAXLEN). */
const TOKEN_STREAM_MAXLEN = 10_000;

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

export class RedisStateClient {
  private readonly runId: string;
  private readonly stateKey: string;
  private readonly tokenStreamKey: string;
  private client: Redis | null = null;
  private step = 0;
  private tokenStreamExpireSet = false;

  /**
   * @param runId     stable run identifier (scheduler injects RUN_ID).
   * @param redisUrl  redis://[:password@]host:port/db. Empty/undefined => no-op.
   */
  constructor(runId: string, redisUrl: string | undefined) {
    this.runId = runId;
    this.stateKey = `${KEY_PREFIX}:${runId}:state`;
    this.tokenStreamKey = `${KEY_PREFIX}:${runId}:tokens`;

    if (!redisUrl) {
      // REDIS_URL unset — hot-state disabled; every method is a no-op.
      return;
    }
    if (!runId) {
      console.warn("[redis_state] RUN_ID unset; hot-state disabled");
      return;
    }

    try {
      this.client = new Redis(redisUrl, {
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
      this.client.on("error", (err: Error) => {
        console.warn(
          `[redis_state] redis connection error for run ${this.runId}: ${err.message}`,
        );
      });
    } catch (err) {
      console.warn(
        `[redis_state] redis unavailable for run ${this.runId}; hot-state disabled: ${(err as Error).message}`,
      );
      this.client = null;
    }
  }

  // ── checkpoint state ───────────────────────────────────────────────────────

  /** Return the existing state blob, or null when absent/error. */
  async readCheckpoint(): Promise<CheckpointState | null> {
    if (this.client === null) return null;
    try {
      const raw = await this.client.get(this.stateKey);
      if (raw === null) return null;
      return JSON.parse(raw) as CheckpointState;
    } catch (err) {
      console.warn(
        `[redis_state] read failed for run ${this.runId}: ${(err as Error).message}`,
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
    if (this.client === null) return;
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
    try {
      await this.client.set(
        this.stateKey,
        JSON.stringify(state),
        "EX",
        STATE_TTL_SECONDS,
      );
    } catch (err) {
      console.warn(
        `[redis_state] write failed at step ${this.step} for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  /** Mark status=COMPLETED, keeping the key alive for the TTL. */
  async markCompleted(): Promise<void> {
    await this.writeTerminal("COMPLETED");
  }

  /** Mark status=FAILED, keeping the key alive for the TTL. */
  async markFailed(): Promise<void> {
    await this.writeTerminal("FAILED");
  }

  // ── token stream ─────────────────────────────────────────────────────────

  /** Append a type=delta entry (one assistant text token chunk). */
  async pushToken(text: string): Promise<void> {
    await this.xadd({ type: "delta", text });
  }

  /** Append the terminal type=end entry; consumers stop when they see it. */
  async pushTokenEnd(): Promise<void> {
    await this.xadd({ type: "end", text: "" });
  }

  /** Append a type=tool_call entry. args is the complete JSON args string. */
  async pushToolCall(
    toolCallId: string,
    name: string,
    args: string,
  ): Promise<void> {
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
    if (this.client === null) return;
    try {
      const raw = await this.client.get(this.stateKey);
      let state: CheckpointState;
      if (raw) {
        state = JSON.parse(raw) as CheckpointState;
      } else {
        state = {
          run_id: this.runId,
          step: this.step,
          plan: "",
          cursor: "",
          last_event_seq: this.step,
          status,
          updated_step: this.step,
        };
      }
      state.status = status;
      await this.client.set(
        this.stateKey,
        JSON.stringify(state),
        "EX",
        STATE_TTL_SECONDS,
      );
    } catch (err) {
      console.warn(
        `[redis_state] terminal write failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  /**
   * Append one stream entry with approximate MAXLEN trimming, then set the TTL
   * on first successful write. Failure-open. Field values are flattened to a
   * string list for XADD (ioredis takes key/value pairs).
   */
  private async xadd(fields: Record<string, string>): Promise<void> {
    if (this.client === null) return;
    try {
      const pairs: string[] = [];
      for (const [k, v] of Object.entries(fields)) {
        pairs.push(k, v);
      }
      // XADD key MAXLEN ~ <n> * field value ...
      await this.client.xadd(
        this.tokenStreamKey,
        "MAXLEN",
        "~",
        TOKEN_STREAM_MAXLEN,
        "*",
        ...pairs,
      );
      if (!this.tokenStreamExpireSet) {
        await this.client.expire(this.tokenStreamKey, STATE_TTL_SECONDS);
        this.tokenStreamExpireSet = true;
      }
    } catch (err) {
      console.warn(
        `[redis_state] token push (${fields.type}) failed for run ${this.runId}: ${(err as Error).message}`,
      );
    }
  }

  /** Close the underlying connection (no-op when disabled). */
  async close(): Promise<void> {
    if (this.client === null) return;
    try {
      await this.client.quit();
    } catch {
      // best-effort
    }
  }
}

/** Truncate a string to at most maxBytes UTF-8 bytes, appending a marker. */
function truncateUtf8(s: string, maxBytes: number): string {
  const buf = Buffer.from(s, "utf8");
  if (buf.length <= maxBytes) return s;
  return buf.subarray(0, maxBytes).toString("utf8") + "…[truncated]";
}

/** Build a client from RUN_ID / REDIS_URL in the environment. */
export function fromEnv(): RedisStateClient {
  return new RedisStateClient(
    process.env.RUN_ID ?? "",
    process.env.REDIS_URL || undefined,
  );
}
