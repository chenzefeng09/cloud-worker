import { create } from 'zustand';
import type { Message, PendingInteraction, Session, ToolCallEntry, UserProfile } from '../api/types';
import type { PlanStep } from '../lib/designFixtures';

/** Payload for a single tool event from the SSE stream. */
export interface ToolCallUpdate {
  kind: 'call' | 'result';
  tool_call_id: string;
  name: string;
  /** Raw args string (JSON) for kind=call */
  args?: string;
  /** Result text for kind=result */
  result?: string;
  /** State string for kind=result, e.g. "success" */
  state?: string;
}

interface ChatState {
  sessions: Session[];
  /** Cursor for the next page of the sidebar recents list (null = no more pages). Shared across all useSessions() instances so the Sidebar's "显示更多" button reflects the page that WorkspaceLayout fetched. */
  sessionsCursor: string | null;
  /** True while a loadMore() page fetch is in flight. */
  sessionsLoadingMore: boolean;
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  /**
   * Real-time structured plan steps keyed by sessionId.
   * Populated from the SSE ``plan`` event while a run is in progress.
   * Shape matches the frontend ``PlanStep`` type: ``{n, t, d, live}``.
   */
  planSteps: Record<string, PlanStep[]>;
  /**
   * Highest Redis-stream offset applied per run, keyed sessionId → runId →
   * offset (e.g. "1718-0").  Drives idempotent streaming: a token/tool/thinking
   * event whose offset is ≤ the recorded value is a replay and is dropped.  An
   * internal bookkeeping map — never rendered, never persisted.
   */
  streamOffsets: Record<string, Record<string, string>>;
  /**
   * Runs whose streaming channel has been CLOSED because their durable DB
   * message rows have already arrived (sessionId → set of runIds).  Once a run
   * is here, any further appendStreamingToken/appendStreamingThinking/
   * upsertStreamingTool for it is DROPPED — a consumer-side idempotency backstop
   * against the server replaying the Redis token stream of an already-hydrated
   * run (which would rebuild a duplicate, side-by-side set of streaming blocks).
   * Reset by `run_started` (reopenRunStream) so a re-run / new leg streams
   * normally.  Internal bookkeeping — never rendered, never persisted.
   */
  closedStreamRuns: Record<string, Set<string>>;
  isStreaming: boolean;
  isLoggedIn: boolean;
  userProfile: UserProfile | null;
  /**
   * False until the very first `/auth/me` call has resolved on app load.
   */
  bootstrapped: boolean;
  /**
   * Per-session set of enabled skill names. Bridges the context rail (where the
   * user toggles skills) and `useChat.sendMessage` (which reads them at send
   * time and forwards them in the /chat body → task.json["skills"]).
   */
  selectedSkills: Record<string, string[]>;
  /** Per-session selected model id (gateway routing key). Read at send time. */
  selectedModel: Record<string, string>;
  /** Per-session reasoning effort ('' = off). Read at send time. */
  selectedEffort: Record<string, string>;

  // ── HITL — human-in-the-loop ─────────────────────────────────────────────
  //
  // Pending interactions (tool confirmations, agent questions) are held here
  // and rendered as inline cards at the bottom of the message list while a run
  // is paused.
  //
  // REFRESH-DURING-PAUSE GAP (TODO P2-followup):
  // ---------------------------------------------------
  // Pending interactions are populated from the live SSE stream when events
  // `pending_confirmation` / `pending_question` arrive.  A PAUSED run is NOT
  // selected by the orchestrator's stream selector (status PAUSED is excluded),
  // so after a page refresh while the agent is paused the orchestrator does NOT
  // replay the run's Redis stream — meaning the pending_confirmation events do
  // NOT re-arrive and the confirm/question cards are LOST on refresh.
  //
  // PROPER FIX REQUIRES a backend change: the orchestrator must expose a
  // GET /sessions/{sid}/runs/{run_id}/pending endpoint that returns the
  // current paused interactions from Redis, or the stream selector must
  // include PAUSED runs' pending entries in the initial replay.
  //
  // Until that endpoint exists, this v1 implementation renders pendings from
  // the live stream only. On refresh with a PAUSED run the user will see an
  // empty card area and must be told to check for a context update or
  // re-submit. A follow-up story is needed: "Expose pending HITL interactions
  // on paused-run hydration endpoint."
  //
  // Deduplication: interactions are keyed by toolCallId; a reconnect that
  // re-delivers pending_confirmation for the same toolCallId will not
  // duplicate the card because addPendingInteraction dedupes by toolCallId.
  pendingInteractions: Record<string, PendingInteraction[]>;
  /**
   * HITL interactions that have already been submitted locally or completed by
   * a tool result.  Keyed per session by `${runId}:${toolCallId}` so a replayed
   * pending_confirmation / pending_question from the beginning of Redis stream
   * cannot resurrect an already-consumed approval card.
   */
  acknowledgedPendingInteractions: Record<string, Set<string>>;

  setSessions: (sessions: Session[]) => void;
  setSessionsCursor: (cursor: string | null) => void;
  setSessionsLoadingMore: (loading: boolean) => void;
  addSession: (session: Session) => void;
  setActiveSession: (sessionId: string | null) => void;
  setMessages: (sessionId: string, messages: Message[]) => void;
  /**
   * Add a message, deduplicating by id.
   *
   * Special case — optimistic user message replacement:
   * If `message` has a non-temp id and role USER, and there is a pre-existing
   * `temp_user_*` message with the same content, the temp entry is replaced
   * in-place rather than appended.
   */
  addMessage: (sessionId: string, message: Message) => void;
  updateLastMessage: (sessionId: string, content: string) => void;

  // ── Ordered streaming pipeline (Option A) ─────────────────────────────────
  //
  // Each "block" in an assistant turn is a separate temporary Message whose id
  // encodes its kind and position.  Events are processed in arrival order:
  //
  //   token  → append to the "open" text segment for this run (create if none).
  //   tool   → seal the current text segment, create/update a tool block.
  //   thinking → create a side-channel thinking block without splitting text.
  //
  // When the real DB messages arrive (event: message) we call
  // clearStreamingBlocks to swap temp blocks for real rows.  Clearing is
  // kind-aware: a durable row only clears the streaming blocks of the SAME
  // kind it replaces (e.g. a THINKING row must not wipe the live-streamed
  // assistant text, which is replaced separately by its own ASSISTANT row).

  /**
   * Append a token delta to the currently-open text block for `runId`.
   * If the last block for this run is not an open text segment, a new one is
   * created (sealed segments have `streaming: false`).
   * Block id: `stream_<runId>_<seq>` where seq is the count of text segments.
   *
   * Idempotency: when `offset` (the Redis stream entry id, e.g. "1718-0") is
   * given, the delta is applied only if its offset is strictly greater than the
   * highest offset already applied for this run.  This makes replay (a fresh SSE
   * connection always replays from "0-0") and StrictMode reconnect harmless: a
   * re-delivered token is dropped instead of being mis-appended to the wrong
   * (later) text block.  Calls without an offset are always applied (legacy /
   * tests).
   */
  appendStreamingToken: (
    sessionId: string,
    runId: string,
    delta: string,
    offset?: string,
  ) => void;

  /**
   * Handle a tool event (kind="call" | "result").
   *
   * - call: seal the current open text block (if any), then create or update
   *   a tool placeholder `tool_<runId>_<tool_call_id>`.
   * - result: find the existing tool block by tool_call_id and merge
   *   result/state into it.
   */
  upsertStreamingTool: (
    sessionId: string,
    runId: string,
    update: ToolCallUpdate,
    offset?: string,
  ) => void;

  /**
   * Handle a thinking event. Thinking is a side-channel, not a hard boundary in
   * assistant text: when an open text block already exists for the run, insert
   * the thinking block before it and keep the text block open so later tokens
   * continue the same visible answer.
   *
   * Idempotency: see `appendStreamingToken` — when `offset` is given, a thinking
   * event already applied (offset ≤ highest seen for the run) is dropped so
   * replay does not duplicate the thinking block.
   */
  appendStreamingThinking: (
    sessionId: string,
    runId: string,
    text: string,
    offset?: string,
  ) => void;

  /**
   * Remove temporary blocks whose `streamRunId` matches `runId`.  Called when
   * real DB messages arrive.  When `kind` is given, only streaming blocks of
   * that `blockKind` are removed (so a THINKING row leaves the streamed text
   * intact); when omitted, all streaming blocks for the run are removed.
   */
  clearStreamingBlocks: (
    sessionId: string,
    runId: string,
    kind?: 'text' | 'tool' | 'thinking',
  ) => void;

  /**
   * Reset the stream-offset watermark for `runId` so that a replay from offset
   * "0-0" (which the server always re-streams on reconnect) passes the
   * idempotency guard and tokens are applied again rather than dropped.
   *
   * Must be called alongside `clearStreamingBlocks` on every `run_started`
   * event to implement C3 replay idempotency: the first `run_started` sets up
   * a clean slate; a reconnect mid-run re-emits `run_started`, clears whatever
   * partial blocks were built, resets offsets, then the replayed token/tool/
   * thinking stream rebuilds the blocks from scratch — no duplicates, no gaps.
   */
  clearRunStreamOffsets: (sessionId: string, runId: string) => void;

  /**
   * Mark `runId`'s streaming channel as terminally CLOSED.  Called from the
   * `message` handler once a durable assistant/thinking/tool row for the run has
   * been hydrated: from then on the three streaming entry points drop further
   * token/thinking/tool events for this run, so a server (or other connection)
   * that keeps replaying the run's Redis stream can't rebuild a duplicate set of
   * streaming blocks alongside the durable rows.
   */
  closeRunStream: (sessionId: string, runId: string) => void;

  /**
   * Re-open `runId`'s streaming channel (clears the closed flag).  Called from
   * `run_started` so a resumed run or a fresh leg with the same id can stream
   * again normally.
   */
  reopenRunStream: (sessionId: string, runId: string) => void;

  // ── Legacy compat (kept for tests that import them directly) ──────────────

  /**
   * @deprecated Use appendStreamingToken — kept for backward compat.
   * This now seals previous text blocks and starts a fresh segment only when
   * the last block for this run is already a sealed text segment.  Under the
   * new ordered pipeline appendStreamingToken handles all the logic.
   */
  finalizeStreaming: (sessionId: string, runId: string, finalMessage: Message) => void;

  /**
   * @deprecated Use upsertStreamingTool — forwarded for backward compat.
   */
  upsertToolCall: (sessionId: string, runId: string, update: ToolCallUpdate) => void;

  /**
   * @deprecated Use clearStreamingBlocks — forwarded for backward compat.
   */
  removeToolingPlaceholder: (sessionId: string, runId: string) => void;

  /**
   * Remove all messages with sequence >= fromSequence from the local store.
   * Called after a successful DELETE /sessions/{id}/messages?from_sequence=...
   * to keep the UI in sync immediately.
   */
  truncateFromSequence: (sessionId: string, fromSequence: number) => void;

  setPlanSteps: (sessionId: string, steps: PlanStep[]) => void;
  setIsStreaming: (streaming: boolean) => void;

  // ── HITL mutators ─────────────────────────────────────────────────────────

  /**
   * Add (or ignore-if-duplicate) a pending interaction for a session.
   * Deduplication is by toolCallId, and acknowledged interactions are ignored
   * so replay after approve/answer cannot resurrect a consumed card.
   */
  addPendingInteraction: (sessionId: string, p: PendingInteraction) => void;

  /**
   * Mark a pending interaction as consumed/submitted.  Used before optimistic
   * removal and when a tool_result arrives, because both mean an older replayed
   * pending event must be ignored.
   */
  markPendingInteractionAcknowledged: (
    sessionId: string,
    runId: string,
    toolCallId: string,
  ) => void;

  /**
   * Undo the acknowledgement after a failed submit so the same card can be
   * re-added and retried.
   */
  unmarkPendingInteractionAcknowledged: (
    sessionId: string,
    runId: string,
    toolCallId: string,
  ) => void;

  /**
   * Remove a single pending interaction by its toolCallId (call after the user
   * submits a response — optimistic removal before the API call completes).
   */
  removePendingInteraction: (sessionId: string, toolCallId: string) => void;

  /**
   * Clear all pending interactions for a session, or only those belonging to
   * a specific runId when `runId` is given.
   *
   * Called on `run_started` (clear visible cards; replay may re-deliver any
   * unacknowledged pendings) and on `run_complete`.
   */
  clearPendingInteractions: (sessionId: string, runId?: string) => void;
  login: (profile: UserProfile) => void;
  logout: () => void;
  /** Replace the cached user profile (e.g. after a display-name rename) so
   *  every consumer — sidebar avatar, account menu — reflects it immediately. */
  setUserProfile: (profile: UserProfile) => void;
  setBootstrapped: (value: boolean) => void;
  /** Toggle a skill on/off for a session (used by the context rail). */
  toggleSkill: (sessionId: string, name: string) => void;
  /**
   * Seed a session's enabled skills from the user's install defaults — but
   * only if no selection exists yet for that session (never clobbers a choice
   * the user already made this session).
   */
  seedSkills: (sessionId: string, names: string[]) => void;
  /** Set the model for a session (composer dropdown). */
  setSessionModel: (sessionId: string, model: string) => void;
  /** Set the reasoning effort for a session ('' = off). */
  setSessionEffort: (sessionId: string, effort: string) => void;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Return all messages for a session, initialising to [] on first use. */
function getMessages(s: { messages: Record<string, Message[]> }, sessionId: string): Message[] {
  return s.messages[sessionId] || [];
}

/**
 * Compare two Redis-stream entry ids ("<ms>-<seq>").  Returns a negative number
 * when `a < b`, 0 when equal, positive when `a > b`.  Both components are
 * numeric, so lexical string comparison is WRONG ("10-0" < "9-0" lexically);
 * we parse and compare each part numerically.
 */
function compareOffsets(a: string, b: string): number {
  const [aMs, aSeq] = a.split('-');
  const [bMs, bSeq] = b.split('-');
  const ms = Number(aMs) - Number(bMs);
  if (ms !== 0) return ms;
  return Number(aSeq || 0) - Number(bSeq || 0);
}

/**
 * Decide whether a streaming event carrying `offset` for `runId` is a replay
 * that must be dropped.  Returns `true` when the event is a duplicate (offset ≤
 * the highest already applied for this run).  Calls without an offset are never
 * treated as replays (always applied) — preserves legacy/test behaviour.
 */
function isReplayOffset(
  s: { streamOffsets: Record<string, Record<string, string>> },
  sessionId: string,
  runId: string,
  offset: string | undefined,
): boolean {
  if (offset === undefined) return false;
  const last = s.streamOffsets[sessionId]?.[runId];
  return last !== undefined && compareOffsets(offset, last) <= 0;
}

/**
 * True when `runId`'s streaming channel has been closed (its durable rows have
 * already hydrated).  Used by the three streaming entry points to early-return
 * so a replayed token/tool/thinking event can't rebuild a duplicate block.
 */
function isRunStreamClosed(
  s: { closedStreamRuns: Record<string, Set<string>> },
  sessionId: string,
  runId: string,
): boolean {
  return s.closedStreamRuns[sessionId]?.has(runId) === true;
}

function pendingInteractionKey(runId: string, toolCallId: string): string {
  return `${runId}:${toolCallId}`;
}

function isPendingInteractionAcknowledged(
  s: { acknowledgedPendingInteractions: Record<string, Set<string>> },
  sessionId: string,
  runId: string,
  toolCallId: string,
): boolean {
  return s.acknowledgedPendingInteractions[sessionId]?.has(
    pendingInteractionKey(runId, toolCallId),
  ) === true;
}

/**
 * Return an updated `streamOffsets` map recording `offset` as the latest seen
 * for `runId` (only advances; never moves backwards).  When `offset` is
 * undefined the map is returned unchanged.
 */
function recordOffset(
  prev: Record<string, Record<string, string>>,
  sessionId: string,
  runId: string,
  offset: string | undefined,
): Record<string, Record<string, string>> {
  if (offset === undefined) return prev;
  const forSession = prev[sessionId] || {};
  const last = forSession[runId];
  if (last !== undefined && compareOffsets(offset, last) <= 0) return prev;
  return {
    ...prev,
    [sessionId]: { ...forSession, [runId]: offset },
  };
}

/**
 * Seal the currently-open text block for a run, if there is one.
 * "Open" means: last block whose id starts with `stream_<runId>_` AND has
 * `streaming: true`.  Sealing sets `streaming: false` so that the next token
 * starts a new block.
 */
function sealOpenTextBlock(msgs: Message[], runId: string): Message[] {
  // Walk backwards to find the most-recent text block for this run.
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (m.streamRunId === runId && m.blockKind === 'text' && m.streaming) {
      const updated = [...msgs];
      updated[i] = { ...m, streaming: false };
      return updated;
    }
  }
  return msgs; // nothing to seal
}

/**
 * Count how many text blocks exist for a run (sealed or open).
 * Used to generate unique ids `stream_<runId>_0`, `stream_<runId>_1`, etc.
 */
function countTextBlocks(msgs: Message[], runId: string): number {
  return msgs.filter((m) => m.streamRunId === runId && m.blockKind === 'text').length;
}

// ── Store ─────────────────────────────────────────────────────────────────────

/**
 * Derived selector: true when the given session has at least one pending HITL
 * interaction. Used by ChatWindow to disable the composer while cards are
 * displayed (prevents forking the paused run with a new message).
 */
export function hasPendingInteraction(
  state: ChatState,
  sessionId: string | null,
): boolean {
  if (!sessionId) return false;
  const pending = state.pendingInteractions[sessionId];
  return Array.isArray(pending) && pending.length > 0;
}

export const useChatStore = create<ChatState>((set) => ({
  sessions: [],
  sessionsCursor: null,
  sessionsLoadingMore: false,
  activeSessionId: null,
  messages: {},
  planSteps: {},
  streamOffsets: {},
  closedStreamRuns: {},
  isStreaming: false,
  isLoggedIn: false,
  userProfile: null,
  bootstrapped: false,
  selectedSkills: {},
  selectedModel: {},
  selectedEffort: {},
  pendingInteractions: {},
  acknowledgedPendingInteractions: {},

  setSessions: (sessions) => set({ sessions }),
  setSessionsCursor: (cursor) => set({ sessionsCursor: cursor }),
  setSessionsLoadingMore: (loading) => set({ sessionsLoadingMore: loading }),
  addSession: (session) => set((s) => ({ sessions: [session, ...s.sessions] })),
  setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
  setMessages: (sessionId, messages) =>
    set((s) => ({ messages: { ...s.messages, [sessionId]: messages } })),

  addMessage: (sessionId, message) =>
    set((s) => {
      // Normalize persisted reasoning rows (role='thinking') into a thinking
      // block so the renderer (StepGroup → ThinkingBlock) shows them collapsed.
      // DB rows carry no blockKind; streaming thinking blocks already set it.
      if ((message.role || '').toUpperCase() === 'THINKING' && message.blockKind !== 'thinking') {
        message = { ...message, blockKind: 'thinking' };
      }

      const existing = getMessages(s, sessionId);

      // 1. Hard dedup by id.
      if (message.id && existing.some((m) => m.id === message.id)) return s;

      // 2. Optimistic USER message replacement.
      //    Compare content trimmed — the backend `.strip()`s user content, so
      //    a temp placeholder built from raw input may carry trailing/leading
      //    whitespace the persisted row does not.
      if (
        message.role === 'USER' &&
        message.id &&
        !message.id.startsWith('temp_user_')
      ) {
        const tempIdx = existing.findIndex(
          (m) =>
            m.id.startsWith('temp_user_') &&
            m.content.trim() === message.content.trim(),
        );
        if (tempIdx !== -1) {
          const updated = [...existing];
          updated[tempIdx] = message;
          return { messages: { ...s.messages, [sessionId]: updated } };
        }
      }

      // 3. Ordered insertion for real (persisted) messages.
      //    Streaming/optimistic placeholders carry sequence===0 and are anchored
      //    at the current tail (they're "in progress"); a real message with
      //    sequence>0 must land at its sequence-ordered position so that a USER
      //    row arriving after an assistant streaming block doesn't get appended
      //    below it. The array invariant is [real rows sorted by sequence...,
      //    sequence===0 placeholders...]; we insert before the first element
      //    that is either a real row with a greater sequence OR a placeholder
      //    (sequence===0) — so the real row slots into sequence order and ahead
      //    of any trailing streaming blocks. Real rows preserve strict sequence
      //    order, which keeps same-run messages contiguous for groupSteps.
      if (message.sequence > 0) {
        const insertIdx = existing.findIndex(
          (m) => m.sequence === 0 || m.sequence > message.sequence,
        );
        if (insertIdx !== -1) {
          const updated = [
            ...existing.slice(0, insertIdx),
            message,
            ...existing.slice(insertIdx),
          ];
          return { messages: { ...s.messages, [sessionId]: updated } };
        }
      }

      // 4. Fallback: placeholder/streaming block (sequence===0) or a real
      //    message that belongs at the tail → append.
      return {
        messages: { ...s.messages, [sessionId]: [...existing, message] },
      };
    }),

  updateLastMessage: (sessionId, content) =>
    set((s) => {
      const msgs = getMessages(s, sessionId);
      if (msgs.length === 0) return s;
      const updated = [...msgs];
      updated[updated.length - 1] = { ...updated[updated.length - 1], content };
      return { messages: { ...s.messages, [sessionId]: updated } };
    }),

  // ── Ordered streaming pipeline ────────────────────────────────────────────

  appendStreamingToken: (sessionId, runId, delta, offset) =>
    set((s) => {
      // Channel-closed guard: the run's durable rows already hydrated — drop any
      // (replayed) token so we don't rebuild a duplicate streaming block.
      if (isRunStreamClosed(s, sessionId, runId)) return s;
      // Idempotency guard: drop a token we've already applied for this run.
      // Without this, a reconnect/StrictMode-remount replays the run from
      // "0-0" and re-appends earlier deltas onto whatever text block is
      // currently open — scrambling and partly losing the streamed text.
      if (isReplayOffset(s, sessionId, runId, offset)) return s;

      const existing = getMessages(s, sessionId);
      const streamOffsets = recordOffset(s.streamOffsets, sessionId, runId, offset);

      // Look for the most-recent block for this run that is an open text block.
      for (let i = existing.length - 1; i >= 0; i--) {
        const m = existing[i];
        if (m.streamRunId === runId && m.blockKind === 'text' && m.streaming) {
          // Append delta to this block.
          const updated = [...existing];
          updated[i] = { ...m, content: m.content + delta };
          return { messages: { ...s.messages, [sessionId]: updated }, streamOffsets };
        }
        // Stop scanning once we pass all blocks for this run (hit an older run or user msg).
        if (m.streamRunId && m.streamRunId !== runId) break;
      }

      // No open text block — create a new one.
      const seq = countTextBlocks(existing, runId);
      const placeholder: Message = {
        id: `stream_${runId}_${seq}`,
        session_id: sessionId,
        role: 'ASSISTANT',
        content: delta,
        tool_calls: null,
        run_id: runId,
        sequence: 0,
        created_at: new Date().toISOString(),
        streaming: true,
        blockKind: 'text',
        streamRunId: runId,
      };
      return {
        messages: { ...s.messages, [sessionId]: [...existing, placeholder] },
        streamOffsets,
      };
    }),

  upsertStreamingTool: (sessionId, runId, update, offset) =>
    set((s) => {
      // Channel-closed guard: durable rows already hydrated — drop replayed tool
      // events so they can't rebuild a duplicate tool block.
      if (isRunStreamClosed(s, sessionId, runId)) return s;
      // Idempotency guard: a replayed tool_call/tool_result is dropped so a
      // reconnect doesn't re-seal text blocks or re-merge results.
      if (isReplayOffset(s, sessionId, runId, offset)) return s;
      const streamOffsets = recordOffset(s.streamOffsets, sessionId, runId, offset);

      let existing = getMessages(s, sessionId);

      if (update.kind === 'call') {
        // 1. Seal the current open text block.
        existing = sealOpenTextBlock(existing, runId);

        // 2. Parse args.
        let input: Record<string, unknown> = {};
        if (update.args) {
          try {
            const parsed = JSON.parse(update.args);
            input = typeof parsed === 'object' && parsed !== null ? parsed : { value: parsed };
          } catch {
            input = { value: update.args };
          }
        }

        const newEntry: ToolCallEntry = {
          id: update.tool_call_id,
          name: update.name,
          input,
          result: undefined,
          state: undefined,
        };

        // 3. Check if a tool block for this specific tool_call_id already exists.
        const toolBlockId = `tool_${runId}_${update.tool_call_id}`;
        if (!existing.some((m) => m.id === toolBlockId)) {
          const toolBlock: Message = {
            id: toolBlockId,
            session_id: sessionId,
            role: 'TOOL',
            content: '',
            tool_calls: [newEntry],
            run_id: runId,
            sequence: 0,
            created_at: new Date().toISOString(),
            streaming: true,
            blockKind: 'tool',
            streamRunId: runId,
          };
          return {
            messages: { ...s.messages, [sessionId]: [...existing, toolBlock] },
            streamOffsets,
          };
        }
        // Already exists (shouldn't happen for kind=call, but safe fallback).
        return { messages: { ...s.messages, [sessionId]: existing }, streamOffsets };
      }

      // kind === 'result': merge into existing tool block.
      const toolBlockId = `tool_${runId}_${update.tool_call_id}`;
      const idx = existing.findIndex((m) => m.id === toolBlockId);
      if (idx === -1) return { ...s, streamOffsets };

      const updated = [...existing];
      const prev = updated[idx];
      const calls = (prev.tool_calls ?? []).map((tc) =>
        tc.id === update.tool_call_id
          ? { ...tc, result: update.result, state: update.state }
          : tc,
      );
      updated[idx] = { ...prev, tool_calls: calls, streaming: false };
      return { messages: { ...s.messages, [sessionId]: updated }, streamOffsets };
    }),

  appendStreamingThinking: (sessionId, runId, text, offset) =>
    set((s) => {
      // Channel-closed guard: durable rows already hydrated — drop replayed
      // thinking events so they can't rebuild a duplicate thinking block.
      if (isRunStreamClosed(s, sessionId, runId)) return s;
      // Idempotency guard: a replayed thinking event would duplicate the
      // thinking block and can reshuffle the visible run layout. Drop it.
      if (isReplayOffset(s, sessionId, runId, offset)) return s;
      const streamOffsets = recordOffset(s.streamOffsets, sessionId, runId, offset);

      const existing = getMessages(s, sessionId);

      const seq = existing.filter(
        (m) => m.streamRunId === runId && m.blockKind === 'thinking',
      ).length;

      const thinkingBlock: Message = {
        id: `thinking_${runId}_${seq}`,
        session_id: sessionId,
        role: 'ASSISTANT',
        content: text,
        tool_calls: null,
        run_id: runId,
        sequence: 0,
        created_at: new Date().toISOString(),
        streaming: false,
        blockKind: 'thinking',
        streamRunId: runId,
      };
      const openTextIdx = existing.findIndex(
        (m) => m.streamRunId === runId && m.blockKind === 'text' && m.streaming,
      );
      const nextMessages = openTextIdx === -1
        ? [...existing, thinkingBlock]
        : [
            ...existing.slice(0, openTextIdx),
            thinkingBlock,
            ...existing.slice(openTextIdx),
          ];
      return {
        messages: { ...s.messages, [sessionId]: nextMessages },
        streamOffsets,
      };
    }),

  clearStreamingBlocks: (sessionId, runId, kind) =>
    set((s) => {
      const existing = getMessages(s, sessionId);
      const filtered = existing.filter((m) => {
        if (m.streamRunId !== runId) return true; // other runs untouched
        if (kind && m.blockKind !== kind) return true; // keep other-kind blocks
        return false; // drop this run's matching streaming block
      });
      if (filtered.length === existing.length) return s;
      return { messages: { ...s.messages, [sessionId]: filtered } };
    }),

  clearRunStreamOffsets: (sessionId, runId) =>
    set((s) => {
      const forSession = s.streamOffsets[sessionId];
      if (!forSession || forSession[runId] === undefined) return s;
      const { [runId]: _dropped, ...rest } = forSession;
      return {
        streamOffsets: {
          ...s.streamOffsets,
          [sessionId]: rest,
        },
      };
    }),

  closeRunStream: (sessionId, runId) =>
    set((s) => {
      const forSession = s.closedStreamRuns[sessionId];
      if (forSession?.has(runId)) return s; // already closed
      const next = new Set(forSession ?? []);
      next.add(runId);
      return {
        closedStreamRuns: { ...s.closedStreamRuns, [sessionId]: next },
      };
    }),

  reopenRunStream: (sessionId, runId) =>
    set((s) => {
      const forSession = s.closedStreamRuns[sessionId];
      if (!forSession || !forSession.has(runId)) return s; // not closed
      const next = new Set(forSession);
      next.delete(runId);
      return {
        closedStreamRuns: { ...s.closedStreamRuns, [sessionId]: next },
      };
    }),

  // ── Legacy compat ─────────────────────────────────────────────────────────

  finalizeStreaming: (sessionId, runId, finalMessage) =>
    set((s) => {
      const existing = getMessages(s, sessionId);

      // Guard against duplicate real ids.
      if (finalMessage.id && existing.some((m) => m.id === finalMessage.id)) {
        // Remove any streaming temp blocks for this run and return.
        const filtered = existing.filter((m) => m.streamRunId !== runId);
        return { messages: { ...s.messages, [sessionId]: filtered } };
      }

      // Remove all temp blocks for this run, then append the final message.
      const filtered = existing.filter((m) => m.streamRunId !== runId);
      return {
        messages: {
          ...s.messages,
          [sessionId]: [...filtered, { ...finalMessage, streaming: false }],
        },
      };
    }),

  upsertToolCall: (sessionId, runId, update) => {
    // Forward to the new ordered implementation.
    useChatStore.getState().upsertStreamingTool(sessionId, runId, update);
  },

  removeToolingPlaceholder: (sessionId, runId) =>
    set((s) => {
      // Legacy: remove the old `tooling_<runId>` placeholder AND any new-style
      // tool blocks for this run that are still streaming (haven't been cleared
      // by clearStreamingBlocks yet).
      const existing = getMessages(s, sessionId);
      const toolingId = `tooling_${runId}`;
      const filtered = existing.filter(
        (m) => m.id !== toolingId,
      );
      if (filtered.length === existing.length) return s;
      return { messages: { ...s.messages, [sessionId]: filtered } };
    }),

  truncateFromSequence: (sessionId, fromSequence) =>
    set((s) => {
      const existing = getMessages(s, sessionId);
      const filtered = existing.filter((m) => m.sequence < fromSequence);
      if (filtered.length === existing.length) return s;
      return { messages: { ...s.messages, [sessionId]: filtered } };
    }),

  setPlanSteps: (sessionId, steps) =>
    set((s) => ({ planSteps: { ...s.planSteps, [sessionId]: steps } })),
  setIsStreaming: (isStreaming) => set({ isStreaming }),

  // ── HITL mutators ─────────────────────────────────────────────────────────

  addPendingInteraction: (sessionId, p) =>
    set((s) => {
      const current = s.pendingInteractions[sessionId] || [];
      if (isPendingInteractionAcknowledged(s, sessionId, p.runId, p.toolCallId)) {
        return s;
      }
      // Deduplicate by toolCallId — idempotent on replay.
      if (current.some((x) => x.toolCallId === p.toolCallId)) return s;
      return {
        pendingInteractions: {
          ...s.pendingInteractions,
          [sessionId]: [...current, p],
        },
      };
    }),

  markPendingInteractionAcknowledged: (sessionId, runId, toolCallId) =>
    set((s) => {
      const current = s.acknowledgedPendingInteractions[sessionId];
      const key = pendingInteractionKey(runId, toolCallId);
      if (current?.has(key)) return s;
      const next = new Set(current ?? []);
      next.add(key);
      return {
        acknowledgedPendingInteractions: {
          ...s.acknowledgedPendingInteractions,
          [sessionId]: next,
        },
      };
    }),

  unmarkPendingInteractionAcknowledged: (sessionId, runId, toolCallId) =>
    set((s) => {
      const current = s.acknowledgedPendingInteractions[sessionId];
      const key = pendingInteractionKey(runId, toolCallId);
      if (!current?.has(key)) return s;
      const next = new Set(current);
      next.delete(key);
      return {
        acknowledgedPendingInteractions: {
          ...s.acknowledgedPendingInteractions,
          [sessionId]: next,
        },
      };
    }),

  removePendingInteraction: (sessionId, toolCallId) =>
    set((s) => {
      const current = s.pendingInteractions[sessionId] || [];
      const filtered = current.filter((x) => x.toolCallId !== toolCallId);
      if (filtered.length === current.length) return s;
      return {
        pendingInteractions: {
          ...s.pendingInteractions,
          [sessionId]: filtered,
        },
      };
    }),

  clearPendingInteractions: (sessionId, runId) =>
    set((s) => {
      const current = s.pendingInteractions[sessionId] || [];
      const filtered = runId
        ? current.filter((x) => x.runId !== runId)
        : [];
      if (filtered.length === current.length) return s;
      return {
        pendingInteractions: {
          ...s.pendingInteractions,
          [sessionId]: filtered,
        },
      };
    }),
  login: (profile) => set({ isLoggedIn: true, userProfile: profile, bootstrapped: true }),
  logout: () =>
    set({
      isLoggedIn: false,
      userProfile: null,
      sessions: [],
      sessionsCursor: null,
      sessionsLoadingMore: false,
      messages: {},
      planSteps: {},
      streamOffsets: {},
      closedStreamRuns: {},
      activeSessionId: null,
      bootstrapped: true,
      selectedSkills: {},
      selectedModel: {},
      selectedEffort: {},
      pendingInteractions: {},
      acknowledgedPendingInteractions: {},
    }),
  setUserProfile: (profile) => set({ userProfile: profile }),
  setBootstrapped: (value) => set({ bootstrapped: value }),
  toggleSkill: (sessionId, name) =>
    set((s) => {
      const current = s.selectedSkills[sessionId] || [];
      const next = current.includes(name)
        ? current.filter((n) => n !== name)
        : [...current, name];
      return { selectedSkills: { ...s.selectedSkills, [sessionId]: next } };
    }),
  seedSkills: (sessionId, names) =>
    set((s) => {
      // Don't clobber an existing selection (user may have already toggled).
      if (s.selectedSkills[sessionId] !== undefined) return {};
      return { selectedSkills: { ...s.selectedSkills, [sessionId]: names } };
    }),
  setSessionModel: (sessionId, model) =>
    set((s) => ({ selectedModel: { ...s.selectedModel, [sessionId]: model } })),
  setSessionEffort: (sessionId, effort) =>
    set((s) => ({ selectedEffort: { ...s.selectedEffort, [sessionId]: effort } })),
}));
