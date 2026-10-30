/**
 * result.ts — terminal artifact writers: /workspace/result.json + events.jsonl.
 *
 * The scheduler reads result.json to learn the run outcome and events.jsonl to
 * replay what happened. Both writers are failure-open: a filesystem error logs
 * a warning and is swallowed so an artifact write never crashes a finished run.
 *
 * Field names are pinned to the worker wire contract (see
 * contracts/worker-wire-contract.md): status / summary / error /
 * usage{input_tokens,output_tokens,total_tokens} / plan_steps[{n,t,d,live}].
 */

import { appendFileSync, writeFileSync } from "node:fs";
import path from "node:path";

export type ResultStatus = "OK" | "FAILED" | "RUNNING" | "paused";

/** One planning step. n is 1-based; d = done; live = currently in progress. */
export interface PlanStep {
  n: number;
  t: string;
  d: boolean;
  live: boolean;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  /**
   * DeepSeek prefix-cache hit observability (additive, non-contract field).
   * Count of prompt tokens served from DeepSeek's automatic prefix cache,
   * summed across all model calls in the run. Sourced from DeepSeek's
   * `prompt_cache_hit_tokens`, carried through as Anthropic
   * `cache_read_input_tokens`. The existing three AgentScope-contract fields
   * (input/output/total) are unchanged; this is an extra observability field.
   */
  cache_read_input_tokens?: number;
}

export interface ResultPayload {
  status: ResultStatus;
  summary?: string;
  error?: string | null;
  usage: Usage;
  plan_steps: PlanStep[];
}

/**
 * The highest usage this process has written to each result.json. Usage only
 * grows during a run, and the platform bills from what the file says last, so
 * a later write must never lower it: the crash handler, for one, knows only
 * what earlier legs used, not what this leg's checkpoints already recorded.
 */
const writtenUsage = new Map<string, Usage>();

function neverBelowWritten(file: string, usage: Usage): Usage {
  const prior = writtenUsage.get(file);
  const merged: Usage = {
    input_tokens: Math.max(usage.input_tokens, prior?.input_tokens ?? 0),
    output_tokens: Math.max(usage.output_tokens, prior?.output_tokens ?? 0),
    total_tokens: Math.max(usage.total_tokens, prior?.total_tokens ?? 0),
    // DeepSeek prefix-cache hit observability — additive field, defaults to 0
    // when the upstream did not report a cache hit. Does not alter the three
    // AgentScope-contract fields above.
    cache_read_input_tokens: Math.max(
      usage.cache_read_input_tokens ?? 0,
      prior?.cache_read_input_tokens ?? 0,
    ),
  };
  writtenUsage.set(file, merged);
  return merged;
}

/**
 * Synchronously write /workspace/result.json. Failure-open: a write error is
 * logged and swallowed (the run outcome is already decided; a missing artifact
 * is preferable to crashing on the way out). `usage` never drops below what an
 * earlier write in this process recorded.
 */
export function writeResult(workspace: string, payload: ResultPayload): void {
  const file = path.join(workspace, "result.json");
  const body = {
    status: payload.status,
    summary: payload.summary ?? "",
    error: payload.error ?? null,
    usage: neverBelowWritten(file, payload.usage),
    plan_steps: payload.plan_steps,
  };
  try {
    writeFileSync(file, JSON.stringify(body, null, 2), "utf8");
  } catch (err) {
    console.warn(
      `[result] failed to write result.json: ${(err as Error).message}`,
    );
  }
}

/**
 * Append one JSON event line to events.jsonl. Failure-open: a write error is
 * logged and swallowed so a logging failure never aborts the agent loop.
 */
export function appendEvent(eventsPath: string, event: unknown): void {
  try {
    appendFileSync(eventsPath, JSON.stringify(event) + "\n", "utf8");
  } catch (err) {
    console.warn(
      `[result] failed to append event to ${eventsPath}: ${(err as Error).message}`,
    );
  }
}
