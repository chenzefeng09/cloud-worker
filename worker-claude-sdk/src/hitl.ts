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
   * Tool-use ids approved on a resume-confirm leg. The first time `canUseTool`
   * sees one of these it allows it once (then removes it), so the re-emitted
   * tool call runs exactly once. Matches the spec's "approved-tool registry".
   */
  approvedToolUseIds: Set<string>;
  /**
   * Tool NAMES approved on a resume-confirm leg (allow-once-by-name). On resume
   * the model re-emits the tool with a fresh tool_use_id, so id matching alone
   * never fires; we allow the first call whose name was approved.
   */
  approvedToolNames: Set<string>;
}

/** Build a fresh gate context, seeding the approved registry from a resume. */
export function newHitlContext(task: Task): HitlContext {
  const ctx: HitlContext = {
    paused: false,
    kind: null,
    pending: [],
    question: null,
    approvedToolUseIds: new Set(),
    approvedToolNames: new Set(),
  };
  seedApprovedRegistry(ctx, task);
  return ctx;
}

/**
 * On a confirm-approve resume leg, seed the approved registry so the re-emitted
 * tool call passes the gate once. Rejected confirms and answers seed nothing.
 */
function seedApprovedRegistry(ctx: HitlContext, task: Task): void {
  const resume = task.resume;
  if (!resume || resume.kind !== "confirm" || resume.confirmed !== true) return;
  if (resume.tool_call_id) ctx.approvedToolUseIds.add(resume.tool_call_id);
  // Recover the approved tool NAME from the saved transcript's pending call so
  // the re-emitted call (which carries a fresh id) is allowed by name.
  const state = parseReplayState(resume.agent_state);
  for (const p of state?.pending ?? []) {
    if (p.name) ctx.approvedToolNames.add(p.name);
  }
}

/**
 * Build the `canUseTool` callback. It is the SOLE pause trigger: on a tool that
 * needs human input it records the pending call, flips `ctx.paused`, and returns
 * deny+interrupt to end the run. Approved-registry hits allow once; everything
 * else allows.
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

    // Allow-once: a tool approved on the resume leg runs exactly once. Match by
    // id first (rare — ids change across legs), then by name.
    if (ctx.approvedToolUseIds.has(toolUseId)) {
      ctx.approvedToolUseIds.delete(toolUseId);
      return { behavior: "allow", updatedInput: input };
    }
    if (ctx.approvedToolNames.has(toolName)) {
      ctx.approvedToolNames.delete(toolName);
      return { behavior: "allow", updatedInput: input };
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

/** One captured assistant text segment, in stream order. */
export interface TranscriptTurn {
  role: "assistant" | "tool";
  /** assistant text (role==="assistant"). */
  text?: string;
  /** tool name (role==="tool"). */
  name?: string;
  /** tool args JSON string (role==="tool"). */
  args?: string;
  /** tool result text (role==="tool"). */
  result?: string;
}

/**
 * The opaque object we store under the paused blob's `agent_state`. The
 * orchestrator round-trips it back to us on resume. On the resume leg
 * {@link buildReplayPrompt} reconstructs the conversation from it.
 */
export interface ReplayState {
  /** The original user task text, so the replay can restate the intent. */
  original_task: string;
  /** Assistant text + completed tool_call/tool_result pairs observed so far. */
  transcript: TranscriptTurn[];
  /** The tool call(s) that triggered the pause (not yet executed). */
  pending: PendingToolCall[];
}

/** Inputs the worker accumulates during the run, used to build the snapshot. */
export interface TranscriptAccumulator {
  /** Assistant text streamed so far (consolidated). */
  assistantText: string;
  /** Completed tool calls keyed by tool_use_id, in first-seen order. */
  toolOrder: string[];
  toolName: Map<string, string>;
  toolArgs: Map<string, string>;
  toolResult: Map<string, string>;
}

/** A fresh accumulator for a run. */
export function newTranscriptAccumulator(): TranscriptAccumulator {
  return {
    assistantText: "",
    toolOrder: [],
    toolName: new Map(),
    toolArgs: new Map(),
    toolResult: new Map(),
  };
}

/**
 * Assemble the replay state from what we observed + the pending call(s). The
 * pending tool call itself is excluded from the completed-tool list (it never
 * ran). Stored verbatim into the paused blob's `agent_state`.
 */
export function snapshotTranscriptForPause(
  task: Task,
  acc: TranscriptAccumulator,
  pending: PendingToolCall[],
): ReplayState {
  const pendingIds = new Set(pending.map((p) => p.id));
  const transcript: TranscriptTurn[] = [];
  if (acc.assistantText.trim()) {
    transcript.push({ role: "assistant", text: acc.assistantText });
  }
  for (const id of acc.toolOrder) {
    if (pendingIds.has(id)) continue; // the pending call did not complete
    transcript.push({
      role: "tool",
      name: acc.toolName.get(id) ?? "tool",
      args: acc.toolArgs.get(id) ?? "",
      result: acc.toolResult.get(id) ?? "",
    });
  }
  return {
    original_task: task.task,
    transcript,
    pending,
  };
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
    parts.push(`[原始任务] ${original}`);
  }

  const turns = state?.transcript ?? [];
  if (turns.length > 0) {
    const lines = ["[之前的对话回顾]"];
    for (const t of turns) {
      if (t.role === "assistant" && t.text) {
        lines.push(`助手: ${t.text}`);
      } else if (t.role === "tool") {
        lines.push(`工具调用 ${t.name}(${t.args ?? ""}) -> ${t.result ?? ""}`);
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
    return "[系统] 请继续完成任务。";
  }

  // The ask_user leg arrives as kind "answer" (orchestrator) or "question"
  // (paused-blob spelling). Accept both.
  if (resume.kind === "answer" || resume.kind === "question") {
    const answer = resume.answer ?? "";
    return (
      `[系统] 用户已回答你之前通过 ask_user 提出的问题。用户的回答是：${answer}\n` +
      `请基于这个回答继续完成任务，不要再次提出相同的问题。`
    );
  }

  // confirm leg
  const pending = state?.pending ?? [];
  const first = pending[0];
  const toolDesc = first ? `${first.name}(${first.args})` : "之前请求的工具调用";

  if (resume.confirmed === true) {
    return (
      `[系统] 用户已批准你之前请求的工具调用 ${toolDesc}。\n` +
      `请立即重新发起该工具调用以执行它，然后继续完成任务。`
    );
  }
  // rejected (confirmed false/null) — inject denial guidance, do NOT execute.
  return (
    `[系统] 用户拒绝了你之前请求的工具调用 ${toolDesc}。\n` +
    `请不要再尝试调用该工具，改用其他方式或在没有它的情况下继续完成任务。`
  );
}

/** Parse the round-tripped `agent_state` back into ReplayState (failure-soft). */
export function parseReplayState(raw: unknown): ReplayState | null {
  if (typeof raw !== "object" || raw === null) return null;
  const o = raw as Record<string, unknown>;
  const transcript = Array.isArray(o.transcript)
    ? (o.transcript as TranscriptTurn[])
    : [];
  const pending = Array.isArray(o.pending)
    ? (o.pending as PendingToolCall[])
    : [];
  return {
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
