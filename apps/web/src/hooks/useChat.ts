import { useCallback, useEffect, useRef, useState } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api, ApiError } from '../api/client';
import type { Message, MessageAttachment } from '../api/types';

/**
 * SSE event handler bound to a specific sessionId.
 *
 * Why we capture the sessionId at connection-time
 * ------------------------------------------------
 * Both the persistent stream effect and sendMessage capture the sessionId
 * when the connection/call is made.  If the user switches sessions while a
 * stream is still open, every subsequent event is routed to the original
 * session — never to whatever is currently active.
 *
 * Ordered streaming pipeline
 * --------------------------
 * SSE events arrive in chronological order from the Redis stream.  We build
 * an ordered block list in the store rather than collapsing everything into
 * two buckets (text + tools).
 *
 * Event → store method mapping:
 *   run_started  → setIsStreaming(true)   (event-driven; eliminates the race
 *                                          window when POST returns but no
 *                                          stream event has landed yet — the
 *                                          optimistic setIsStreaming(true) in
 *                                          sendMessage covers that gap instead)
 *   token        → appendStreamingToken   (appends to open text segment, or opens new one)
 *   tool         → upsertStreamingTool    (seals open text; creates/updates tool block)
 *   thinking     → appendStreamingThinking(seals open text; appends thinking block)
 *   message      → clearStreamingBlocks + addMessage  (replaces temp blocks with real DB rows)
 *   run_complete → setIsStreaming(false)  (event-driven)
 *   done         → onDone()  (stream cycling signal — NOT isStreaming, a `done`
 *                              only means the 5-min server window closed; a new
 *                              window will reconnect immediately, so isStreaming
 *                              must not flip here)
 *   error        → addMessage SYSTEM + onDone()
 */
function handleSSEEvent(
  sessionId: string,
  eventType: string,
  data: Record<string, unknown>,
  onDone: () => void,
): void {
  const store = useChatStore.getState();
  switch (eventType) {
    case 'run_started':
      // A new run started on this session — mark streaming live.
      store.setIsStreaming(true);
      break;

    case 'message_created':
      // Backend echo of the user message — may replace the optimistic entry.
      store.addMessage(sessionId, data as unknown as Message);
      break;

    case 'token': {
      const runId = data.run_id as string | undefined;
      const delta = data.delta as string | undefined;
      // Redis stream entry id ("<ms>-<seq>"); enables idempotent replay so a
      // reconnect/StrictMode-remount that re-streams from "0-0" can't double or
      // scramble the streamed text. Optional — older backends omit it.
      const offset = data.offset as string | undefined;
      if (runId && delta !== undefined) {
        store.appendStreamingToken(sessionId, runId, delta, offset);
      }
      break;
    }

    case 'tool': {
      const runId = data.run_id as string | undefined;
      const kind = data.kind as 'call' | 'result' | undefined;
      const toolCallId = data.tool_call_id as string | undefined;
      const toolName = data.name as string | undefined;
      const offset = data.offset as string | undefined;
      if (runId && kind && toolCallId && toolName) {
        store.upsertStreamingTool(sessionId, runId, {
          kind,
          tool_call_id: toolCallId,
          name: toolName,
          args: kind === 'call' ? (data.args as string | undefined) : undefined,
          result: kind === 'result' ? (data.result as string | undefined) : undefined,
          state: kind === 'result' ? (data.state as string | undefined) : undefined,
        }, offset);
      }
      break;
    }

    case 'thinking': {
      const runId = data.run_id as string | undefined;
      const text = data.text as string | undefined;
      const offset = data.offset as string | undefined;
      if (runId && text) {
        store.appendStreamingThinking(sessionId, runId, text, offset);
      }
      break;
    }

    case 'plan': {
      // Real-time plan steps from the worker's TaskCreate/TaskUpdate calls.
      // Shape: { run_id: string, plan_steps: PlanStep[] }
      const steps = data.plan_steps;
      if (Array.isArray(steps) && steps.length > 0) {
        store.setPlanSteps(sessionId, steps as import('../lib/designFixtures').PlanStep[]);
      }
      break;
    }

    case 'message': {
      // A complete message row from the server.  The orchestrator stores roles
      // lowercase ("assistant"/"user"/"system"/"tool"); compare case-insensitively.
      const msg = data as unknown as Message;
      const roleUpper = (msg.role || '').toUpperCase();
      if ((roleUpper === 'ASSISTANT' || roleUpper === 'TOOL' || roleUpper === 'THINKING') && msg.run_id) {
        // Swap the transient streaming block for the durable DB row — but only
        // the block of the SAME kind this row replaces.  Durable rows arrive
        // THINKING-first, then ASSISTANT text; a non-kind-aware clear would wipe
        // the live-streamed text when the THINKING row lands, and if the text's
        // own durable row is delayed (or the stream closes first) the run would
        // show a lone THOUGHT block with no answer.  addMessage deduplicates.
        const kindForRole =
          roleUpper === 'THINKING' ? 'thinking' : roleUpper === 'TOOL' ? 'tool' : 'text';
        store.clearStreamingBlocks(sessionId, msg.run_id, kindForRole);
        store.addMessage(sessionId, msg);
      } else {
        store.addMessage(sessionId, msg);
      }
      break;
    }

    case 'agent_message':
      // Legacy event name — treat same as `message`.
      store.addMessage(sessionId, data as unknown as Message);
      break;

    case 'run_complete':
      // Run finished — clear the streaming indicator.  The stream window may
      // stay open a while longer; isStreaming is about run activity, not
      // connection state.
      store.setIsStreaming(false);
      break;

    case 'done':
      // The server closes the SSE window after ~5 min and emits `done`.  This
      // is NOT a run-completion signal; a run_complete will have preceded it if
      // a run was active.  We call onDone so the reconnect loop can reopen the
      // connection.  We deliberately do NOT flip isStreaming here.
      onDone();
      break;

    case 'error':
      store.addMessage(sessionId, {
        id: `err_${Date.now()}`,
        session_id: sessionId,
        role: 'SYSTEM',
        content: `Error: ${(data as Record<string, unknown>).detail || 'Unknown error'}`,
        tool_calls: null,
        run_id: null,
        sequence: 0,
        created_at: new Date().toISOString(),
      });
      onDone();
      break;

    default:
      // Never silently drop unknown events — a previously-unhandled `message`
      // event name silently broke the UI; warn loudly so it's never missed.
      console.warn('[useChat] Unknown SSE event type:', eventType, data);
      break;
  }
}

/**
 * Parse a raw SSE byte stream coming off a ReadableStreamDefaultReader, calling
 * `onEvent(eventType, parsedData)` for each complete SSE event.
 *
 * Returns a Promise that resolves when the stream reaches EOF (reader.done).
 * The promise rejects if the AbortSignal fires or the reader throws.
 *
 * Why a shared helper?
 * --------------------
 * Both the persistent stream effect (openSessionStream) and the old sendMessage
 * used identical line-buffer logic.  Centralising it avoids a copy-paste drift
 * bug where one path mis-handles partial UTF-8 chunks differently from the other.
 */
async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (eventType: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    let eventType = '';
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith('data: ') && eventType) {
        try {
          const data = JSON.parse(line.slice(6));
          onEvent(eventType, data);
        } catch {
          // Non-JSON data line — skip silently (SSE spec allows non-data lines)
        }
        eventType = '';
      }
    }
  }
}

// ── Singleton persistent-stream manager ───────────────────────────────────────
//
// Why a module-level singleton instead of a per-hook effect
// ---------------------------------------------------------
// `useChat()` is consumed by more than one mounted component at a time (e.g.
// AgentPage calls it for `sendMessage` AND renders ChatWindow which calls it for
// the transcript).  If the SSE connect-loop lived in a hook-local effect, EACH
// consumer would open its own GET /sessions/{id}/stream.  Two live streams both
// replay the same run's Redis token entries from `0-0`, so every `token` /
// `thinking` event is applied twice → the assistant text renders doubled and
// chopped ("你好你好", interleaved THOUGHT blocks).  React 18 StrictMode's
// double-mount made this even easier to hit.
//
// The fix: exactly ONE connection per sessionId for the whole app, ref-counted
// across all subscribers.  The first subscriber opens it; the last to leave
// tears it down.  Switching the active session is handled by each subscriber
// re-subscribing to the new id (and releasing the old one).
interface StreamHandle {
  controller: AbortController;
  cancelled: boolean;
  refCount: number;
  /** Pending teardown timer (see the delayed-abort note in unsubscribe). */
  teardownTimer: ReturnType<typeof setTimeout> | null;
}

const activeStreams = new Map<string, StreamHandle>();

// How long to wait after the last subscriber leaves before tearing the
// connection down.  React 18 StrictMode (and ordinary remounts) unmount then
// immediately re-mount an effect, driving refCount 1→0→1 within the same tick.
// A synchronous abort would kill the live SSE connection and force a full
// reconnect+replay on every mount flicker; the small grace window lets the
// re-subscribe cancel the teardown so the connection survives untouched.  The
// offset-based idempotency is the real correctness guarantee — this is purely a
// churn-avoidance optimisation.
const STREAM_TEARDOWN_GRACE_MS = 50;

/**
 * Open (or join) the singleton SSE stream for `sid`; returns an unsubscribe fn.
 * Exported for tests — production code reaches it via the useChat effect.
 */
export function subscribeSessionStream(sid: string): () => void {
  let handle = activeStreams.get(sid);
  if (handle) {
    // A connection for this session already exists — just join it.  If a
    // teardown was scheduled (last subscriber just left), cancel it: the
    // connection is still alive and this new subscriber reclaims it.
    if (handle.teardownTimer !== null) {
      clearTimeout(handle.teardownTimer);
      handle.teardownTimer = null;
    }
    handle.refCount += 1;
  } else {
    const controller = new AbortController();
    handle = { controller, cancelled: false, refCount: 1, teardownTimer: null };
    activeStreams.set(sid, handle);
    runConnectLoop(sid, handle);
  }

  return () => {
    const h = activeStreams.get(sid);
    if (!h) return;
    h.refCount -= 1;
    if (h.refCount <= 0 && h.teardownTimer === null) {
      // Last subscriber left — schedule (don't immediately execute) teardown so
      // a StrictMode/remount re-subscribe in the same tick can keep the
      // connection alive (see STREAM_TEARDOWN_GRACE_MS).
      h.teardownTimer = setTimeout(() => {
        // Re-check under the timer: a subscriber may have rejoined.
        if (h.refCount <= 0) {
          h.cancelled = true;
          h.controller.abort();
          activeStreams.delete(sid);
        } else {
          h.teardownTimer = null;
        }
      }, STREAM_TEARDOWN_GRACE_MS);
    }
  };
}

/** The reconnecting fetch loop for one session's SSE stream. */
async function runConnectLoop(sid: string, handle: StreamHandle): Promise<void> {
  const { controller } = handle;
  let backoffMs = 1000;

  while (!handle.cancelled) {
    const connectStart = Date.now();
    try {
      const res = await fetch(`/api/bff/v1/sessions/${sid}/stream`, {
        credentials: 'include',
        signal: controller.signal,
      });

      if (!res.ok || !res.body) {
        console.warn(`[useChat] session stream ${res.status} for ${sid}; backing off ${backoffMs}ms`);
        await delay(backoffMs, controller.signal);
        backoffMs = Math.min(backoffMs * 2, 8000);
        continue;
      }

      backoffMs = 1000;

      const reader = res.body.getReader();
      let doneFired = false;

      await parseSSEStream(reader, (eventType, data) => {
        if (eventType === 'done') doneFired = true;
        handleSSEEvent(sid, eventType, data, () => {
          doneFired = true;
        });
      });

      if (!handle.cancelled) {
        if (doneFired) {
          await delay(1000, controller.signal);
        } else {
          console.warn(`[useChat] session stream closed unexpectedly for ${sid}; reconnecting in ${backoffMs}ms`);
          await delay(backoffMs, controller.signal);
          backoffMs = Math.min(backoffMs * 2, 8000);
        }
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') break;
      const elapsed = Date.now() - connectStart;
      if (elapsed < 200) {
        backoffMs = Math.min(backoffMs * 2, 8000);
      }
      if (!handle.cancelled) {
        console.warn(`[useChat] session stream error for ${sid}; reconnecting in ${backoffMs}ms`, e);
        try {
          await delay(backoffMs, controller.signal);
        } catch {
          break; // aborted during backoff delay
        }
      }
    }
  }
}

export function useChat() {
  const { activeSessionId, messages, setIsStreaming, isStreaming } = useChatStore();
  // abortRef is used by cancel() to abort an in-flight POST (rare — the POST is
  // now very short-lived, typically <500 ms, but we keep the ref so cancel()
  // can still call abortRef.current?.abort() for consistency).
  const abortRef = useRef<AbortController | null>(null);
  const loadedRef = useRef<Set<string>>(new Set());
  // Prefill text to inject into InputBar after a rewind (cleared once consumed).
  const [prefillText, setPrefillText] = useState<string>('');
  // Rewind confirmation state — drives the app-level confirm modal.
  const [pendingRewind, setPendingRewind] = useState<Message | null>(null);
  const [rewindBusy, setRewindBusy] = useState<boolean>(false);
  const [rewindError, setRewindError] = useState<string>('');

  // ── Hydrate transcript ──────────────────────────────────────────────────────
  //
  // Load existing message history from the server when the active session
  // changes (page refresh, deep-link, session switch).
  useEffect(() => {
    if (!activeSessionId) return;
    if (loadedRef.current.has(activeSessionId)) return;
    loadedRef.current.add(activeSessionId);
    const sid = activeSessionId;
    (async () => {
      try {
        const page = await api.messages.list(sid);
        const store = useChatStore.getState();
        // addMessage handles dedup; we add in order so sequence is preserved.
        for (const m of page.items) {
          store.addMessage(sid, m);
        }
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) {

          console.warn('load messages failed', err);
        }
      }
    })();
  }, [activeSessionId]);

  // ── Persistent session SSE stream ───────────────────────────────────────────
  //
  // One long-lived connection per active session, opened on mount / session
  // switch and kept alive via reconnect.  This decouples event reception from
  // the send path: all token/tool/message/run_complete events arrive here
  // regardless of which component triggered the run.
  //
  // Lifecycle:
  //   1. Open GET /sessions/{id}/stream with credentials + AbortSignal.
  //   2. Parse SSE via parseSSEStream → handleSSEEvent.
  //   3. On clean EOF (server `done` event followed by stream close):
  //      reconnect after 1 s backoff if the session is still active.
  //   4. On immediate connect failure (network down, 401, etc.):
  //      back off exponentially (1 s → 2 s → 4 s, capped at 8 s) to avoid
  //      storm-reconnect loops that would saturate the server.
  //   5. Cleanup (session change / unmount) aborts the in-flight fetch via
  //      streamAbortRef, which causes the reader to throw AbortError → loop exits.
  //
  // Double-stream prevention: the actual SSE connection lives in a module-level
  // ref-counted singleton (subscribeSessionStream).  Every mounted useChat()
  // consumer for the same session shares ONE connection, so token/thinking
  // events are never applied twice — which is what produced the doubled,
  // chopped streaming text.  React StrictMode's double-mount is handled by the
  // ref count too: the synchronous subscribe→unsubscribe→subscribe cycle leaves
  // the connection up with refCount 1.
  useEffect(() => {
    if (!activeSessionId) return;
    const unsubscribe = subscribeSessionStream(activeSessionId);
    return unsubscribe;
  }, [activeSessionId]);

  // ── sendMessage ─────────────────────────────────────────────────────────────
  //
  // Phase 1.3 contract: POST /chat → plain JSON {run_id, message_id, run}.
  // The SSE stream (opened by the effect above) delivers all subsequent events.
  //
  // What sendMessage now does:
  //   1. Guard against concurrent sends (isStreaming).
  //   2. Add optimistic USER message.
  //   3. POST /chat with the same body as before.
  //   4. On success: call setIsStreaming(true) optimistically so the UI shows
  //      the loading state immediately, before run_started arrives on the stream.
  //      The stream's run_complete (not this function) turns it off.
  //   5. On failure: add SYSTEM error message, return.
  //
  // The `res.body.getReader()` SSE loop that was here is GONE — the persistent
  // stream effect handles all events.
  const sendMessage = useCallback(
    async (text: string, attachmentIds?: string[], attachmentsMeta?: MessageAttachment[]) => {
      if (!activeSessionId || isStreaming) return;
      const sessionId = activeSessionId;

      // Optimistic USER message — shown immediately so the UI feels responsive.
      useChatStore.getState().addMessage(sessionId, {
        id: `temp_user_${Date.now()}`,
        session_id: sessionId,
        role: 'USER',
        content: text,
        tool_calls: null,
        run_id: null,
        sequence: 0,
        created_at: new Date().toISOString(),
        attachments: attachmentsMeta && attachmentsMeta.length > 0 ? attachmentsMeta : undefined,
      });

      try {
        abortRef.current = new AbortController();

        // Skills enabled for this session in the context rail; forwarded so the
        // worker loads exactly these (BFF → orchestrator → task.json["skills"]).
        const skills = useChatStore.getState().selectedSkills[sessionId] || [];
        // Per-session model + reasoning effort, set via the composer dropdowns.
        const model = useChatStore.getState().selectedModel[sessionId] || undefined;
        const effortSel = useChatStore.getState().selectedEffort[sessionId] || '';

        const res = await fetch(`/api/bff/v1/sessions/${sessionId}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: text,
            attachment_ids: attachmentIds || [],
            skills,
            ...(model ? { model } : {}),
            ...(effortSel ? { reasoning_effort: effortSel } : {}),
          }),
          credentials: 'include',
          signal: abortRef.current.signal,
        });

        if (!res.ok) {
          // Parse the JSON error body (backend now returns plain JSON on failure).
          let detail = `请求失败 (${res.status})`;
          try {
            const errBody = await res.json();
            if (errBody?.detail) detail = String(errBody.detail);
          } catch {
            // ignore parse error, keep default message
          }
          useChatStore.getState().addMessage(sessionId, {
            id: `err_${Date.now()}`,
            session_id: sessionId,
            role: 'SYSTEM',
            content: detail,
            tool_calls: null,
            run_id: null,
            sequence: 0,
            created_at: new Date().toISOString(),
          });
          return;
        }

        // Success: parse {run_id, message_id, run}.
        // We don't strictly need the payload right now — the persistent stream
        // will deliver run_started/token/run_complete.  But setting isStreaming
        // optimistically here closes the gap between POST return and the stream's
        // first run_started event (typically <100 ms on a healthy connection, but
        // avoids a brief flash of "not loading" in the UI).
        //
        // Reconciliation of the optimistic user message: addMessage already
        // deduplicates by content (temp_user_* → real USER row) when the stream
        // emits the message_created event, so no extra work needed here.
        await res.json(); // consume body (run_id / message_id / run — unused for now)
        setIsStreaming(true);
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {

          console.error('Chat error:', e);
        }
        // Note: we do NOT call setIsStreaming(false) here because the persistent
        // stream's run_complete is the authoritative signal.  If the POST aborted
        // before the server processed it, no run was started, so isStreaming was
        // never set to true and there's nothing to clear.
      }
    },
    [activeSessionId, isStreaming, setIsStreaming],
  );

  // ── cancel ──────────────────────────────────────────────────────────────────
  //
  // Abort the in-flight POST (if it's still running — rare, since POST is now
  // very short-lived) and ask the backend to cancel the active run.
  //
  // We do NOT abort the persistent stream connection here: the stream should
  // stay open so we can see the run_complete event when the backend run
  // terminates.  The stream's run_complete will flip isStreaming(false).
  //
  // We DO call setIsStreaming(false) immediately for responsive UI feedback,
  // accepting the tiny risk that a run_complete arrives a moment later and
  // redundantly calls it again (idempotent).
  const cancel = useCallback(() => {
    const sessionId = useChatStore.getState().activeSessionId;
    abortRef.current?.abort(); // abort POST if still in flight
    setIsStreaming(false);
    if (sessionId) {
      api.sessions.cancel(sessionId).catch((err) => {

        console.warn('backend cancel failed', err);
      });
    }
  }, [setIsStreaming]);

  // rewindTo only *requests* a rewind — it opens an app-level confirm modal by
  // recording the pending message.  The actual delete happens in confirmRewind
  // once the user confirms (no native window.confirm).
  const rewindTo = useCallback((message: Message) => {
    setRewindError('');
    setPendingRewind(message);
  }, []);

  const cancelRewind = useCallback(() => {
    setPendingRewind(null);
    setRewindError('');
  }, []);

  const confirmRewind = useCallback(async () => {
    if (!activeSessionId || !pendingRewind) return;
    const sessionId = activeSessionId;
    const { sequence, content } = pendingRewind;

    setRewindBusy(true);
    setRewindError('');
    try {
      await api.messages.deleteFrom(sessionId, sequence);
      useChatStore.getState().truncateFromSequence(sessionId, sequence);
      setPrefillText(content);
      setPendingRewind(null);
    } catch (err) {
      console.error('rewind failed', err);
      // Surface the failure inline; keep the modal open so the user notices.
      setRewindError(
        err instanceof ApiError ? `回退失败：${err.message}` : '回退失败，请重试。',
      );
    } finally {
      setRewindBusy(false);
    }
  }, [activeSessionId, pendingRewind]);

  return {
    sendMessage: sendMessage as (text: string, attachmentIds?: string[], attachmentsMeta?: MessageAttachment[]) => Promise<void>,
    cancel,
    isStreaming,
    messages: activeSessionId ? messages[activeSessionId] || [] : [],
    prefillText,
    clearPrefill: () => setPrefillText(''),
    rewindTo,
    pendingRewind,
    confirmRewind,
    cancelRewind,
    rewindBusy,
    rewindError,
  };
}

// ── Utilities ─────────────────────────────────────────────────────────────────

/**
 * Promise-based delay that resolves after `ms` milliseconds but rejects
 * immediately if the given AbortSignal fires.  Used by the reconnect loop to
 * back off without holding the event loop and to exit cleanly on cleanup.
 */
function delay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'));
      return;
    }
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

if (import.meta.hot) {
  import.meta.hot.dispose(() => {
    for (const [, handle] of activeStreams) {
      handle.cancelled = true;
      handle.controller.abort();
    }
    activeStreams.clear();
  });
}
