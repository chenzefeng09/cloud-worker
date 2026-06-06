import { create } from 'zustand';
import type { Message, Session, ToolCallEntry, UserProfile } from '../api/types';
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
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  /**
   * Real-time structured plan steps keyed by sessionId.
   * Populated from the SSE ``plan`` event while a run is in progress.
   * Shape matches the frontend ``PlanStep`` type: ``{n, t, d, live}``.
   */
  planSteps: Record<string, PlanStep[]>;
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

  setSessions: (sessions: Session[]) => void;
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
  //   thinking → seal the current text segment, create a thinking block.
  //
  // When the real DB messages arrive (event: message) we call
  // clearStreamingBlocks to remove all temp blocks for the run, then addMessage
  // each real row.

  /**
   * Append a token delta to the currently-open text block for `runId`.
   * If the last block for this run is not an open text segment, a new one is
   * created (sealed segments have `streaming: false`).
   * Block id: `stream_<runId>_<seq>` where seq is the count of text segments.
   */
  appendStreamingToken: (sessionId: string, runId: string, delta: string) => void;

  /**
   * Handle a tool event (kind="call" | "result").
   *
   * - call: seal the current open text block (if any), then create or update
   *   a tool placeholder `tool_<runId>_<tool_call_id>`.
   * - result: find the existing tool block by tool_call_id and merge
   *   result/state into it.
   */
  upsertStreamingTool: (sessionId: string, runId: string, update: ToolCallUpdate) => void;

  /**
   * Handle a thinking event.  Seals the current open text block, then appends
   * a thinking block `thinking_<runId>_<seq>`.
   */
  appendStreamingThinking: (sessionId: string, runId: string, text: string) => void;

  /**
   * Remove all temporary blocks (id prefix `stream_`, `tool_`, `thinking_`)
   * whose `streamRunId` matches `runId`.  Called when real DB messages arrive.
   */
  clearStreamingBlocks: (sessionId: string, runId: string) => void;

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
  login: (profile: UserProfile) => void;
  logout: () => void;
  setBootstrapped: (value: boolean) => void;
  /** Toggle a skill on/off for a session (used by the context rail). */
  toggleSkill: (sessionId: string, name: string) => void;
  /**
   * Seed a session's enabled skills from the user's install defaults — but
   * only if no selection exists yet for that session (never clobbers a choice
   * the user already made this session).
   */
  seedSkills: (sessionId: string, names: string[]) => void;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Return all messages for a session, initialising to [] on first use. */
function getMessages(s: { messages: Record<string, Message[]> }, sessionId: string): Message[] {
  return s.messages[sessionId] || [];
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

export const useChatStore = create<ChatState>((set) => ({
  sessions: [],
  activeSessionId: null,
  messages: {},
  planSteps: {},
  isStreaming: false,
  isLoggedIn: false,
  userProfile: null,
  bootstrapped: false,
  selectedSkills: {},

  setSessions: (sessions) => set({ sessions }),
  addSession: (session) => set((s) => ({ sessions: [session, ...s.sessions] })),
  setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
  setMessages: (sessionId, messages) =>
    set((s) => ({ messages: { ...s.messages, [sessionId]: messages } })),

  addMessage: (sessionId, message) =>
    set((s) => {
      const existing = getMessages(s, sessionId);

      // 1. Hard dedup by id.
      if (message.id && existing.some((m) => m.id === message.id)) return s;

      // 2. Optimistic USER message replacement.
      if (
        message.role === 'USER' &&
        message.id &&
        !message.id.startsWith('temp_user_')
      ) {
        const tempIdx = existing.findIndex(
          (m) => m.id.startsWith('temp_user_') && m.content === message.content,
        );
        if (tempIdx !== -1) {
          const updated = [...existing];
          updated[tempIdx] = message;
          return { messages: { ...s.messages, [sessionId]: updated } };
        }
      }

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

  appendStreamingToken: (sessionId, runId, delta) =>
    set((s) => {
      const existing = getMessages(s, sessionId);

      // Look for the most-recent block for this run that is an open text block.
      for (let i = existing.length - 1; i >= 0; i--) {
        const m = existing[i];
        if (m.streamRunId === runId && m.blockKind === 'text' && m.streaming) {
          // Append delta to this block.
          const updated = [...existing];
          updated[i] = { ...m, content: m.content + delta };
          return { messages: { ...s.messages, [sessionId]: updated } };
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
      };
    }),

  upsertStreamingTool: (sessionId, runId, update) =>
    set((s) => {
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
          };
        }
        // Already exists (shouldn't happen for kind=call, but safe fallback).
        return { messages: { ...s.messages, [sessionId]: existing } };
      }

      // kind === 'result': merge into existing tool block.
      const toolBlockId = `tool_${runId}_${update.tool_call_id}`;
      const idx = existing.findIndex((m) => m.id === toolBlockId);
      if (idx === -1) return s;

      const updated = [...existing];
      const prev = updated[idx];
      const calls = (prev.tool_calls ?? []).map((tc) =>
        tc.id === update.tool_call_id
          ? { ...tc, result: update.result, state: update.state }
          : tc,
      );
      updated[idx] = { ...prev, tool_calls: calls, streaming: false };
      return { messages: { ...s.messages, [sessionId]: updated } };
    }),

  appendStreamingThinking: (sessionId, runId, text) =>
    set((s) => {
      let existing = getMessages(s, sessionId);

      // Seal any open text block.
      existing = sealOpenTextBlock(existing, runId);

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
      return {
        messages: { ...s.messages, [sessionId]: [...existing, thinkingBlock] },
      };
    }),

  clearStreamingBlocks: (sessionId, runId) =>
    set((s) => {
      const existing = getMessages(s, sessionId);
      const filtered = existing.filter((m) => m.streamRunId !== runId);
      if (filtered.length === existing.length) return s;
      return { messages: { ...s.messages, [sessionId]: filtered } };
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
  login: (profile) => set({ isLoggedIn: true, userProfile: profile, bootstrapped: true }),
  logout: () =>
    set({
      isLoggedIn: false,
      userProfile: null,
      sessions: [],
      messages: {},
      planSteps: {},
      activeSessionId: null,
      bootstrapped: true,
      selectedSkills: {},
    }),
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
}));
