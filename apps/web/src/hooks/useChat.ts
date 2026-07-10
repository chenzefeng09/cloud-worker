import { useCallback, useEffect, useRef, useState } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api, ApiError } from '../api/client';
import type { Message, MessageAttachment, PendingInteractionItem } from '../api/types';

// ── SSE event handler registry ────────────────────────────────────────────────
//
// This map is THE platform SSE event contract and the SINGLE adaptation point.
//
// The backend worker may swap frameworks (AgentScope today, possibly Claude
// Agent SDK or another framework later) and the orchestrator normalises worker
// events into this contract before they reach the client.  If event names
// change, ONLY this map and its comment need updating — nothing else in the
// frontend changes.
//
// Event → store method mapping:
//   run_started    → clearStreamingBlocks (C3 replay idempotency) +
//                    clearRunStreamOffsets + setIsStreaming(true)
//   token          → appendStreamingToken (idempotent via offset watermark)
//   tool           → upsertStreamingTool  (idempotent via offset watermark)
//   thinking       → appendStreamingThinking (idempotent via offset watermark)
//   message        → clearStreamingBlocks (kind-aware) + addMessage
//   message_created→ addMessage  (backend echo of the user message)
//   agent_message  → addMessage  (legacy event name — treated same as message)
//   plan           → setPlanSteps
//   run_complete   → setIsStreaming(false) + one-shot messages.list reconcile
//   ping           → no-op (SSE keepalive heartbeat; see the silent-watchdog
//                    note on runConnectLoop — presence of pings is what arms it)
//   done           → onDone()  (5-min server window closed; NOT isStreaming)
//   error          → addMessage SYSTEM + onDone()
//   <unknown>      → console.warn (never silently dropped — a silent `message`
//                    drop has bitten this codebase before)
//
// Handler signature:
//   ctx  — { sessionId: string; onDone: () => void }
//   data — parsed JSON payload for the event

interface SSEContext {
  sessionId: string;
  onDone: () => void;
}

type SSEHandler = (ctx: SSEContext, data: Record<string, unknown>) => void;

const SSE_HANDLERS: Record<string, SSEHandler> = {
  run_started(ctx, data) {
    // C3 — replay idempotency:
    // On every run_started (including a reconnect mid-run), wipe the transient
    // streaming blocks for this run and reset its offset watermark so that the
    // server's token/tool/thinking replay from "0-0" applies cleanly rather
    // than being filtered by stale offsets or double-appended.
    //
    // Correctness comes from consumer-side idempotency, not from the guarantee
    // that events arrive exactly once — the server ALWAYS re-streams from the
    // beginning of the run's Redis stream on every (re)connect.
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    if (runId) {
      store.clearStreamingBlocks(ctx.sessionId, runId); // clear all kinds
      store.clearRunStreamOffsets(ctx.sessionId, runId); // reset offset watermark
      store.reopenRunStream(ctx.sessionId, runId); // re-open channel for this leg
      // Clear visible pendings for this run.  On a RESUMED leg the stream is
      // replayed from the beginning; addPendingInteraction rebuilds only
      // unacknowledged cards and suppresses approvals already submitted.
      store.clearPendingInteractions(ctx.sessionId, runId);
    }
    // Mark streaming live.  The optimistic setIsStreaming(true) in sendMessage
    // covers the gap between POST return and the first run_started arriving.
    store.setIsStreaming(true);
  },

  message_created(_ctx, data) {
    // Backend echo of the user message — may replace the optimistic entry.
    const store = useChatStore.getState();
    store.addMessage(_ctx.sessionId, data as unknown as Message);
  },

  token(ctx, data) {
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const delta = data.delta as string | undefined;
    // Redis stream entry id ("<ms>-<seq>"); enables idempotent replay so a
    // reconnect/StrictMode-remount that re-streams from "0-0" can't double or
    // scramble the streamed text.  Optional — older backends omit it.
    const offset = data.offset as string | undefined;
    if (runId && delta !== undefined) {
      store.appendStreamingToken(ctx.sessionId, runId, delta, offset);
    }
  },

  tool(ctx, data) {
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const kind = data.kind as 'call' | 'result' | undefined;
    const toolCallId = data.tool_call_id as string | undefined;
    const toolName = data.name as string | undefined;
    const offset = data.offset as string | undefined;
    if (runId && kind && toolCallId) {
      if (toolName) {
        store.upsertStreamingTool(ctx.sessionId, runId, {
          kind,
          tool_call_id: toolCallId,
          name: toolName,
          args: kind === 'call' ? (data.args as string | undefined) : undefined,
          result: kind === 'result' ? (data.result as string | undefined) : undefined,
          state: kind === 'result' ? (data.state as string | undefined) : undefined,
        }, offset);
      }
      if (kind === 'result') {
        // HITL self-heal: a tool_result for this tool_call_id means the
        // interaction has been consumed (confirm approved / answered).  On a
        // replay-only reconnect the pending_confirmation event re-delivers the
        // card; acknowledge + remove it the moment the replayed tool_result
        // lands so older pending events cannot resurrect a consumed card.
        store.markPendingInteractionAcknowledged(ctx.sessionId, runId, toolCallId);
        store.removePendingInteraction(ctx.sessionId, toolCallId);
      }
    }
  },

  thinking(ctx, data) {
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const text = data.text as string | undefined;
    const offset = data.offset as string | undefined;
    if (runId && text) {
      store.appendStreamingThinking(ctx.sessionId, runId, text, offset);
    }
  },

  plan(ctx, data) {
    // Real-time plan steps from the worker's TaskCreate/TaskUpdate calls.
    // Shape: { run_id: string, plan_steps: PlanStep[] }
    const steps = data.plan_steps;
    if (Array.isArray(steps) && steps.length > 0) {
      useChatStore.getState().setPlanSteps(
        ctx.sessionId,
        steps as import('../lib/designFixtures').PlanStep[],
      );
    }
  },

  message(ctx, data) {
    // A complete message row from the server.  The orchestrator stores roles
    // lowercase ("assistant"/"user"/"system"/"tool"); compare case-insensitively.
    const store = useChatStore.getState();
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
      store.clearStreamingBlocks(ctx.sessionId, msg.run_id, kindForRole);
      store.addMessage(ctx.sessionId, msg);
      // A durable row for this run has landed → the run reached terminal and was
      // hydrated.  Close its streaming channel and drop its offset watermark so
      // any further (replayed) token/tool/thinking events are discarded instead
      // of rebuilding a duplicate set of side-by-side streaming blocks.  A future
      // `run_started` (resume / new leg) reopens the channel.
      store.closeRunStream(ctx.sessionId, msg.run_id);
      store.clearRunStreamOffsets(ctx.sessionId, msg.run_id);
    } else {
      store.addMessage(ctx.sessionId, msg);
    }
  },

  agent_message(ctx, data) {
    // Legacy event name — treat same as `message`.
    useChatStore.getState().addMessage(ctx.sessionId, data as unknown as Message);
  },

  // ── HITL event handlers ──────────────────────────────────────────────────
  //
  // Backend SSE contract (ALL IMPLEMENTED — consumed here):
  //   pending_confirmation {run_id, tool_call_id, name, args}
  //     → addPendingInteraction(kind:'confirm')
  //   pending_question {run_id, tool_call_id, question}
  //     → addPendingInteraction(kind:'question')
  //   run_paused {run_id}
  //     → setIsStreaming(false)  (worker exited; pending cards remain)
  //
  // Note: `run_resumed` is NOT emitted by the backend.  The resumed leg fires
  // a new `run_started` (same run_id), which triggers the normal
  // clearStreamingBlocks + clearRunStreamOffsets + setIsStreaming(true) path.
  // No separate `run_resumed` handler is needed.

  pending_confirmation(ctx, data) {
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const toolCallId = data.tool_call_id as string | undefined;
    const name = data.name as string | undefined;
    const args = data.args as string | undefined;
    if (runId && toolCallId) {
      store.addPendingInteraction(ctx.sessionId, {
        runId,
        toolCallId,
        kind: 'confirm',
        name,
        args,
      });
    }
  },

  pending_question(ctx, data) {
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const toolCallId = data.tool_call_id as string | undefined;
    const question = data.question as string | undefined;
    if (runId && toolCallId) {
      store.addPendingInteraction(ctx.sessionId, {
        runId,
        toolCallId,
        kind: 'question',
        question,
      });
    }
  },

  run_paused(_ctx) {
    // Worker exited to save resources while paused.  The run is NOT complete —
    // it will be respawned when the user responds.  We flip isStreaming(false)
    // so the "Working on it…" pill disappears, but the pending cards (set by
    // pending_confirmation / pending_question) remain visible and the composer
    // stays DISABLED via the hasPendingInteraction selector in ChatWindow.
    useChatStore.getState().setIsStreaming(false);
  },

  run_complete(_ctx, data) {
    // Run finished — clear the streaming indicator AND any remaining pending
    // interactions (a resumed-run that completes without further confirmation
    // should clear the cards automatically).
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    store.setIsStreaming(false);
    if (runId) {
      store.clearPendingInteractions(_ctx.sessionId, runId);
    } else {
      store.clearPendingInteractions(_ctx.sessionId);
    }

    // Final durable-row backstop.  The silent-stall watchdog + replay path
    // recovers a stream that stopped mid-flight, but a stream can also reach
    // run_complete having dropped an individual durable row (e.g. a `message`
    // frame lost to a transient hiccup that did NOT stall the whole tail).  On
    // every completion we do a fire-and-forget re-pull of the canonical message
    // list and feed each row back through addMessage — which dedups by id and
    // inserts in sequence order, so this is fully idempotent and cheap when
    // nothing is missing.  This is the LAST line of defence for lost durable
    // rows; it never blocks the handler (must stay synchronous), so we run it
    // inside a detached async IIFE.
    const sessionId = _ctx.sessionId;
    void (async () => {
      try {
        const page = await api.messages.list(sessionId);
        const s = useChatStore.getState();
        for (const m of page.items) {
          s.addMessage(sessionId, m);
        }
      } catch (err) {
        console.warn('[useChat] run_complete reconcile failed', err);
      }
    })();
  },

  run_stream_end(ctx, data) {
    // The orchestrator emits this when a run's Redis token *tail* for THIS
    // connection ends — on normal completion AND on replay-only reconnects
    // (where no run_started/run_complete pair is re-delivered).  Payload:
    // { run_id, paused }.
    //
    // IMPORTANT: run_stream_end means "this connection's token tail ended",
    // NOT "the run completed".  An active run goes quiet between tool batches
    // while the model decides the next step; the tail can legitimately end and
    // be re-opened on a later poll.  We therefore do NOT flip isStreaming to
    // false here — `run_complete` (and the durable-message path) are the
    // authoritative completers.  On a terminal replay reconnect the outer
    // orchestrator loop still emits run_complete (the completion_primed
    // synthetic run_complete and the post-stream run_complete), so the
    // "Working on it…" pill is cleared by that path — not by this handler.
    const store = useChatStore.getState();
    const runId = data.run_id as string | undefined;
    const paused = data.paused === true;
    if (!paused) {
      // Stream tail ended without a pending interaction → clear any pending
      // cards resurrected by a replay-only reconnect for this run.  (We do
      // NOT touch isStreaming — see comment above.)
      if (runId) store.clearPendingInteractions(ctx.sessionId, runId);
    }
    // When paused === true the pending_confirmation event (relayed in the same
    // stream) is the source of truth — leave the card and let the user act.
  },

  ping(_ctx, _data) {
    // SSE keepalive heartbeat.  The orchestrator emits `event: ping` roughly
    // once a second while it is otherwise idle (poll spin between runs, or an
    // active tail that is momentarily quiet between tool batches).  It carries
    // no application state — its ONLY purpose is liveness.  We intentionally do
    // nothing here so it does not fall into the unknown-event console.warn path;
    // the value is extracted in runConnectLoop, which treats every parsed event
    // (ping included) as proof the connection is alive and arms the silent-
    // stall watchdog once the first ping has been seen.
  },

  done(_ctx, _data) {
    // The server closes the SSE window after ~5 min and emits `done`.  This
    // is NOT a run-completion signal; a run_complete will have preceded it if
    // a run was active.  We call onDone so the reconnect loop can reopen the
    // connection.  We deliberately do NOT flip isStreaming here.
    _ctx.onDone();
  },

  error(ctx, data) {
    useChatStore.getState().addMessage(ctx.sessionId, {
      id: `err_${Date.now()}`,
      session_id: ctx.sessionId,
      role: 'SYSTEM',
      content: `Error: ${(data as Record<string, unknown>).detail || 'Unknown error'}`,
      tool_calls: null,
      run_id: null,
      sequence: 0,
      created_at: new Date().toISOString(),
    });
    ctx.onDone();
  },
};

/**
 * Dispatch a single SSE event through the handler registry.
 *
 * Exported for use in tests — allows tests to drive the full SSE_HANDLERS
 * adapter map rather than maintaining a parallel reimplementation.
 *
 * Why we capture the sessionId at connection-time
 * ------------------------------------------------
 * Both the persistent stream effect and sendMessage capture the sessionId
 * when the connection/call is made.  If the user switches sessions while a
 * stream is still open, every subsequent event is routed to the original
 * session — never to whatever is currently active.
 */
export function handleSSEEvent(
  sessionId: string,
  eventType: string,
  data: Record<string, unknown>,
  onDone: () => void,
): void {
  const handler = SSE_HANDLERS[eventType];
  if (handler) {
    handler({ sessionId, onDone }, data);
  } else {
    // Never silently drop unknown events — a previously-unhandled `message`
    // event name silently broke the UI; warn loudly so it's never missed.
    console.warn('[useChat] Unknown SSE event type:', eventType, data);
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
export async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (eventType: string, data: Record<string, unknown>) => void,
): Promise<void> {
  const decoder = new TextDecoder();
  let buffer = '';
  let eventType = '';
  let dataLines: string[] = [];

  const dispatchEvent = () => {
    if (dataLines.length === 0) {
      eventType = '';
      return;
    }
    const rawData = dataLines.join('\n');
    const type = eventType || 'message';
    // Two SEPARATE try/catch blocks on purpose.  Parsing and dispatching used to
    // share one try, so a throw from inside a handler (onEvent) was swallowed by
    // the "non-JSON frame" catch — a real handler bug would vanish silently and,
    // worse, could look like a malformed frame.  Split them: a JSON.parse failure
    // is an expected non-JSON data frame (skip quietly, SSE allows arbitrary
    // data); a handler throw is a bug we must surface, but it must NOT abort the
    // read loop — one bad frame can't be allowed to tear down the whole stream.
    let data: Record<string, unknown>;
    try {
      data = JSON.parse(rawData);
    } catch {
      // Non-JSON data — skip silently (SSE allows arbitrary data frames).
      eventType = '';
      dataLines = [];
      return;
    }
    try {
      onEvent(type, data);
    } catch (err) {
      console.error('[useChat] SSE handler error for', type, err);
    }
    eventType = '';
    dataLines = [];
  };

  const processLine = (rawLine: string) => {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
    if (line === '') {
      dispatchEvent();
      return;
    }
    if (line.startsWith(':')) return; // SSE comment / heartbeat.

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    if (field === 'event') {
      eventType = value;
    } else if (field === 'data') {
      dataLines.push(value);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      processLine(line);
    }
  }

  buffer += decoder.decode();
  if (buffer) {
    processLine(buffer);
  }
  dispatchEvent();
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
 *
 * C2 — single subscription (HARD requirement):
 * Only ONE stream is ever open per session.  A ref-counted handle tracks all
 * subscribers; opening for a session that already has a live stream increments
 * the ref count without opening a new fetch.  Cleanup decrements and schedules
 * teardown (with grace delay for StrictMode remounts).  This prevents the token-
 * doubling that a double-open causes when the server replays from "0-0".
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

// ── Silent-stall watchdog ─────────────────────────────────────────────────────
//
// The bug this guards against: the backend session SSE stream can go silent
// mid-flight — the orchestrator's event loop starves after emitting a few
// tokens, so the worker has finished and Redis holds the full transcript, but
// the socket sends no further bytes AND never closes.  runConnectLoop's reader
// only reconnects when the read loop RETURNS (EOF) or THROWS; "connected but
// forever byteless" is invisible to it, so the UI hangs (a lone THOUGHT, no
// answer) until a manual refresh.
//
// The fix, in the same "consumer-side idempotent convergence" spirit as C2/C3:
// the backend now emits an `event: ping` heartbeat roughly once a second while
// otherwise idle.  We time the gap between parsed SSE events (pings included)
// and, if it exceeds the threshold, abort THIS connection attempt so the outer
// loop reconnects.  On reconnect the server replays from the head (synthetic
// run_complete + full message replay + a run_started that resets the offset
// watermark), and the existing idempotency (offset watermark, closedStreamRuns,
// addMessage id-dedup) makes the replay converge with zero extra state.
//
// Why only AFTER the first ping (`sawPing`):
//   Older backends that predate the heartbeat never send pings, and on those a
//   legitimately busy run can sit silent for 30s+ between tool batches while the
//   model thinks.  Arming the watchdog there would kill healthy connections.
//   Requiring at least one ping first means we only ever fire against a backend
//   that has PROMISED ~1Hz liveness — so a long gap genuinely means "stalled",
//   not "thinking".
//
// Why 30s:
//   Comfortably above the ~1s ping cadence (so ordinary jitter, GC pauses, or a
//   brief network hiccup never trip it) yet short enough that a real stall is
//   recovered in well under the old "refresh the page" timescale.
const WATCHDOG_STALL_MS = 30_000;
const WATCHDOG_CHECK_MS = 5_000;

/**
 * Pure predicate for the silent-stall watchdog: should the current connection
 * attempt be aborted?  Extracted so the decision is unit-testable without
 * standing up a fetch/reader.
 *
 *   sawPing     — has THIS connection received at least one `ping` event?
 *   lastEventAt — Date.now() of the most recent parsed SSE event (any type).
 *   now         — current time.
 *
 * Only fires once a ping has been seen (see the sawPing rationale above) and the
 * gap since the last event exceeds WATCHDOG_STALL_MS.
 */
export function shouldWatchdogFire(
  sawPing: boolean,
  lastEventAt: number,
  now: number,
): boolean {
  return sawPing && now - lastEventAt > WATCHDOG_STALL_MS;
}

/** The reconnecting fetch loop for one session's SSE stream. */
async function runConnectLoop(sid: string, handle: StreamHandle): Promise<void> {
  const { controller: master } = handle;
  let backoffMs = 1000;

  while (!handle.cancelled) {
    const connectStart = Date.now();

    // Per-attempt controller.  The master controller (handle.controller) is the
    // real unsubscribe/teardown signal; each connection attempt gets its OWN
    // controller so the watchdog can abort just this attempt (triggering a
    // reconnect) without touching the master.  We bridge master → attempt so a
    // genuine teardown still kills the in-flight fetch.
    const attempt = new AbortController();
    const onMasterAbort = () => attempt.abort();
    if (master.signal.aborted) {
      // Torn down between iterations — bail before opening another fetch.
      break;
    }
    master.signal.addEventListener('abort', onMasterAbort, { once: true });

    // Watchdog state for THIS attempt.  lastEventAt is refreshed by every parsed
    // event (including ping) via the onEvent callback below; sawPing latches once
    // the first ping lands and only then does the interval arm the abort.
    let lastEventAt = Date.now();
    let sawPing = false;
    let watchdogFired = false;
    const watchdogTimer = setInterval(() => {
      if (shouldWatchdogFire(sawPing, lastEventAt, Date.now())) {
        watchdogFired = true;
        attempt.abort();
      }
    }, WATCHDOG_CHECK_MS);

    try {
      const res = await fetch(`/api/bff/v1/sessions/${sid}/stream`, {
        credentials: 'include',
        signal: attempt.signal,
      });

      if (!res.ok || !res.body) {
        console.warn(`[useChat] session stream ${res.status} for ${sid}; backing off ${backoffMs}ms`);
        await delay(backoffMs, attempt.signal);
        backoffMs = Math.min(backoffMs * 2, 8000);
        continue;
      }

      backoffMs = 1000;

      const reader = res.body.getReader();
      let doneFired = false;

      // Explicitly cancel the reader when the attempt aborts.  Aborting the
      // fetch signal errors the body stream in a live browser, but we do NOT
      // rely on that propagation: a watchdog abort must reject the parked
      // reader.read() promptly (and this also makes the path testable with a
      // hand-rolled response body).  reader.cancel() rejects the pending read,
      // surfacing as an AbortError-shaped rejection routed to the catch below.
      const onAttemptAbort = () => {
        reader.cancel().catch(() => {});
      };
      if (attempt.signal.aborted) {
        onAttemptAbort();
      } else {
        attempt.signal.addEventListener('abort', onAttemptAbort, { once: true });
      }

      try {
        await parseSSEStream(reader, (eventType, data) => {
          // Every parsed event proves the connection is alive — refresh the
          // watchdog clock before dispatching (ping included; that's the point).
          lastEventAt = Date.now();
          if (eventType === 'ping') sawPing = true;
          if (eventType === 'done') doneFired = true;
          handleSSEEvent(sid, eventType, data, () => {
            doneFired = true;
          });
        });
      } finally {
        attempt.signal.removeEventListener('abort', onAttemptAbort);
      }

      // parseSSEStream returned normally.  If the watchdog aborted mid-stream,
      // reader.cancel() makes the read loop hit EOF (a clean return) rather than
      // throw — so translate a watchdog abort here into the reconnect path
      // instead of falling through to the "closed unexpectedly" backoff.
      if (watchdogFired && !handle.cancelled) {
        console.warn(
          `[useChat] session stream for ${sid} silent >${WATCHDOG_STALL_MS}ms after ping; watchdog reconnecting`,
        );
        backoffMs = 1000;
        try {
          await delay(backoffMs, master.signal);
          continue;
        } catch {
          break; // master aborted during backoff delay
        }
      }

      if (!handle.cancelled) {
        if (doneFired) {
          await delay(1000, attempt.signal);
        } else {
          console.warn(`[useChat] session stream closed unexpectedly for ${sid}; reconnecting in ${backoffMs}ms`);
          await delay(backoffMs, attempt.signal);
          backoffMs = Math.min(backoffMs * 2, 8000);
        }
      }
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        // Distinguish a genuine teardown from a watchdog-forced reconnect.  Only
        // a real unsubscribe (handle.cancelled) should exit the loop; a watchdog
        // abort must fall through to the reconnect path.
        if (handle.cancelled) break;
        if (watchdogFired) {
          console.warn(
            `[useChat] session stream for ${sid} silent >${WATCHDOG_STALL_MS}ms after ping; watchdog reconnecting`,
          );
          // Reset backoff to 1s: the server is reachable (it was heartbeating),
          // this is a stall not an outage, so reconnect promptly.
          backoffMs = 1000;
          try {
            await delay(backoffMs, master.signal);
          } catch {
            break; // master aborted during backoff delay
          }
          continue;
        }
        // AbortError without cancelled/watchdogFired: master aborted mid-fetch
        // (teardown raced the once-listener) — exit.
        break;
      }
      const elapsed = Date.now() - connectStart;
      if (elapsed < 200) {
        backoffMs = Math.min(backoffMs * 2, 8000);
      }
      if (!handle.cancelled) {
        console.warn(`[useChat] session stream error for ${sid}; reconnecting in ${backoffMs}ms`, e);
        try {
          await delay(backoffMs, master.signal);
        } catch {
          break; // aborted during backoff delay
        }
      }
    } finally {
      // Always tear down this attempt's watchdog + master bridge so neither
      // leaks across reconnects.
      clearInterval(watchdogTimer);
      master.signal.removeEventListener('abort', onMasterAbort);
    }
  }
}

/**
 * Apply a batch of paused HITL interactions (from GET /sessions/{id}/pending)
 * to the chat store on mount.  Mirrors the SSE `pending_confirmation` /
 * `pending_question` handlers above so the rehydrated cards are identical to
 * the live-streamed ones.  `addPendingInteraction` dedups by toolCallId and
 * respects acknowledged interactions, so this is safe to run alongside SSE
 * replay (no duplicate or resurrected cards).
 *
 * Exported so it can be unit-tested directly against the store, matching the
 * store-driven style of the other tests in this file.
 */
export function applyPendingInteractions(
  sessionId: string,
  items: PendingInteractionItem[],
): void {
  const store = useChatStore.getState();
  for (const it of items) {
    if (it.kind === 'question') {
      const tc = it.tool_calls[0];
      store.addPendingInteraction(sessionId, {
        runId: it.run_id,
        toolCallId: tc?.id ?? it.run_id,
        kind: 'question',
        question: it.question ?? undefined,
      });
    } else {
      for (const tc of it.tool_calls) {
        store.addPendingInteraction(sessionId, {
          runId: it.run_id,
          toolCallId: tc.id,
          kind: 'confirm',
          name: tc.name,
          args: tc.args,
        });
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

      // Rehydrate any HITL pause that happened while the page was closed (or on
      // an earlier run in this session).  The live SSE pending_confirmation /
      // pending_question event is missed on refresh/reconnect, so without this
      // the run sits PAUSED with no card.  applyPendingInteractions mirrors the
      // SSE handlers and is dedup-safe, so it can run alongside stream replay.
      // Wrapped in its own try/catch so a failure never breaks message hydration.
      try {
        const pending = await api.sessions.pending(sid);
        applyPendingInteractions(sid, pending.items);
      } catch (err) {
        console.warn('load pending interactions failed', err);
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
  //   2. Parse SSE via parseSSEStream → handleSSEEvent → SSE_HANDLERS.
  //   3. On clean EOF (server `done` event followed by stream close):
  //      reconnect after 1 s backoff if the session is still active.
  //   4. On immediate connect failure (network down, 401, etc.):
  //      back off exponentially (1 s → 2 s → 4 s, capped at 8 s) to avoid
  //      storm-reconnect loops that would saturate the server.
  //   5. Cleanup (session change / unmount) aborts the in-flight fetch via
  //      streamAbortRef, which causes the reader to throw AbortError → loop exits.
  //
  // C2 — single subscription: the actual SSE connection lives in a module-level
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
  // The `res.body.getReader()` SSE loop that was previously here is GONE — the
  // persistent stream effect handles all events.
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
