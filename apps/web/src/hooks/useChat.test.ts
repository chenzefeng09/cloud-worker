/**
 * useChat tests — SSE events route to the correct session and build ordered
 * blocks in the store.
 *
 * Tests drive the real SSE_HANDLERS adapter map via the exported handleSSEEvent,
 * rather than maintaining a parallel re-implementation of the routing logic.
 * This means a change to a handler is automatically tested here.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { useChatStore } from '../stores/chatStore';
import {
  handleSSEEvent,
  parseSSEStream,
  subscribeSessionStream,
  applyPendingInteractions,
  shouldWatchdogFire,
} from './useChat';
import type { ToolCallEntry, PendingInteractionItem, Message } from '../api/types';
import { api } from '../api/client';

// Mock the API client so the run_complete reconcile path (fire-and-forget
// api.messages.list) can be asserted without a real fetch.  Default the mock to
// an empty page so unrelated run_complete tests don't trip the reconcile's
// undefined-.items path; tests that assert reconcile behaviour override it.
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      messages: {
        ...actual.api.messages,
        list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      },
    },
  };
});

/**
 * Thin wrapper so tests don't need to pass a no-op `onDone` everywhere.
 * Matches the shape of `handleSSEEvent(sessionId, event, data, onDone)`.
 */
function dispatchSSE(
  sessionId: string,
  event: string,
  data: Record<string, unknown>,
  onDone: () => void = () => {},
) {
  handleSSEEvent(sessionId, event, data, onDone);
}

function readerFromChunks(chunks: string[]): ReadableStreamDefaultReader<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk));
      }
      controller.close();
    },
  }).getReader();
}

describe('parseSSEStream', () => {
  it('preserves event type when event and data arrive in different chunks', async () => {
    const events: Array<[string, Record<string, unknown>]> = [];
    await parseSSEStream(
      readerFromChunks([
        'event: token\n',
        'data: {"run_id":"run_1","delta":"A","offset":"1-0"}\n\n',
      ]),
      (event, data) => events.push([event, data]),
    );

    expect(events).toEqual([
      ['token', { run_id: 'run_1', delta: 'A', offset: '1-0' }],
    ]);
  });

  it('parses multi-line data frames and data fields without a space', async () => {
    const events: Array<[string, Record<string, unknown>]> = [];
    await parseSSEStream(
      readerFromChunks([
        'event: token\n',
        'data: {"run_id":"run_1",\n',
        'data:"delta":"A",\n',
        'data: "offset":"10-0"}\n\n',
      ]),
      (event, data) => events.push([event, data]),
    );

    expect(events).toEqual([
      ['token', { run_id: 'run_1', delta: 'A', offset: '10-0' }],
    ]);
  });

  it('flushes a final frame even when EOF arrives without a blank line', async () => {
    const events: Array<[string, Record<string, unknown>]> = [];
    await parseSSEStream(
      readerFromChunks(['event: done\ndata: {}']),
      (event, data) => events.push([event, data]),
    );

    expect(events).toEqual([['done', {}]]);
  });
});

describe('SSE → store routing', () => {
  beforeEach(() => {
    useChatStore.setState({
      sessions: [],
      activeSessionId: 'ses_B',
      messages: {},
      streamOffsets: {},
      closedStreamRuns: {},
      isStreaming: false,
      isLoggedIn: true,
      userProfile: null,
      bootstrapped: true,
      pendingInteractions: {},
      acknowledgedPendingInteractions: {},
    });
  });

  it('routes message to the snapshotted session, not the active one', () => {
    dispatchSSE('ses_A', 'agent_message', {
      id: 'msg_1',
      session_id: 'ses_A',
      role: 'ASSISTANT',
      content: 'hi from A',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01',
    });
    const s = useChatStore.getState();
    expect(s.messages['ses_A']).toHaveLength(1);
    expect(s.messages['ses_B']).toBeUndefined();
  });

  it('two parallel streams route messages independently', () => {
    dispatchSSE('ses_A', 'agent_message', {
      id: 'a1', session_id: 'ses_A', role: 'ASSISTANT', content: 'a',
      tool_calls: null, run_id: null, sequence: 1, created_at: '2026-01-01',
    });
    dispatchSSE('ses_B', 'agent_message', {
      id: 'b1', session_id: 'ses_B', role: 'ASSISTANT', content: 'b',
      tool_calls: null, run_id: null, sequence: 1, created_at: '2026-01-01',
    });
    const s = useChatStore.getState();
    expect(s.messages['ses_A'][0].content).toBe('a');
    expect(s.messages['ses_B'][0].content).toBe('b');
  });

  // ─── Token streaming pipeline ─────────────────────────────────────────────

  it('token events build up an open text block', () => {
    dispatchSSE('ses_A', 'token', { run_id: 'run_1', delta: 'Hello' });
    dispatchSSE('ses_A', 'token', { run_id: 'run_1', delta: ' world' });
    const msgs = useChatStore.getState().messages['ses_A'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('Hello world');
    expect(msgs[0].streaming).toBe(true);
    expect(msgs[0].blockKind).toBe('text');
  });

  // ─── Ordered block construction: token → tool → token ────────────────────

  it('token → tool → token produces text, tool, text in order', () => {
    dispatchSSE('ses_A', 'token', { run_id: 'run_2', delta: 'Before' });
    dispatchSSE('ses_A', 'tool', {
      run_id: 'run_2', kind: 'call', tool_call_id: 'tc_1', name: 'Read',
      args: JSON.stringify({ file_path: '/index.ts' }),
    });
    dispatchSSE('ses_A', 'tool', {
      run_id: 'run_2', kind: 'result', tool_call_id: 'tc_1', name: 'Read',
      result: 'file content', state: 'success',
    });
    dispatchSSE('ses_A', 'token', { run_id: 'run_2', delta: 'After' });

    const msgs = useChatStore.getState().messages['ses_A'];
    expect(msgs).toHaveLength(3);
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].content).toBe('Before');
    expect(msgs[0].streaming).toBe(false); // sealed when tool arrived
    expect(msgs[1].blockKind).toBe('tool');
    expect(msgs[1].id).toBe('tool_run_2_tc_1');
    expect(msgs[2].blockKind).toBe('text');
    expect(msgs[2].content).toBe('After');
    expect(msgs[2].streaming).toBe(true); // still open
  });

  // ─── Thinking pipeline ────────────────────────────────────────────────────

  it('thinking event does not split an already-started text answer', () => {
    dispatchSSE('ses_A', 'token', { run_id: 'run_3', delta: 'intro' });
    dispatchSSE('ses_A', 'thinking', { run_id: 'run_3', text: 'plan the next step' });
    dispatchSSE('ses_A', 'token', { run_id: 'run_3', delta: 'result' });

    const msgs = useChatStore.getState().messages['ses_A'];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].blockKind).toBe('thinking');
    expect(msgs[0].content).toBe('plan the next step');
    expect(msgs[1].blockKind).toBe('text');
    expect(msgs[1].streaming).toBe(true);
    expect(msgs[1].content).toBe('introresult');
  });

  // ─── message event: clear temp blocks + add real row ─────────────────────

  it('message event with ASSISTANT role clears temp blocks and adds real row', () => {
    dispatchSSE('ses_A', 'token', { run_id: 'run_4', delta: 'Draft' });
    dispatchSSE('ses_A', 'message', {
      id: 'final_msg',
      session_id: 'ses_A',
      role: 'ASSISTANT',
      content: 'Draft',
      tool_calls: null,
      run_id: 'run_4',
      sequence: 1,
      created_at: '2026-01-01',
    });
    const msgs = useChatStore.getState().messages['ses_A'];
    // Temp block cleared, real message added
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('final_msg');
    expect(msgs[0].streaming).toBeFalsy();
    expect(msgs[0].blockKind).toBeUndefined();
  });

  it('THINKING durable message does not wipe the live-streamed assistant text', () => {
    // Live stream: assistant text + thinking for the same run.
    dispatchSSE('ses_A', 'token', { run_id: 'run_t', delta: 'the answer' });
    dispatchSSE('ses_A', 'thinking', { run_id: 'run_t', text: 'let me think' });
    // Durable THINKING row arrives FIRST (orchestrator order) — must only
    // replace the streamed thinking block, not the streamed text.
    dispatchSSE('ses_A', 'message', {
      id: 'think_row',
      session_id: 'ses_A',
      role: 'thinking',
      content: 'let me think',
      tool_calls: null,
      run_id: 'run_t',
      sequence: 1,
      created_at: '2026-01-01',
    });
    const after = useChatStore.getState().messages['ses_A'];
    // Streamed text survives; thinking swapped for the durable row.
    expect(after.some((m) => m.blockKind === 'text' && m.content === 'the answer')).toBe(true);
    expect(after.some((m) => m.id === 'think_row')).toBe(true);
    expect(after.some((m) => m.id === 'thinking_run_t_0')).toBe(false);
  });

  it('durable assistant message closes the run stream: a later token is dropped (no side-by-side rebuild)', () => {
    // Durable assistant row arrives (run reached terminal, hydrated).
    dispatchSSE('ses_A', 'message', {
      id: 'final_msg',
      session_id: 'ses_A',
      role: 'assistant',
      content: 'the answer',
      tool_calls: null,
      run_id: 'run_dup',
      sequence: 1,
      created_at: '2026-01-01',
    });
    // Server replays the Redis token stream for the SAME run afterwards.
    dispatchSSE('ses_A', 'token', { run_id: 'run_dup', delta: 'the answer', offset: '1-0' });

    const msgs = useChatStore.getState().messages['ses_A'];
    // Only the durable row survives — no replayed streaming text block rebuilt.
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('final_msg');
    expect(msgs.some((m) => m.blockKind === 'text' && m.streaming)).toBe(false);
    // run_started reopens the channel so a re-run / new leg can stream again.
    dispatchSSE('ses_A', 'run_started', { run_id: 'run_dup' });
    dispatchSSE('ses_A', 'token', { run_id: 'run_dup', delta: 'rerun', offset: '1-0' });
    const after = useChatStore.getState().messages['ses_A'];
    expect(after.some((m) => m.blockKind === 'text' && m.content === 'rerun')).toBe(true);
  });

  it('after a full turn hydrates (thinking + tool + 2 text rows), streaming block count for the run is 0', () => {
    const runId = 'run_full';
    // Live stream builds blocks: thinking, tool, then two text segments.
    dispatchSSE('ses_A', 'thinking', { run_id: runId, text: 'thinking', offset: '1-0' });
    dispatchSSE('ses_A', 'tool', {
      run_id: runId, kind: 'call', tool_call_id: 'tc_1', name: 'WebFetch', args: '{}', offset: '2-0',
    });
    dispatchSSE('ses_A', 'token', { run_id: runId, delta: 'part one', offset: '3-0' });
    dispatchSSE('ses_A', 'thinking', { run_id: runId, text: 'more', offset: '4-0' });
    dispatchSSE('ses_A', 'token', { run_id: runId, delta: 'part two', offset: '5-0' });

    // All durable rows arrive (orchestrator order: thinking, tool, assistant…).
    const durable = [
      { id: 'd_think_0', role: 'thinking', content: 'thinking', tool_calls: null },
      {
        id: 'd_tool_0', role: 'tool', content: '',
        tool_calls: [{ id: 'tc_1', name: 'WebFetch', input: {}, result: 'ok', state: 'success' }],
      },
      { id: 'd_think_1', role: 'thinking', content: 'more', tool_calls: null },
      { id: 'd_text_0', role: 'assistant', content: 'part one', tool_calls: null },
      { id: 'd_text_1', role: 'assistant', content: 'part two', tool_calls: null },
    ];
    durable.forEach((row, i) =>
      dispatchSSE('ses_A', 'message', {
        ...row, session_id: 'ses_A', run_id: runId, sequence: i + 1, created_at: '2026-01-01',
      }),
    );

    const msgs = useChatStore.getState().messages['ses_A'];
    const streamingForRun = msgs.filter((m) => m.streamRunId === runId);
    // No transient streaming blocks left for this run — only durable rows remain.
    expect(streamingForRun).toHaveLength(0);
    expect(msgs.filter((m) => m.run_id === runId && !m.streamRunId)).toHaveLength(5);

    // A subsequent replayed token/tool/thinking is discarded, not re-rendered.
    dispatchSSE('ses_A', 'token', { run_id: runId, delta: 'part one', offset: '3-0' });
    dispatchSSE('ses_A', 'thinking', { run_id: runId, text: 'thinking', offset: '1-0' });
    expect(
      useChatStore.getState().messages['ses_A'].filter((m) => m.streamRunId === runId),
    ).toHaveLength(0);
  });

  it('message event with TOOL role clears temp tool blocks', () => {
    dispatchSSE('ses_A', 'tool', {
      run_id: 'run_5', kind: 'call', tool_call_id: 'tc_x', name: 'Read', args: '{}',
    });
    dispatchSSE('ses_A', 'message', {
      id: 'msg_tool_5',
      session_id: 'ses_A',
      role: 'tool',
      content: '',
      tool_calls: [{ id: 'tc_x', name: 'Read', input: {}, result: 'ok', state: 'success' }],
      run_id: 'run_5',
      sequence: 2,
      created_at: '2026-01-01',
    });
    const msgs = useChatStore.getState().messages['ses_A'];
    const ids = msgs.map((m) => m.id);
    expect(ids).not.toContain('tool_run_5_tc_x');
    expect(ids).toContain('msg_tool_5');
  });

  it('tool result event merges into existing tool block', () => {
    dispatchSSE('ses_A', 'tool', {
      run_id: 'run_6',
      kind: 'call',
      tool_call_id: 'tc_a',
      name: 'Grep',
      args: '{"pattern":"foo"}',
    });
    dispatchSSE('ses_A', 'tool', {
      run_id: 'run_6',
      kind: 'result',
      tool_call_id: 'tc_a',
      name: 'Grep',
      result: 'line 1\nline 2',
      state: 'success',
    });
    const msgs = useChatStore.getState().messages['ses_A'];
    const calls = msgs[0].tool_calls as ToolCallEntry[];
    expect(calls[0].result).toBe('line 1\nline 2');
    expect(calls[0].state).toBe('success');
  });

  it('message_created user echo replaces the optimistic temp entry', () => {
    useChatStore.getState().addMessage('ses_A', {
      id: 'temp_user_111',
      session_id: 'ses_A',
      role: 'USER',
      content: 'hi there',
      tool_calls: null,
      run_id: null,
      sequence: 0,
      created_at: '2026-01-01',
    });
    dispatchSSE('ses_A', 'message_created', {
      id: 'real_user_1',
      session_id: 'ses_A',
      role: 'USER',
      content: 'hi there',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01',
    });
    const msgs = useChatStore.getState().messages['ses_A'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('real_user_1');
  });

  // ─── Regression: a single (non-doubled) token stream stays clean ──────────
  //
  // Before the singleton-stream fix two useChat() consumers each opened their
  // own SSE connection, so every token/thinking event was applied twice and
  // interleaved → "你好你好" + chopped THOUGHT/text blocks.  With one stream the
  // ordered pipeline produces one thinking block and one clean text block.
  it('single token+thinking stream is not duplicated or chopped', () => {
    dispatchSSE('ses_A', 'thinking', { run_id: 'run_z', text: '让我想想' });
    for (const d of ['我是', '你的', '企业', '级', 'AI', '助理']) {
      dispatchSSE('ses_A', 'token', { run_id: 'run_z', delta: d });
    }
    const msgs = useChatStore.getState().messages['ses_A'];
    const textBlocks = msgs.filter((m) => m.blockKind === 'text');
    const thinkingBlocks = msgs.filter((m) => m.blockKind === 'thinking');
    // Exactly one thinking block and one text block — nothing doubled/chopped.
    expect(thinkingBlocks).toHaveLength(1);
    expect(textBlocks).toHaveLength(1);
    expect(thinkingBlocks[0].content).toBe('让我想想');
    expect(textBlocks[0].content).toBe('我是你的企业级AI助理');
  });

  // ─── Regression: a real USER row never lands below an assistant stream ────
  //
  // The addMessage sequence-ordered insertion must keep a persisted USER row
  // (sequence>0) ahead of the in-progress streaming assistant block (which
  // carries sequence===0), while still leaving the streamed text intact.
  it('persisted user row inserts ahead of an in-progress assistant stream', () => {
    // Assistant tokens streaming for run_q (sequence===0 placeholder).
    dispatchSSE('ses_A', 'token', { run_id: 'run_q', delta: 'partial answer' });
    // A real persisted USER row for the same turn arrives (sequence 1).
    dispatchSSE('ses_A', 'message', {
      id: 'user_row_1',
      session_id: 'ses_A',
      role: 'USER',
      content: 'my question',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01',
    });
    const msgs = useChatStore.getState().messages['ses_A'];
    const userIdx = msgs.findIndex((m) => m.id === 'user_row_1');
    const streamIdx = msgs.findIndex((m) => m.blockKind === 'text' && m.streaming);
    expect(userIdx).toBeGreaterThanOrEqual(0);
    expect(streamIdx).toBeGreaterThanOrEqual(0);
    // USER must come before the streaming assistant block.
    expect(userIdx).toBeLessThan(streamIdx);
    // ...and the streamed text is not wiped.
    expect(msgs[streamIdx].content).toBe('partial answer');
  });

  // ─── run_started: event-driven isStreaming + C3 replay idempotency ─────────
  //
  // C3: on every run_started (including a reconnect mid-run), the handler
  // clears transient streaming blocks AND resets offset watermarks so the
  // server's token replay from "0-0" is applied cleanly, not dropped.
  it('run_started sets isStreaming to true', () => {
    dispatchSSE('ses_A', 'run_started', { run_id: 'run_new' });
    expect(useChatStore.getState().isStreaming).toBe(true);
  });

  it('run_started clears existing streaming blocks for the run (C3 reconnect)', () => {
    // Simulate partial tokens before a reconnect.
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_A', 'run_r', 'partial', '1-0');
    expect(st().messages['ses_A']).toHaveLength(1);

    // Reconnect: server re-emits run_started.
    dispatchSSE('ses_A', 'run_started', { run_id: 'run_r' });

    // Streaming blocks must be wiped so the replay rebuilds them.
    expect(st().messages['ses_A'].filter((m) => m.streamRunId === 'run_r')).toHaveLength(0);
  });

  it('run_started resets offset watermark so replayed tokens apply cleanly (C3)', () => {
    const st = () => useChatStore.getState();
    // First connection: apply some tokens with offsets.
    st().appendStreamingToken('ses_A', 'run_r2', 'A', '1-0');
    st().appendStreamingToken('ses_A', 'run_r2', 'B', '2-0');

    // Reconnect: run_started fires, wiping blocks and resetting offsets.
    dispatchSSE('ses_A', 'run_started', { run_id: 'run_r2' });

    // The offset watermark should now be gone for this run, so replayed
    // tokens from "1-0" are accepted again (not filtered as already-seen).
    const afterReset = st().streamOffsets['ses_A'];
    expect(afterReset?.['run_r2']).toBeUndefined();

    // Replay the same offsets — they must apply now (build new blocks).
    st().appendStreamingToken('ses_A', 'run_r2', 'A', '1-0');
    st().appendStreamingToken('ses_A', 'run_r2', 'B', '2-0');
    const msgs = st().messages['ses_A'];
    const textBlock = msgs.find((m) => m.blockKind === 'text' && m.streamRunId === 'run_r2');
    expect(textBlock).toBeDefined();
    expect(textBlock?.content).toBe('AB');
  });

  it('run_started does not affect streaming blocks of OTHER runs', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_A', 'run_other', 'keep me', '1-0');
    dispatchSSE('ses_A', 'run_started', { run_id: 'run_new_one' });
    // run_other blocks must survive — only run_new_one is cleared.
    const msgs = st().messages['ses_A'];
    expect(msgs.some((m) => m.streamRunId === 'run_other')).toBe(true);
  });

  it('run_complete sets isStreaming to false', () => {
    useChatStore.setState({ isStreaming: true });
    dispatchSSE('ses_A', 'run_complete', {});
    expect(useChatStore.getState().isStreaming).toBe(false);
  });

  it('run_stream_end{paused:false} does NOT flip isStreaming (only the tail ended, not the run)', () => {
    // Regression for the mid-run streaming stall: run_stream_end means "this
    // connection's token tail ended", NOT "the run completed".  An active run
    // goes quiet between tool batches; the tail can end and re-open later, so
    // run_stream_end must leave isStreaming alone.  run_complete remains the
    // authoritative completer.
    useChatStore.setState({ isStreaming: true });
    dispatchSSE('ses_A', 'run_stream_end', { run_id: 'run_1', paused: false });
    expect(useChatStore.getState().isStreaming).toBe(true);

    // run_complete is still what clears it.
    dispatchSSE('ses_A', 'run_complete', { run_id: 'run_1' });
    expect(useChatStore.getState().isStreaming).toBe(false);
  });

  it('run_stream_end{paused:false} still clears resurrected pending cards for the run', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_2', toolCallId: 'tc_02', kind: 'question',
    });
    dispatchSSE('ses_1', 'run_stream_end', { run_id: 'run_1', paused: false });
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    // Only run_1's card cleared; run_2 survives.  (isStreaming is untouched.)
    expect(pendings).toHaveLength(1);
    expect(pendings[0].runId).toBe('run_2');
  });

  it('done calls onDone without flipping isStreaming', () => {
    useChatStore.setState({ isStreaming: true });
    let doneCalled = false;
    dispatchSSE('ses_A', 'done', {}, () => { doneCalled = true; });
    expect(doneCalled).toBe(true);
    // isStreaming must NOT be flipped by done — a run may still be active.
    expect(useChatStore.getState().isStreaming).toBe(true);
  });

  it('unknown event type warns and does not crash', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    dispatchSSE('ses_A', 'totally_unknown_event', { foo: 'bar' });
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('[useChat] Unknown SSE event type:'),
      'totally_unknown_event',
      expect.anything(),
    );
    warnSpy.mockRestore();
  });

  it('ping is a recognised no-op (does NOT hit the unknown-event warn path)', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    dispatchSSE('ses_A', 'ping', {});
    dispatchSSE('ses_A', 'ping', { run_id: 'run_1' });
    // ping is registered, so the unknown-event warn is never reached, and the
    // handler mutates no store state.
    expect(warnSpy).not.toHaveBeenCalled();
    expect(useChatStore.getState().messages['ses_A']).toBeUndefined();
    warnSpy.mockRestore();
  });
});

// ─── Singleton SSE-stream manager: one connection per session ────────────────
describe('subscribeSessionStream — single connection per session', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const aborts: AbortSignal[] = [];

  beforeEach(() => {
    // Teardown is now scheduled on a setTimeout (grace window for StrictMode
    // remounts), so drive time deterministically with fake timers.
    vi.useFakeTimers();
    aborts.length = 0;
    // Never-resolving fetch so the connect loop stays parked on the first await,
    // letting us assert how many connections were opened.
    fetchMock = vi.fn((_url: string, init: { signal: AbortSignal }) => {
      aborts.push(init.signal);
      return new Promise(() => {}); // pending forever
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('two subscribers to the same session open exactly one fetch', () => {
    const unsub1 = subscribeSessionStream('ses_one');
    const unsub2 = subscribeSessionStream('ses_one');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(aborts[0].aborted).toBe(false);
    unsub1();
    // Still one subscriber left — connection stays open.
    expect(aborts[0].aborted).toBe(false);
    unsub2();
    // Last subscriber gone — teardown is scheduled but not yet fired.
    expect(aborts[0].aborted).toBe(false);
    vi.advanceTimersByTime(100); // past the grace window
    expect(aborts[0].aborted).toBe(true);
  });

  it('StrictMode remount (unsubscribe→resubscribe in same tick) keeps one live connection', () => {
    const unsub1 = subscribeSessionStream('ses_sm');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // StrictMode: cleanup runs, then the effect re-runs synchronously.
    unsub1(); // refCount 1→0, teardown scheduled
    const unsub2 = subscribeSessionStream('ses_sm'); // refCount 0→1, cancels teardown
    // No new fetch — the original connection was reclaimed.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(100);
    // Connection survived the flicker: never aborted, no reconnect.
    expect(aborts[0].aborted).toBe(false);
    // Real teardown when the surviving subscriber actually leaves.
    unsub2();
    vi.advanceTimersByTime(100);
    expect(aborts[0].aborted).toBe(true);
  });

  it('different sessions get independent connections', () => {
    const unsubA = subscribeSessionStream('ses_a');
    const unsubB = subscribeSessionStream('ses_b');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    unsubA();
    unsubB();
    vi.advanceTimersByTime(100);
    expect(aborts[0].aborted).toBe(true);
    expect(aborts[1].aborted).toBe(true);
  });
});

// ─── HITL SSE handlers ────────────────────────────────────────────────────────

describe('HITL SSE event handlers', () => {
  beforeEach(() => {
    useChatStore.setState({
      sessions: [],
      activeSessionId: 'ses_1',
      messages: {},
      streamOffsets: {},
      closedStreamRuns: {},
      isStreaming: false,
      isLoggedIn: true,
      userProfile: null,
      bootstrapped: true,
      pendingInteractions: {},
      acknowledgedPendingInteractions: {},
    });
  });

  it('pending_confirmation adds a confirm interaction', () => {
    dispatchSSE('ses_1', 'pending_confirmation', {
      run_id: 'run_1',
      tool_call_id: 'tc_01',
      name: 'Read',
      args: '{"file_path": "/foo.ts"}',
    });
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].kind).toBe('confirm');
    expect(pendings[0].toolCallId).toBe('tc_01');
    expect(pendings[0].name).toBe('Read');
  });

  it('pending_question adds a question interaction', () => {
    dispatchSSE('ses_1', 'pending_question', {
      run_id: 'run_1',
      tool_call_id: 'tc_02',
      question: 'Should I proceed?',
    });
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].kind).toBe('question');
    expect(pendings[0].question).toBe('Should I proceed?');
  });

  it('pending_confirmation is idempotent (replay safe)', () => {
    const data = { run_id: 'run_1', tool_call_id: 'tc_01', name: 'Read', args: '{}' };
    dispatchSSE('ses_1', 'pending_confirmation', data);
    dispatchSSE('ses_1', 'pending_confirmation', data); // replay
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1); // deduplicated
  });

  it('pending_confirmation does not resurrect an acknowledged approval after replay', () => {
    const data = {
      run_id: 'run_1',
      tool_call_id: 'tc_01',
      name: 'WebFetch',
      args: '{"url":"https://wttr.in/Beijing?lang=zh"}',
    };
    dispatchSSE('ses_1', 'pending_confirmation', data);
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);

    useChatStore.getState().markPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_01');
    useChatStore.getState().removePendingInteraction('ses_1', 'tc_01');
    dispatchSSE('ses_1', 'run_started', { run_id: 'run_1' });
    dispatchSSE('ses_1', 'pending_confirmation', data);

    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(0);
  });

  it('tool result consumes the pending interaction before older pending events replay', () => {
    const pending = {
      run_id: 'run_1',
      tool_call_id: 'tc_01',
      name: 'WebFetch',
      args: '{"url":"https://wttr.in/Beijing?lang=zh"}',
    };
    dispatchSSE('ses_1', 'pending_confirmation', pending);
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);

    dispatchSSE('ses_1', 'tool', {
      run_id: 'run_1',
      kind: 'result',
      tool_call_id: 'tc_01',
      name: 'WebFetch',
      result: 'ok',
      state: 'success',
      offset: '2-0',
    });
    dispatchSSE('ses_1', 'pending_confirmation', pending);

    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(0);
  });

  it('run_paused sets isStreaming to false', () => {
    useChatStore.setState({ isStreaming: true });
    dispatchSSE('ses_1', 'run_paused', { run_id: 'run_1' });
    expect(useChatStore.getState().isStreaming).toBe(false);
  });

  it('run_complete clears pending interactions for the completed run', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_2', toolCallId: 'tc_02', kind: 'question',
    });
    dispatchSSE('ses_1', 'run_complete', { run_id: 'run_1' });
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    // Only run_1's interaction cleared; run_2 survives
    expect(pendings).toHaveLength(1);
    expect(pendings[0].runId).toBe('run_2');
  });

  it('run_started clears pending interactions for the run (resume replay rebuilds them)', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    dispatchSSE('ses_1', 'run_started', { run_id: 'run_1' });
    // Cleared so the replayed stream can rebuild from scratch (deduplication handles re-delivery).
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(0);
  });

  it('pending_confirmation routes to the correct session (not the active one)', () => {
    dispatchSSE('ses_OTHER', 'pending_confirmation', {
      run_id: 'run_1', tool_call_id: 'tc_01', name: 'Read', args: '{}',
    });
    expect(useChatStore.getState().pendingInteractions['ses_OTHER']).toHaveLength(1);
    // Active session (ses_1) must be untouched.
    expect(useChatStore.getState().pendingInteractions['ses_1']).toBeUndefined();
  });
});

describe('applyPendingInteractions (mount rehydration)', () => {
  beforeEach(() => {
    useChatStore.setState({
      pendingInteractions: {},
      acknowledgedPendingInteractions: {},
    });
  });

  it('rehydrates a confirm item into a pending confirm card', () => {
    const items: PendingInteractionItem[] = [
      {
        run_id: 'run_1',
        kind: 'confirm',
        tool_calls: [{ id: 'tc_bash', name: 'Bash', args: '{"cmd":"ls"}' }],
        question: null,
        paused_at: '2026-01-01T00:00:00+00:00',
      },
    ];
    applyPendingInteractions('ses_1', items);

    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].kind).toBe('confirm');
    expect(pendings[0].runId).toBe('run_1');
    expect(pendings[0].toolCallId).toBe('tc_bash');
    expect(pendings[0].name).toBe('Bash');
    expect(pendings[0].args).toBe('{"cmd":"ls"}');
  });

  it('rehydrates a question item into a pending question card', () => {
    const items: PendingInteractionItem[] = [
      {
        run_id: 'run_2',
        kind: 'question',
        tool_calls: [{ id: 'tc_q' }],
        question: 'Which file?',
        paused_at: null,
      },
    ];
    applyPendingInteractions('ses_1', items);

    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].kind).toBe('question');
    expect(pendings[0].toolCallId).toBe('tc_q');
    expect(pendings[0].question).toBe('Which file?');
  });

  it('is dedup-safe alongside an SSE pending_confirmation for the same tool call', () => {
    // SSE delivers the card first…
    handleSSEEvent('ses_1', 'pending_confirmation', {
      run_id: 'run_1', tool_call_id: 'tc_bash', name: 'Bash', args: '{}',
    }, () => {});
    // …then mount rehydration runs with the same tool call.
    applyPendingInteractions('ses_1', [
      { run_id: 'run_1', kind: 'confirm', tool_calls: [{ id: 'tc_bash', name: 'Bash', args: '{}' }] },
    ]);
    // No duplicate card.
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);
  });

  it('does not resurrect an acknowledged interaction', () => {
    useChatStore.getState().markPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_bash');
    applyPendingInteractions('ses_1', [
      { run_id: 'run_1', kind: 'confirm', tool_calls: [{ id: 'tc_bash', name: 'Bash', args: '{}' }] },
    ]);
    expect(useChatStore.getState().pendingInteractions['ses_1'] ?? []).toHaveLength(0);
  });
});

// ─── run_complete one-shot durable-row reconcile ─────────────────────────────
//
// On every run_complete the handler fires a fire-and-forget api.messages.list
// re-pull and feeds each row back through addMessage (id-dedup + sequence
// order) — the final backstop for any durable row lost mid-stream.
describe('run_complete durable-row reconcile', () => {
  const listMock = api.messages.list as unknown as ReturnType<typeof vi.fn>;

  beforeEach(() => {
    listMock.mockReset();
    listMock.mockResolvedValue({ items: [], next_cursor: null });
    useChatStore.setState({
      sessions: [],
      activeSessionId: 'ses_rc',
      messages: {},
      streamOffsets: {},
      closedStreamRuns: {},
      isStreaming: false,
      isLoggedIn: true,
      userProfile: null,
      bootstrapped: true,
      pendingInteractions: {},
      acknowledgedPendingInteractions: {},
    });
  });

  const msg = (over: Partial<Message>): Message => ({
    id: 'x',
    session_id: 'ses_rc',
    role: 'ASSISTANT',
    content: '',
    tool_calls: null,
    run_id: 'run_1',
    sequence: 1,
    created_at: '2026-01-01',
    ...over,
  });

  it('re-pulls messages and backfills a row missing from the stream', async () => {
    // The stream delivered only the user row; the assistant row was lost.
    useChatStore.getState().addMessage('ses_rc', msg({
      id: 'user_1', role: 'USER', content: 'hi', run_id: null, sequence: 1,
    }));
    listMock.mockResolvedValue({
      items: [
        msg({ id: 'user_1', role: 'USER', content: 'hi', run_id: null, sequence: 1 }),
        msg({ id: 'assistant_1', role: 'ASSISTANT', content: 'the lost answer', sequence: 2 }),
      ],
      next_cursor: null,
    });

    dispatchSSE('ses_rc', 'run_complete', { run_id: 'run_1' });
    // Flush the detached async IIFE (fire-and-forget) — one turn for the awaited
    // promise, plus a microtask drain.
    await Promise.resolve();
    await Promise.resolve();

    expect(listMock).toHaveBeenCalledTimes(1);
    expect(listMock).toHaveBeenCalledWith('ses_rc');
    const msgs = useChatStore.getState().messages['ses_rc'];
    expect(msgs.some((m) => m.id === 'assistant_1' && m.content === 'the lost answer')).toBe(true);
  });

  it('is idempotent — re-pulled rows already present do not duplicate', async () => {
    useChatStore.getState().addMessage('ses_rc', msg({ id: 'assistant_1', content: 'answer', sequence: 2 }));
    listMock.mockResolvedValue({
      items: [msg({ id: 'assistant_1', content: 'answer', sequence: 2 })],
      next_cursor: null,
    });

    dispatchSSE('ses_rc', 'run_complete', { run_id: 'run_1' });
    await Promise.resolve();
    await Promise.resolve();

    const msgs = useChatStore.getState().messages['ses_rc'];
    expect(msgs.filter((m) => m.id === 'assistant_1')).toHaveLength(1);
  });

  it('reconcile failure warns but does not throw', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    listMock.mockRejectedValue(new Error('network down'));

    // Must not throw synchronously (handler is fire-and-forget).
    expect(() => dispatchSSE('ses_rc', 'run_complete', { run_id: 'run_1' })).not.toThrow();
    await Promise.resolve();
    await Promise.resolve();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('[useChat] run_complete reconcile failed'),
      expect.anything(),
    );
    warnSpy.mockRestore();
  });
});

// ─── parseSSEStream: handler errors are surfaced, not swallowed ──────────────
//
// A throw from inside a handler used to be caught by the same try that guarded
// JSON.parse, so a real bug looked like a "non-JSON frame" and vanished.  The
// two try blocks are now split: parse failures stay quiet, handler throws are
// logged via console.error AND must not abort the read loop.
describe('parseSSEStream — handler error isolation', () => {
  it('a throwing handler is logged and the read loop keeps processing later events', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const seen: Array<[string, Record<string, unknown>]> = [];

    await parseSSEStream(
      readerFromChunks([
        'event: token\ndata: {"n":1}\n\n',
        'event: token\ndata: {"n":2}\n\n',
        'event: token\ndata: {"n":3}\n\n',
      ]),
      (event, data) => {
        // Blow up on the middle event only.
        if (data.n === 2) throw new Error('handler boom');
        seen.push([event, data]);
      },
    );

    // Events before AND after the throwing one were both processed — one bad
    // frame did not tear down the stream.
    expect(seen).toEqual([
      ['token', { n: 1 }],
      ['token', { n: 3 }],
    ]);
    expect(errSpy).toHaveBeenCalledWith(
      '[useChat] SSE handler error for',
      'token',
      expect.any(Error),
    );
    errSpy.mockRestore();
  });

  it('a non-JSON data frame is still skipped silently (no console.error)', async () => {
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const seen: Array<[string, Record<string, unknown>]> = [];

    await parseSSEStream(
      readerFromChunks([
        'event: token\ndata: not-json-at-all\n\n',
        'event: token\ndata: {"ok":true}\n\n',
      ]),
      (event, data) => seen.push([event, data]),
    );

    // Bad frame dropped quietly; the good one still dispatched.
    expect(seen).toEqual([['token', { ok: true }]]);
    expect(errSpy).not.toHaveBeenCalled();
    errSpy.mockRestore();
  });
});

// ─── Silent-stall watchdog predicate ─────────────────────────────────────────
//
// runConnectLoop arms a watchdog only once a `ping` has been seen, then aborts
// the attempt (forcing a reconnect) if no SSE event arrives for
// WATCHDOG_STALL_MS.  The decision is extracted into shouldWatchdogFire so it is
// testable without standing up a fetch/reader.
describe('shouldWatchdogFire', () => {
  it('never fires before any ping has been seen (old backend, legit long silence)', () => {
    const now = 1_000_000;
    // 36s of silence — legitimate on a pre-heartbeat backend between tool batches.
    expect(shouldWatchdogFire(false, now - 36_000, now)).toBe(false);
  });

  it('does not fire when a ping was seen but the gap is under the threshold', () => {
    const now = 1_000_000;
    expect(shouldWatchdogFire(true, now - 5_000, now)).toBe(false);
    // Exactly at the threshold is not "greater than" → still no fire.
    expect(shouldWatchdogFire(true, now - 30_000, now)).toBe(false);
  });

  it('fires when a ping was seen and the gap exceeds the threshold', () => {
    const now = 1_000_000;
    expect(shouldWatchdogFire(true, now - 30_001, now)).toBe(true);
    expect(shouldWatchdogFire(true, now - 60_000, now)).toBe(true);
  });
});

// ─── Silent-stall watchdog end-to-end (fake timers) ──────────────────────────
//
// Drive runConnectLoop (via subscribeSessionStream) with a stream that emits a
// ping and then goes silent forever.  After the stall window elapses the
// watchdog must abort THAT attempt's signal and the loop must reconnect (a
// second fetch), all without the master unsubscribe having been called.
describe('silent-stall watchdog (integration)', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  const signals: AbortSignal[] = [];

  beforeEach(() => {
    vi.useFakeTimers();
    signals.length = 0;
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('aborts the stalled attempt after a ping and reconnects', async () => {
    // First fetch: a body that yields one ping frame then stays open (silent)
    // forever.  Second fetch onward: park pending so we can count reconnects.
    let call = 0;
    fetchMock = vi.fn((_url: string, init: { signal: AbortSignal }) => {
      signals.push(init.signal);
      call += 1;
      if (call === 1) {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new TextEncoder().encode('event: ping\ndata: {}\n\n'));
            // Never close and never enqueue again → the tail is silent.
          },
        });
        return Promise.resolve({ ok: true, body } as unknown as Response);
      }
      return new Promise(() => {}); // subsequent attempts park pending
    });
    vi.stubGlobal('fetch', fetchMock);

    const unsub = subscribeSessionStream('ses_wd');

    // Let the first fetch resolve and the ping frame be read/parsed.
    await vi.advanceTimersByTimeAsync(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(signals[0].aborted).toBe(false);

    // Before the stall window elapses the attempt is still live — the watchdog
    // must NOT fire early.
    await vi.advanceTimersByTimeAsync(29_000);
    expect(signals[0].aborted).toBe(false);

    // Advance past the stall window.  The 5s watchdog interval whose tick lands
    // >30s after the ping aborts the first attempt; reader.cancel() then unblocks
    // the read loop and the loop reconnects (backoff reset to 1s).
    await vi.advanceTimersByTimeAsync(8_000); // total ~37s since the ping
    expect(signals[0].aborted).toBe(true); // stalled attempt aborted
    await vi.advanceTimersByTimeAsync(1_000); // clear the 1s reconnect backoff
    expect(fetchMock).toHaveBeenCalledTimes(2); // reconnected
    // The master controller was never torn down: the second attempt is live.
    expect(signals[1].aborted).toBe(false);

    unsub();
    await vi.advanceTimersByTimeAsync(100); // past teardown grace
    expect(signals[1].aborted).toBe(true); // real teardown aborts the live attempt
  });
});
