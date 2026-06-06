/**
 * useChat tests — SSE events route to the correct session and build ordered
 * blocks in the store.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from '../stores/chatStore';
import type { Message, ToolCallEntry } from '../api/types';

// Recreate the handleSSEEvent routing logic inline so we can test it without
// a React / RTL environment.
function dispatchSSE(sessionId: string, event: string, data: Record<string, unknown>) {
  const store = useChatStore.getState();

  if (event === 'message_created' || event === 'agent_message') {
    store.addMessage(sessionId, data as unknown as Message);
  } else if (event === 'token') {
    const runId = data.run_id as string | undefined;
    const delta = data.delta as string | undefined;
    if (runId && delta !== undefined) {
      store.appendStreamingToken(sessionId, runId, delta);
    }
  } else if (event === 'tool') {
    const runId = data.run_id as string | undefined;
    const kind = data.kind as 'call' | 'result' | undefined;
    const toolCallId = data.tool_call_id as string | undefined;
    const toolName = data.name as string | undefined;
    if (runId && kind && toolCallId && toolName) {
      store.upsertStreamingTool(sessionId, runId, {
        kind,
        tool_call_id: toolCallId,
        name: toolName,
        args: kind === 'call' ? (data.args as string | undefined) : undefined,
        result: kind === 'result' ? (data.result as string | undefined) : undefined,
        state: kind === 'result' ? (data.state as string | undefined) : undefined,
      });
    }
  } else if (event === 'thinking') {
    const runId = data.run_id as string | undefined;
    const text = data.text as string | undefined;
    if (runId && text) {
      store.appendStreamingThinking(sessionId, runId, text);
    }
  } else if (event === 'message') {
    const msg = data as unknown as Message;
    const roleUpper = (msg.role || '').toUpperCase();
    if ((roleUpper === 'ASSISTANT' || roleUpper === 'TOOL') && msg.run_id) {
      store.clearStreamingBlocks(sessionId, msg.run_id);
      store.addMessage(sessionId, msg);
    } else {
      store.addMessage(sessionId, msg);
    }
  }
}

describe('SSE → store routing', () => {
  beforeEach(() => {
    useChatStore.setState({
      sessions: [],
      activeSessionId: 'ses_B',
      messages: {},
      isStreaming: false,
      isLoggedIn: true,
      userProfile: null,
      bootstrapped: true,
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

  it('thinking event creates a thinking block and seals preceding text', () => {
    dispatchSSE('ses_A', 'token', { run_id: 'run_3', delta: 'intro' });
    dispatchSSE('ses_A', 'thinking', { run_id: 'run_3', text: 'plan the next step' });
    dispatchSSE('ses_A', 'token', { run_id: 'run_3', delta: 'result' });

    const msgs = useChatStore.getState().messages['ses_A'];
    expect(msgs).toHaveLength(3);
    expect(msgs[0].blockKind).toBe('text');
    expect(msgs[0].streaming).toBe(false);
    expect(msgs[1].blockKind).toBe('thinking');
    expect(msgs[1].content).toBe('plan the next step');
    expect(msgs[2].blockKind).toBe('text');
    expect(msgs[2].content).toBe('result');
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
});
