/**
 * chatStore tests — Zustand actions stay consistent.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from './chatStore';
import type { Message, ToolCallEntry, UserProfile } from '../api/types';

const PROFILE: UserProfile = {
  id: 'usr_test',
  email: 'test@example.com',
  display_name: 'Test',
  tenant_id: 'tnt_test',
  roles: [],
};

const MESSAGE: Message = {
  id: 'msg_1',
  session_id: 'ses_1',
  role: 'USER',
  content: 'hello',
  tool_calls: null,
  run_id: null,
  sequence: 1,
  created_at: '2026-01-01T00:00:00Z',
};

const RESET_STATE = {
  sessions: [],
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
};

describe('chatStore', () => {
  beforeEach(() => {
    useChatStore.setState(RESET_STATE);
  });

  it('login sets profile + bootstrapped', () => {
    useChatStore.getState().login(PROFILE);
    const s = useChatStore.getState();
    expect(s.isLoggedIn).toBe(true);
    expect(s.userProfile).toEqual(PROFILE);
    expect(s.bootstrapped).toBe(true);
  });

  it('logout clears session data', () => {
    useChatStore.getState().login(PROFILE);
    useChatStore.getState().setSessions([
      {
        id: 'ses_1',
        tenant_id: 't',
        user_id: 'u',
        title: null,
        status: 'ACTIVE',
        current_run_id: null,
        created_at: '',
        updated_at: '',
      },
    ]);
    useChatStore.getState().logout();
    const s = useChatStore.getState();
    expect(s.isLoggedIn).toBe(false);
    expect(s.userProfile).toBeNull();
    expect(s.sessions).toEqual([]);
    expect(s.bootstrapped).toBe(true);
  });

  it('addMessage appends per-session', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().addMessage('ses_1', { ...MESSAGE, id: 'msg_2' });
    useChatStore.getState().addMessage('ses_2', { ...MESSAGE, id: 'msg_3' });
    const s = useChatStore.getState();
    expect(s.messages['ses_1']).toHaveLength(2);
    expect(s.messages['ses_2']).toHaveLength(1);
  });

  it('updateLastMessage rewrites last entry content', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().updateLastMessage('ses_1', 'world');
    expect(useChatStore.getState().messages['ses_1'][0].content).toBe('world');
  });

  it('updateLastMessage is a no-op on empty session', () => {
    useChatStore.getState().updateLastMessage('ses_1', 'world');
    expect(useChatStore.getState().messages['ses_1']).toBeUndefined();
  });

  it('setBootstrapped flips the flag without touching auth', () => {
    useChatStore.getState().setBootstrapped(true);
    const s = useChatStore.getState();
    expect(s.bootstrapped).toBe(true);
    expect(s.isLoggedIn).toBe(false);
  });

  // ─── Per-session skill selection (context rail toggle) ──────────────────────

  it('toggleSkill adds, removes, and isolates per session', () => {
    const { toggleSkill } = useChatStore.getState();
    toggleSkill('ses_1', 'pdf');
    toggleSkill('ses_1', 'xlsx');
    expect(useChatStore.getState().selectedSkills['ses_1']).toEqual(['pdf', 'xlsx']);

    // Toggling an enabled skill removes it.
    toggleSkill('ses_1', 'pdf');
    expect(useChatStore.getState().selectedSkills['ses_1']).toEqual(['xlsx']);

    // Other sessions are unaffected.
    toggleSkill('ses_2', 'pdf');
    expect(useChatStore.getState().selectedSkills['ses_2']).toEqual(['pdf']);
    expect(useChatStore.getState().selectedSkills['ses_1']).toEqual(['xlsx']);
  });

  it('logout clears selectedSkills', () => {
    useChatStore.getState().toggleSkill('ses_1', 'pdf');
    useChatStore.getState().logout();
    expect(useChatStore.getState().selectedSkills).toEqual({});
  });

  it('no longer exposes a per-session execution mode (sandbox worker only)', () => {
    const state = useChatStore.getState() as unknown as Record<string, unknown>;
    expect('executionMode' in state).toBe(false);
    expect('setSessionExecutionMode' in state).toBe(false);
  });

  // ─── Optimistic USER message replacement ────────────────────────────────────

  it('addMessage replaces temp_user_ entry with real server message', () => {
    const tempMsg: Message = {
      id: 'temp_user_1234',
      session_id: 'ses_1',
      role: 'USER',
      content: 'hello server',
      tool_calls: null,
      run_id: null,
      sequence: 0,
      created_at: '2026-01-01T00:00:00Z',
    };
    useChatStore.getState().addMessage('ses_1', tempMsg);

    const realMsg: Message = {
      id: 'msg_real_1',
      session_id: 'ses_1',
      role: 'USER',
      content: 'hello server',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', realMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('msg_real_1');
  });

  it('addMessage does not replace if content differs', () => {
    const tempMsg: Message = {
      id: 'temp_user_9999',
      session_id: 'ses_1',
      role: 'USER',
      content: 'original text',
      tool_calls: null,
      run_id: null,
      sequence: 0,
      created_at: '2026-01-01T00:00:00Z',
    };
    useChatStore.getState().addMessage('ses_1', tempMsg);

    const differentMsg: Message = {
      id: 'msg_diff',
      session_id: 'ses_1',
      role: 'USER',
      content: 'different text',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', differentMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
  });

  // ─── Ordered insertion by sequence (timeline ordering) ──────────────────────

  it('addMessage inserts a real lower-sequence message before a higher one', () => {
    // Race: assistant (seq=3) lands first, then the real user row (seq=1).
    const assistant: Message = {
      id: 'msg_asst',
      session_id: 'ses_1',
      role: 'ASSISTANT',
      content: 'an answer',
      tool_calls: null,
      run_id: 'run_1',
      sequence: 3,
      created_at: '2026-01-01T00:00:03Z',
    };
    const user: Message = {
      id: 'msg_user',
      session_id: 'ses_1',
      role: 'USER',
      content: 'a question',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', assistant);
    useChatStore.getState().addMessage('ses_1', user);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    // USER must render above the ASSISTANT despite arriving second.
    expect(msgs[0].id).toBe('msg_user');
    expect(msgs[1].id).toBe('msg_asst');
  });

  it('addMessage inserts a real message ahead of sequence===0 streaming blocks', () => {
    // A streaming assistant block (sequence===0) is already present when the
    // real user row (sequence=1) arrives — the user row must jump ahead of it.
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'streaming...');
    const user: Message = {
      id: 'msg_user',
      session_id: 'ses_1',
      role: 'USER',
      content: 'hello',
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', user);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].id).toBe('msg_user');
    expect(msgs[1].id).toBe('stream_run_1_0');
  });

  it('addMessage keeps real messages in strict sequence order', () => {
    const mk = (seq: number, id: string): Message => ({
      id,
      session_id: 'ses_1',
      role: 'ASSISTANT',
      content: id,
      tool_calls: null,
      run_id: null,
      sequence: seq,
      created_at: '2026-01-01T00:00:00Z',
    });
    // Arrive out of order: 2, 1, 3.
    useChatStore.getState().addMessage('ses_1', mk(2, 'b'));
    useChatStore.getState().addMessage('ses_1', mk(1, 'a'));
    useChatStore.getState().addMessage('ses_1', mk(3, 'c'));

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs.map((m) => m.id)).toEqual(['a', 'b', 'c']);
  });

  it('addMessage replaces temp_user_ even when content whitespace differs', () => {
    const tempMsg: Message = {
      id: 'temp_user_42',
      session_id: 'ses_1',
      role: 'USER',
      content: '你好 ', // trailing space (raw composer input)
      tool_calls: null,
      run_id: null,
      sequence: 0,
      created_at: '2026-01-01T00:00:00Z',
    };
    useChatStore.getState().addMessage('ses_1', tempMsg);

    const realMsg: Message = {
      id: 'msg_real_strip',
      session_id: 'ses_1',
      role: 'USER',
      content: '你好', // backend .strip()ed
      tool_calls: null,
      run_id: null,
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', realMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1); // replaced, not duplicated
    expect(msgs[0].id).toBe('msg_real_strip');
  });

  // ─── appendStreamingToken ────────────────────────────────────────────────────

  it('appendStreamingToken creates an open text block on first token', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_abc', 'Hello');
    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('stream_run_abc_0');
    expect(msgs[0].role).toBe('ASSISTANT');
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].streaming).toBe(true);
    expect(msgs[0].content).toBe('Hello');
    expect(msgs[0].streamRunId).toBe('run_abc');
  });

  it('appendStreamingToken accumulates subsequent tokens in the same block', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_abc', 'Hello');
    useChatStore.getState().appendStreamingToken('ses_1', 'run_abc', ', ');
    useChatStore.getState().appendStreamingToken('ses_1', 'run_abc', 'world');
    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('Hello, world');
  });

  it('appendStreamingToken keeps other messages intact', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().appendStreamingToken('ses_1', 'run_abc', 'Hi');
    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].id).toBe('msg_1');
    expect(msgs[1].id).toBe('stream_run_abc_0');
  });

  // ─── Ordered block construction: token → tool → token ────────────────────────

  it('upsertStreamingTool (kind=call) seals the open text block then creates a tool block', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'Let me check');
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'call',
      tool_call_id: 'tc_1',
      name: 'Read',
      args: JSON.stringify({ file_path: '/foo.ts' }),
    });
    const msgs = useChatStore.getState().messages['ses_1'];
    // text block (sealed) + tool block
    expect(msgs).toHaveLength(2);
    expect(msgs[0].id).toBe('stream_run_1_0');
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].streaming).toBe(false); // sealed
    expect(msgs[1].id).toBe('tool_run_1_tc_1');
    expect(msgs[1].blockKind).toBe('tool');
    expect(msgs[1].role).toBe('TOOL');
  });

  it('token after tool creates a NEW text block (strict time-order)', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'Before tool');
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'call',
      tool_call_id: 'tc_1',
      name: 'Read',
      args: '{}',
    });
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'result',
      tool_call_id: 'tc_1',
      name: 'Read',
      result: 'file content',
      state: 'success',
    });
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'After tool');

    const msgs = useChatStore.getState().messages['ses_1'];
    // stream_run_1_0 (sealed text) | tool_run_1_tc_1 (tool) | stream_run_1_1 (new text)
    expect(msgs).toHaveLength(3);
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].streaming).toBe(false);
    expect(msgs[0].content).toBe('Before tool');
    expect(msgs[1].blockKind).toBe('tool');
    expect(msgs[2].blockKind).toBe('text');
    expect(msgs[2].streaming).toBe(true);
    expect(msgs[2].content).toBe('After tool');
    expect(msgs[2].id).toBe('stream_run_1_1');
  });

  it('upsertStreamingTool kind=result merges result and state', () => {
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_t1', {
      kind: 'call',
      tool_call_id: 'tc_1',
      name: 'Read',
      args: JSON.stringify({ file_path: '/a.ts' }),
    });
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_t1', {
      kind: 'result',
      tool_call_id: 'tc_1',
      name: 'Read',
      result: 'line1\nline2',
      state: 'success',
    });
    const msgs = useChatStore.getState().messages['ses_1'];
    const calls = msgs[0].tool_calls as ToolCallEntry[];
    expect(calls[0].result).toBe('line1\nline2');
    expect(calls[0].state).toBe('success');
    expect(msgs[0].streaming).toBe(false); // marked complete after result
  });

  // ─── appendStreamingThinking ─────────────────────────────────────────────────

  it('appendStreamingThinking keeps the answer text open and places thinking before it', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'text before');
    useChatStore.getState().appendStreamingThinking('ses_1', 'run_1', 'I should plan this');
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', ' text after');

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].blockKind).toBe('thinking');
    expect(msgs[0].id).toBe('thinking_run_1_0');
    expect(msgs[0].content).toBe('I should plan this');
    expect(msgs[1].blockKind).toBe('text');
    expect(msgs[1].streaming).toBe(true);
    expect(msgs[1].content).toBe('text before text after');
  });

  // ─── Idempotent streaming by Redis offset (replay / reconnect safety) ────────
  //
  // A fresh SSE connection always replays the run from "0-0", so without
  // dedup a reconnect/StrictMode-remount re-appends already-seen deltas onto
  // whatever text block is currently open — scrambling and partly losing the
  // streamed reply.  Each event carries the Redis entry id as ``offset``; the
  // store applies it only when strictly newer than the last applied for the run.

  it('appendStreamingToken drops a replayed (already-applied) offset', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_1', 'run_1', 'A', '1-0');
    st().appendStreamingToken('ses_1', 'run_1', 'B', '2-0');
    // Replay the same two deltas from the start (new connection).
    st().appendStreamingToken('ses_1', 'run_1', 'A', '1-0');
    st().appendStreamingToken('ses_1', 'run_1', 'B', '2-0');
    const msgs = st().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('AB'); // not "ABAB"
  });

  it('appendStreamingToken applies a strictly-newer offset after replay', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_1', 'run_1', 'A', '1-0');
    st().appendStreamingToken('ses_1', 'run_1', 'A', '1-0'); // replayed → dropped
    st().appendStreamingToken('ses_1', 'run_1', 'B', '2-0'); // new → applied
    expect(st().messages['ses_1'][0].content).toBe('AB');
  });

  it('offset comparison is numeric, not lexical (10-0 > 9-0)', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_1', 'run_1', 'x', '9-0');
    // "10-0" is lexically < "9-0" but numerically greater — must be applied.
    st().appendStreamingToken('ses_1', 'run_1', 'y', '10-0');
    expect(st().messages['ses_1'][0].content).toBe('xy');
  });

  it('replayed thinking event is not duplicated and does not re-split text', () => {
    const st = () => useChatStore.getState();
    // Live: text starts, thinking arrives, then more text. Thinking must not
    // split the visible answer into "before" / "after" blocks.
    st().appendStreamingToken('ses_1', 'run_1', 'before', '1-0');
    st().appendStreamingThinking('ses_1', 'run_1', 'plan', '2-0');
    st().appendStreamingToken('ses_1', 'run_1', 'after', '3-0');
    // Reconnect: replay from the start.
    st().appendStreamingToken('ses_1', 'run_1', 'before', '1-0');
    st().appendStreamingThinking('ses_1', 'run_1', 'plan', '2-0');
    st().appendStreamingToken('ses_1', 'run_1', 'after', '3-0');

    const msgs = st().messages['ses_1'];
    const text = msgs.filter((m) => m.blockKind === 'text').map((m) => m.content);
    const thinking = msgs.filter((m) => m.blockKind === 'thinking');
    expect(thinking).toHaveLength(1); // not duplicated
    expect(text).toEqual(['beforeafter']); // not split/scrambled/duplicated
  });

  it('offset dedup is per-run (a different run is unaffected)', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_1', 'run_a', 'A', '5-0');
    // run_b at offset "1-0" is lower than run_a's "5-0" but belongs to a
    // different run, so it must still be applied.
    st().appendStreamingToken('ses_1', 'run_b', 'B', '1-0');
    const contents = st().messages['ses_1'].map((m) => m.content);
    expect(contents).toContain('A');
    expect(contents).toContain('B');
  });

  it('replayed tool call/result is idempotent (one tool block, merged result)', () => {
    const st = () => useChatStore.getState();
    st().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'call', tool_call_id: 'tc1', name: 'Read', args: '{}',
    }, '1-0');
    st().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'result', tool_call_id: 'tc1', name: 'Read', result: 'ok', state: 'success',
    }, '2-0');
    // Replay both.
    st().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'call', tool_call_id: 'tc1', name: 'Read', args: '{}',
    }, '1-0');
    st().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'result', tool_call_id: 'tc1', name: 'Read', result: 'ok', state: 'success',
    }, '2-0');

    const tools = st().messages['ses_1'].filter((m) => m.blockKind === 'tool');
    expect(tools).toHaveLength(1);
    expect(tools[0].tool_calls?.[0].result).toBe('ok');
  });

  it('offset-less calls are always applied (legacy/test path)', () => {
    const st = () => useChatStore.getState();
    st().appendStreamingToken('ses_1', 'run_1', 'A');
    st().appendStreamingToken('ses_1', 'run_1', 'B');
    expect(st().messages['ses_1'][0].content).toBe('AB');
  });

  // ─── clearStreamingBlocks ────────────────────────────────────────────────────

  it('clearStreamingBlocks removes all temp blocks for the run', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'text');
    useChatStore.getState().upsertStreamingTool('ses_1', 'run_1', {
      kind: 'call',
      tool_call_id: 'tc_1',
      name: 'Read',
      args: '{}',
    });
    // Also add a different session's message (must survive)
    useChatStore.getState().addMessage('ses_1', MESSAGE);

    useChatStore.getState().clearStreamingBlocks('ses_1', 'run_1');

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].id).toBe('msg_1');
  });

  it('clearStreamingBlocks is a no-op when no temp blocks exist', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().clearStreamingBlocks('ses_1', 'run_ghost');
    expect(useChatStore.getState().messages['ses_1']).toHaveLength(1);
  });

  it('clearStreamingBlocks with kind only removes blocks of that kind', () => {
    // Streamed text + streamed thinking for the same run.
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'answer text');
    useChatStore.getState().appendStreamingThinking('ses_1', 'run_1', 'reasoning');

    // A durable THINKING row arriving must NOT wipe the live-streamed text.
    useChatStore.getState().clearStreamingBlocks('ses_1', 'run_1', 'thinking');

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].content).toBe('answer text');
  });

  // ─── finalizeStreaming (legacy compat) ────────────────────────────────────────

  it('finalizeStreaming clears temp blocks and appends the final message', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().appendStreamingToken('ses_1', 'run_xyz', 'Hi');
    useChatStore.getState().appendStreamingToken('ses_1', 'run_xyz', '!');

    const finalMsg: Message = {
      id: 'msg_final',
      session_id: 'ses_1',
      role: 'ASSISTANT',
      content: 'Hi!',
      tool_calls: null,
      run_id: 'run_xyz',
      sequence: 2,
      created_at: '2026-01-01T00:00:02Z',
    };
    useChatStore.getState().finalizeStreaming('ses_1', 'run_xyz', finalMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    // USER + final ASSISTANT
    expect(msgs).toHaveLength(2);
    expect(msgs[1].id).toBe('msg_final');
    expect(msgs[1].streaming).toBe(false);
    expect(msgs[0].id).toBe('msg_1');
  });

  it('finalizeStreaming appends if no temp blocks exist', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);

    const finalMsg: Message = {
      id: 'msg_no_placeholder',
      session_id: 'ses_1',
      role: 'ASSISTANT',
      content: 'Surprise!',
      tool_calls: null,
      run_id: 'run_zzz',
      sequence: 2,
      created_at: '2026-01-01T00:00:02Z',
    };
    useChatStore.getState().finalizeStreaming('ses_1', 'run_zzz', finalMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    expect(msgs[1].id).toBe('msg_no_placeholder');
  });

  it('finalizeStreaming does not duplicate if real id already present', () => {
    const finalMsg: Message = {
      id: 'msg_dup',
      session_id: 'ses_1',
      role: 'ASSISTANT',
      content: 'Once',
      tool_calls: null,
      run_id: 'run_dup',
      sequence: 1,
      created_at: '2026-01-01T00:00:01Z',
    };
    useChatStore.getState().addMessage('ses_1', finalMsg);
    useChatStore.getState().finalizeStreaming('ses_1', 'run_dup', finalMsg);

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(1);
  });

  // ─── removeToolingPlaceholder (legacy compat) ─────────────────────────────────

  it('removeToolingPlaceholder removes legacy tooling_ entry', () => {
    // Manually insert a legacy tooling placeholder.
    useChatStore.getState().addMessage('ses_1', {
      ...MESSAGE,
      id: 'tooling_run_t2',
      role: 'TOOL',
    });
    useChatStore.getState().removeToolingPlaceholder('ses_1', 'run_t2');
    expect(useChatStore.getState().messages['ses_1']).toHaveLength(0);
  });

  it('removeToolingPlaceholder is a no-op when placeholder does not exist', () => {
    useChatStore.getState().addMessage('ses_1', MESSAGE);
    useChatStore.getState().removeToolingPlaceholder('ses_1', 'run_ghost');
    expect(useChatStore.getState().messages['ses_1']).toHaveLength(1);
  });

  // ─── HITL — pendingInteractions ────────────────────────────────────────────────

  it('addPendingInteraction appends a new pending interaction', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1',
      toolCallId: 'tc_01',
      kind: 'confirm',
      name: 'Read',
      args: '{"file_path": "/foo.ts"}',
    });
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].toolCallId).toBe('tc_01');
    expect(pendings[0].kind).toBe('confirm');
  });

  it('addPendingInteraction deduplicates by toolCallId', () => {
    const p = { runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm' as const };
    useChatStore.getState().addPendingInteraction('ses_1', p);
    useChatStore.getState().addPendingInteraction('ses_1', p); // duplicate
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);
  });

  it('addPendingInteraction ignores already acknowledged interactions', () => {
    const p = { runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm' as const };
    useChatStore.getState().markPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_01');
    useChatStore.getState().addPendingInteraction('ses_1', p);
    expect(useChatStore.getState().pendingInteractions['ses_1']).toBeUndefined();
  });

  it('unmarkPendingInteractionAcknowledged allows a failed submit to be retried', () => {
    const p = { runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm' as const };
    useChatStore.getState().markPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_01');
    useChatStore.getState().addPendingInteraction('ses_1', p);
    expect(useChatStore.getState().pendingInteractions['ses_1']).toBeUndefined();

    useChatStore.getState().unmarkPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_01');
    useChatStore.getState().addPendingInteraction('ses_1', p);
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);
  });

  it('addPendingInteraction isolates per session', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_2', {
      runId: 'run_2', toolCallId: 'tc_02', kind: 'question',
    });
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);
    expect(useChatStore.getState().pendingInteractions['ses_2']).toHaveLength(1);
  });

  it('removePendingInteraction removes by toolCallId', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_02', kind: 'question',
    });
    useChatStore.getState().removePendingInteraction('ses_1', 'tc_01');
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].toolCallId).toBe('tc_02');
  });

  it('removePendingInteraction is a no-op if toolCallId not found', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().removePendingInteraction('ses_1', 'tc_ghost');
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(1);
  });

  it('clearPendingInteractions clears all for a session', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_02', kind: 'question',
    });
    useChatStore.getState().clearPendingInteractions('ses_1');
    expect(useChatStore.getState().pendingInteractions['ses_1']).toHaveLength(0);
  });

  it('clearPendingInteractions with runId only removes that run\'s pendings', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_2', toolCallId: 'tc_02', kind: 'question',
    });
    useChatStore.getState().clearPendingInteractions('ses_1', 'run_1');
    const pendings = useChatStore.getState().pendingInteractions['ses_1'];
    expect(pendings).toHaveLength(1);
    expect(pendings[0].runId).toBe('run_2');
  });

  it('logout clears pendingInteractions', () => {
    useChatStore.getState().addPendingInteraction('ses_1', {
      runId: 'run_1', toolCallId: 'tc_01', kind: 'confirm',
    });
    useChatStore.getState().markPendingInteractionAcknowledged('ses_1', 'run_1', 'tc_01');
    useChatStore.getState().logout();
    expect(useChatStore.getState().pendingInteractions).toEqual({});
    expect(useChatStore.getState().acknowledgedPendingInteractions).toEqual({});
  });
});
