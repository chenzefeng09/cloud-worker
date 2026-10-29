/**
 * hitl.ts — human-in-the-loop pause/resume brains for the Claude SDK worker.
 *
 * Parity target: the old AgentScope runtime_worker's HITL behavior
 *   - CONFIRM: tenant MCP tools (`mcp__*`) pause for human approval before they run.
 *   - QUESTION: a synthetic `ask_user` tool pauses to ask the human a question.
 *   - native sandbox tools (Read/Grep/Glob/Write/Edit/Bash) always run.
 *   - `hitl=false` (scheduled/headless runs) bypasses every gate.
 *
 * Approach A (see HITL_FEASIBILITY.md): the gate lives in the SDK's `canUseTool`
 * callback, which fires BEFORE each tool executes. On a tool that needs human
 * input we record the pending call into a shared {@link HitlContext}, flip
 * `ctx.paused`, and return `{behavior:'deny', interrupt:true}` — which ends the
 * `query()` run. The worker then persists a paused blob to Redis (carrying our
 * own replay transcript under `agent_state`), exits 0 with status="paused", and
 * the orchestrator re-dispatches a fresh sandbox with a `resume` payload.
 *
 * On resume we do NOT use the SDK's disk-bound session resume (the sandbox is
 * brand-new). Instead {@link buildReplayPrompt} reconstructs the conversation
 * from the saved transcript as an `AsyncIterable<SDKUserMessage>` fed to a fresh
 * `query()`, with a final decision turn (approved / rejected / answered).
 */

import { randomUUID } from "node:crypto";
import type {
  PermissionResult,
  SDKUserMessage,
} from "@anthropic-ai/claude-agent-sdk";

import type { ResumePayload, Task } from "./task.js";

/** Native sandbox tools the gate always allows (matches old __main__.py:829). */
const NATIVE_TOOL_NAMES = new Set([
  "Read",
  "Grep",
  "Glob",
  "Write",
  "Edit",
  "Bash",
]);

/** The synthetic tool name that pauses as a QUESTION, not a CONFIRM. */
export const ASK_USER_TOOL = "ask_user";
/** Fully-qualified name the SDK exposes for our in-process ask_user MCP tool. */
export const ASK_USER_TOOL_FQN = "mcp__hitl__ask_user";

/**
 * Gate decision for one tool name:
 *   - "allow"    → run it (native tools, or hitl disabled).
 *   - "confirm"  → pause for human approval (tenant MCP tools).
 *   - "question" → pause to ask the human (ask_user).
 */
export type GateDecision = "allow" | "confirm" | "question";

/**
 * Decide what the gate does with a tool call. Purely name-based, mirroring the
 * old worker (which built ALLOW rules for native tools and let everything else
 * fall through to the ASK path). `ask_user` is recognised both by its short name
 * and by the SDK's `mcp__hitl__ask_user` FQN.
 */
export function requiresConfirmation(
  toolName: string,
  task: Task,
): GateDecision {
  // hitl=false → bypass everything (scheduled / headless run).
  if (task.hitl === false) return "allow";

  if (toolName === ASK_USER_TOOL || toolName === ASK_USER_TOOL_FQN) {
    return "question";
  }
  if (NATIVE_TOOL_NAMES.has(toolName)) return "allow";
  // Tenant MCP tools are `mcp__{server}__{tool}` → confirm-pause.
  if (toolName.startsWith("mcp__")) return "confirm";
  // Any other (unknown) tool: be conservative and allow, matching the old
  // worker's behavior for built-ins beyond the native six (the SDK only enables
  // a known toolset). This keeps non-MCP SDK built-ins running.
  return "allow";
}

/** One pending tool call captured at pause time. */
export interface PendingToolCall {
  id: string;
  name: string;
  /** JSON-stringified input args (matches the token-stream `args` field). */
  args: string;
}

/**
 * Mutable per-run gate state shared between the `canUseTool` callback and the
 * worker's post-loop pause handling.
 */
export interface HitlContext {
  /** Set true the moment a tool call triggers a pause. */
  paused: boolean;
  /** Which kind of pause occurred (set alongside `paused`). */
  kind: "confirm" | "question" | null;
  /** The pending tool call(s) captured at pause time (pause-on-first). */
  pending: PendingToolCall[];
  /** For a question pause: the human-facing question text. */
  question: string | null;
  /**
   * The tool call the human approved on a confirm-approve resume leg, as its
   * tool name + canonical input. On resume the model re-emits the call with a
   * fresh tool_use_id, so ids cannot match; `canUseTool` lets a call through
   * only when BOTH the name and the canonical input match an approval, and
   * consumes it (the approved call runs exactly once). Any other call —
   * including the same tool with different arguments — pauses again.
   */
  approvals: ApprovedToolCall[];
}

/** One human-approved tool call (see HitlContext.approvals). */
export interface ApprovedToolCall {
  name: string;
  /** stableStringify of the approved input (sorted keys). */
  canonicalInput: string;
}

/** Build a fresh gate context, seeding the approvals from a resume. */
export function newHitlContext(task: Task): HitlContext {
  const ctx: HitlContext = {
    paused: false,
    kind: null,
    pending: [],
    question: null,
    approvals: [],
  };
  seedApprovals(ctx, task);
  return ctx;
}

/**
 * On a confirm-approve resume leg, record the approved pending call (the one
 * whose id the resume names; the sole pending call when no id is given) so its
 * re-emission passes the gate once. Rejected confirms and answers seed nothing.
 */
function seedApprovals(ctx: HitlContext, task: Task): void {
  const resume = task.resume;
  if (!resume || resume.kind !== "confirm" || resume.confirmed !== true) return;
  const pending = parseReplayState(resume.agent_state)?.pending ?? [];
  const approved = resume.tool_call_id
    ? pending.find((p) => p.id === resume.tool_call_id)
    : pending.length === 1
      ? pending[0]
      : undefined;
  if (!approved?.name) return;
  ctx.approvals.push({
    name: approved.name,
    canonicalInput: canonicalArgs(approved.args),
  });
}

/**
 * Canonical form of a JSON args string: re-serialised with sorted keys so key
 * order / whitespace differences do not matter. Unparseable args compare raw.
 */
function canonicalArgs(args: string): string {
  try {
    return stableStringify(JSON.parse(args));
  } catch {
    return args;
  }
}

/**
 * JSON.stringify with object keys sorted at every depth (JSON semantics
 * otherwise: undefined members dropped, undefined array items → null), so two
 * structurally equal inputs always produce the same string. Never throws.
 */
export function stableStringify(value: unknown): string {
  try {
    return JSON.stringify(sortKeysDeep(value)) ?? "null";
  } catch {
    return safeJsonStringify(value);
  }
}

function sortKeysDeep(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeysDeep);
  if (value !== null && typeof value === "object") {
    const toJSON = (value as { toJSON?: () => unknown }).toJSON;
    if (typeof toJSON === "function") return sortKeysDeep(toJSON.call(value));
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortKeysDeep((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/**
 * Build the `canUseTool` callback. It is the SOLE pause trigger: on a tool that
 * needs human input it records the pending call, flips `ctx.paused`, and returns
 * deny+interrupt to end the run. A call matching an approval (same name AND
 * same canonical input) is allowed once; everything else goes through the gate.
 */
export function makeCanUseTool(
  ctx: HitlContext,
  task: Task,
): (
  toolName: string,
  input: Record<string, unknown>,
  options: { toolUseID: string },
) => Promise<PermissionResult> {
  return async function canUseTool(
    toolName: string,
    input: Record<string, unknown>,
    options: { toolUseID: string },
  ): Promise<PermissionResult> {
    const toolUseId = options.toolUseID;

    // Allow-once: the call the human approved runs exactly once — matched on
    // name + canonical input (the re-emitted call carries a fresh id).
    if (ctx.approvals.length > 0) {
      const canonical = stableStringify(input);
      const idx = ctx.approvals.findIndex(
        (a) => a.name === toolName && a.canonicalInput === canonical,
      );
      if (idx >= 0) {
        ctx.approvals.splice(idx, 1);
        return { behavior: "allow", updatedInput: input };
      }
    }

    const decision = requiresConfirmation(toolName, task);
    if (decision === "allow") {
      return { behavior: "allow", updatedInput: input };
    }

    // Pause-on-first: record the pending call and interrupt the whole run. Any
    // sibling MCP calls in the same assistant turn simply re-appear on resume.
    const argsStr = safeJsonStringify(input);
    ctx.paused = true;
    ctx.kind = decision;
    if (decision === "question") {
      ctx.question = extractQuestion(input);
      ctx.pending.push({ id: toolUseId, name: toolName, args: argsStr });
    } else {
      ctx.pending.push({ id: toolUseId, name: toolName, args: argsStr });
    }
    return {
      behavior: "deny",
      message: "paused for human approval",
      interrupt: true,
    };
  };
}

/** Pull the `question` field out of an ask_user tool input. */
function extractQuestion(input: Record<string, unknown>): string {
  const q = input?.question;
  if (typeof q === "string") return q;
  return safeJsonStringify(input);
}

// ── transcript snapshot + replay ─────────────────────────────────────────────

/**
 * One replay transcript entry. Entries are kept in REAL conversation order —
 * assistant text and tool calls interleaved exactly as they happened.
 */
export interface TranscriptTurn {
  role: "assistant" | "tool";
  /** assistant text (role==="assistant"). */
  text?: string;
  /** tool_use id (role==="tool"); informational. */
  id?: string;
  /** tool name (role==="tool"). */
  name?: string;
  /** tool args JSON string (role==="tool"). */
  args?: string;
  /**
   * tool result text (role==="tool"). Absent when the call produced no result
   * before the pause interrupted the run.
   */
  result?: string;
}

/**
 * The opaque object we store under the paused blob's `agent_state`. The
 * orchestrator round-trips it back to us on resume. On the resume leg
 * {@link buildReplayPrompt} reconstructs the conversation from it.
 *
 * Prior chat history is NOT stored here: it comes from the dispatch payload's
 * `history` (re-sent on every resume leg) and lands in the system prompt
 * exactly as on a fresh run.
 */
export interface ReplayState {
  /** Snapshot format: 2 = interleaved + cumulative across resume legs. */
  version?: number;
  /** The original user task text, so the replay can restate the intent. */
  original_task: string;
  /**
   * Assistant text + tool calls (with results) of every leg so far, in order;
   * a resolved earlier pause appears as a tool turn carrying its outcome.
   */
  transcript: TranscriptTurn[];
  /** The tool call(s) that triggered the pause (not yet executed). */
  pending: PendingToolCall[];
}

/** What the worker records during one leg, in stream order. */
export interface TranscriptAccumulator {
  turns: TranscriptTurn[];
  /** block_id of the assistant text turn currently being extended. */
  openTextBlockId: string | null;
  /** tool_use id -> its turn (the result is attached when it arrives). */
  toolTurns: Map<string, TranscriptTurn>;
}

/** A fresh accumulator for a run. */
export function newTranscriptAccumulator(): TranscriptAccumulator {
  return { turns: [], openTextBlockId: null, toolTurns: new Map() };
}

/** Append streamed assistant text; a new text block starts a new turn. */
export function recordTranscriptText(
  acc: TranscriptAccumulator,
  blockId: string,
  text: string,
): void {
  if (!text) return;
  const last = acc.turns[acc.turns.length - 1];
  if (last?.role === "assistant" && acc.openTextBlockId === blockId) {
    last.text = (last.text ?? "") + text;
    return;
  }
  acc.turns.push({ role: "assistant", text });
  acc.openTextBlockId = blockId;
}

/** Record a closed tool call at its position in the conversation (once). */
export function recordTranscriptToolCall(
  acc: TranscriptAccumulator,
  id: string,
  name: string,
  args: string,
): void {
  if (!id || acc.toolTurns.has(id)) return;
  const turn: TranscriptTurn = { role: "tool", id, name, args };
  acc.turns.push(turn);
  acc.toolTurns.set(id, turn);
  acc.openTextBlockId = null;
}

/** Attach a tool result to its (already recorded) call. */
export function recordTranscriptToolResult(
  acc: TranscriptAccumulator,
  id: string,
  result: string,
): void {
  const turn = acc.toolTurns.get(id);
  if (turn) turn.result = result;
}

// Size bounds for the snapshot. agent_state round-trips through the paused
// blob into the resume leg's task payload, which some runtimes pass as one
// environment variable (Linux caps a single env string at 128 KiB), so the
// transcript must stay well below that whatever the tools returned.
const MAX_TEXT_TURN_BYTES = 16 * 1024;
const MAX_TOOL_FIELD_BYTES = 8 * 1024;
const MAX_TRANSCRIPT_BYTES = 64 * 1024;
const TRUNCATION_MARKER = "…[truncated]";

/**
 * Assemble the replay state: the prior legs' transcript (on a resume leg) +
 * the outcome of the pause that leg resolved + this leg's turns, in order.
 * The pending tool call itself is excluded (it never ran; the decision turn
 * restates it). Stored verbatim into the paused blob's `agent_state`.
 */
export function snapshotTranscriptForPause(
  task: Task,
  acc: TranscriptAccumulator,
  pending: PendingToolCall[],
): ReplayState {
  const pendingIds = new Set(pending.map((p) => p.id));
  const resume = task.resume ?? null;
  const prior = resume ? parseReplayState(resume.agent_state) : null;
  const turns: TranscriptTurn[] = [];
  if (prior) {
    turns.push(...prior.transcript);
    const resolved = resolvedPauseTurn(resume, prior);
    if (resolved) turns.push(resolved);
  }
  for (const turn of acc.turns) {
    if (turn.role === "assistant") {
      if (turn.text?.trim()) turns.push({ ...turn });
    } else if (!(turn.id && pendingIds.has(turn.id))) {
      turns.push({ ...turn });
    }
  }
  return {
    version: 2,
    original_task: prior?.original_task || task.task,
    transcript: boundTranscript(turns),
    pending,
  };
}

/**
 * The resolved earlier pause as a transcript turn, so a second pause keeps it:
 * a rejected call and an answered question carry their outcome. An approved
 * call gets no turn — the model re-issues it on this leg, where it is recorded
 * with its real result.
 */
function resolvedPauseTurn(
  resume: ResumePayload | null,
  prior: ReplayState,
): TranscriptTurn | null {
  if (!resume) return null;
  const call =
    prior.pending.find((p) => p.id === resume.tool_call_id) ?? prior.pending[0];
  if (!call) return null;
  if (resume.kind === "answer" || resume.kind === "question") {
    return {
      role: "tool",
      id: call.id,
      name: call.name,
      args: call.args,
      result: `[the user answered] ${resume.answer ?? ""}`,
    };
  }
  if (resume.confirmed !== true) {
    return {
      role: "tool",
      id: call.id,
      name: call.name,
      args: call.args,
      result: "[not executed: the user rejected this tool call]",
    };
  }
  return null;
}

/** Cap each turn's fields, then drop the oldest turns past the total budget. */
function boundTranscript(turns: TranscriptTurn[]): TranscriptTurn[] {
  const bounded = turns.map((t) => {
    const out: TranscriptTurn = { ...t };
    if (out.text !== undefined) out.text = truncateUtf8(out.text, MAX_TEXT_TURN_BYTES);
    if (out.args !== undefined) out.args = truncateUtf8(out.args, MAX_TOOL_FIELD_BYTES);
    if (out.result !== undefined) {
      out.result = truncateUtf8(out.result, MAX_TOOL_FIELD_BYTES);
    }
    return out;
  });
  const size = (t: TranscriptTurn) => Buffer.byteLength(JSON.stringify(t), "utf8");
  let total = bounded.reduce((n, t) => n + size(t), 0);
  let dropped = 0;
  while (total > MAX_TRANSCRIPT_BYTES && dropped < bounded.length - 1) {
    total -= size(bounded[dropped]);
    dropped += 1;
  }
  if (dropped === 0) return bounded;
  return [
    {
      role: "assistant",
      text: `[${dropped} earlier step(s) omitted to keep the resume payload small]`,
    },
    ...bounded.slice(dropped),
  ];
}

/** Truncate to at most maxBytes UTF-8 bytes INCLUDING the marker (idempotent). */
function truncateUtf8(s: string, maxBytes: number): string {
  const buf = Buffer.from(s, "utf8");
  if (buf.length <= maxBytes) return s;
  const keep = maxBytes - Buffer.byteLength(TRUNCATION_MARKER, "utf8");
  // toString drops a trailing partial multi-byte sequence as U+FFFD; trim it.
  return buf.subarray(0, keep).toString("utf8").replace(/�+$/, "") + TRUNCATION_MARKER;
}

/**
 * Build the resume prompt as an `AsyncIterable<SDKUserMessage>`. The SDK accepts
 * only user messages as a streamed prompt, so we encode the prior conversation
 * as a single synthetic user turn (a textual transcript) followed by the
 * decision instruction — approved / rejected / answered. This restates enough
 * context for the model to continue coherently on a fresh sandbox.
 */
export async function* buildReplayPrompt(
  task: Task,
): AsyncIterable<SDKUserMessage> {
  const resume = task.resume;
  const state = parseReplayState(resume?.agent_state);
  const text = renderReplayText(task, resume, state);

  const msg: SDKUserMessage = {
    type: "user",
    parent_tool_use_id: null,
    isSynthetic: true,
    session_id: "",
    uuid: randomUUID(),
    message: {
      role: "user",
      content: text,
    },
  };
  yield msg;
}

/** Render the replay user-turn text: prior context + decision instruction. */
function renderReplayText(
  task: Task,
  resume: ResumePayload | null | undefined,
  state: ReplayState | null,
): string {
  const parts: string[] = [];

  const original = state?.original_task ?? task.task;
  if (original) {
    parts.push(`[Original task] ${original}`);
  }

  // Rendered in the order it happened: text and tool calls interleaved.
  const turns = state?.transcript ?? [];
  if (turns.length > 0) {
    const lines = ["[Conversation so far]"];
    for (const t of turns) {
      if (t.role === "assistant" && t.text) {
        lines.push(`Assistant: ${t.text}`);
      } else if (t.role === "tool") {
        const result =
          t.result ?? "[no result: the run was interrupted before this call completed]";
        lines.push(`Tool call ${t.name}(${t.args ?? ""}) -> ${result}`);
      }
    }
    parts.push(lines.join("\n"));
  }

  parts.push(renderDecision(resume, state));
  return parts.join("\n\n");
}

/**
 * Render the decision instruction the model must act on:
 *   - confirm + confirmed=true  → execute the approved tool now.
 *   - confirm + confirmed=false → the user rejected; continue without it.
 *   - answer/question           → deliver the human's answer and continue.
 */
function renderDecision(
  resume: ResumePayload | null | undefined,
  state: ReplayState | null,
): string {
  if (!resume) {
    return "[System] Please continue with the task.";
  }

  // The ask_user leg arrives as kind "answer" (orchestrator) or "question"
  // (paused-blob spelling). Accept both.
  if (resume.kind === "answer" || resume.kind === "question") {
    const answer = resume.answer ?? "";
    return (
      `[System] The user has answered the question you asked earlier via ask_user. The user's answer is: ${answer}\n` +
      `Continue the task based on this answer, and do not ask the same question again.`
    );
  }

  // confirm leg — the call the decision is about (the one the resume names).
  const pending = state?.pending ?? [];
  const call = pending.find((p) => p.id === resume.tool_call_id) ?? pending[0];
  const toolDesc = call ? `${call.name}(${call.args})` : "the previously requested tool call";

  if (resume.confirmed === true) {
    return (
      `[System] The user has approved the tool call you requested earlier: ${toolDesc}.\n` +
      `Re-issue that tool call now to execute it, then continue with the task.`
    );
  }
  // rejected (confirmed false/null) — inject denial guidance, do NOT execute.
  return (
    `[System] The user rejected the tool call you requested earlier: ${toolDesc}.\n` +
    `Do not try to call that tool again; continue the task another way or without it.`
  );
}

/** Parse the round-tripped `agent_state` back into ReplayState (failure-soft). */
export function parseReplayState(raw: unknown): ReplayState | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const transcript = Array.isArray(o.transcript)
    ? (o.transcript as unknown[]).filter(
        (t): t is TranscriptTurn => typeof t === "object" && t !== null,
      )
    : [];
  const pending = Array.isArray(o.pending)
    ? (o.pending as unknown[]).filter(
        (p): p is PendingToolCall => typeof p === "object" && p !== null,
      )
    : [];
  return {
    ...(typeof o.version === "number" ? { version: o.version } : {}),
    original_task: typeof o.original_task === "string" ? o.original_task : "",
    transcript,
    pending,
  };
}

/** JSON.stringify that never throws (cyclic / weird inputs degrade to "{}"). */
function safeJsonStringify(v: unknown): string {
  try {
    return JSON.stringify(v ?? {});
  } catch {
    return "{}";
  }
}
