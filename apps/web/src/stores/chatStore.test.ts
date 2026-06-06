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
  isStreaming: false,
  isLoggedIn: false,
  userProfile: null,
  bootstrapped: false,
  selectedSkills: {},
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

  it('appendStreamingThinking seals text block and adds thinking block', () => {
    useChatStore.getState().appendStreamingToken('ses_1', 'run_1', 'text before');
    useChatStore.getState().appendStreamingThinking('ses_1', 'run_1', 'I should plan this');

    const msgs = useChatStore.getState().messages['ses_1'];
    expect(msgs).toHaveLength(2);
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].streaming).toBe(false); // sealed
    expect(msgs[1].blockKind).toBe('thinking');
    expect(msgs[1].id).toBe('thinking_run_1_0');
    expect(msgs[1].content).toBe('I should plan this');
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
});
