/**
 * chatStore tests — Zustand actions stay consistent.
 *
 * Most of the interesting logic is in the actions (login/logout/addMessage);
 * the assertions here pin the contract that components depend on.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from './chatStore';
import type { Message, UserProfile } from '../api/types';

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

describe('chatStore', () => {
  beforeEach(() => {
    useChatStore.setState({
      sessions: [],
      activeSessionId: null,
      messages: {},
      isStreaming: false,
      isLoggedIn: false,
      userProfile: null,
      bootstrapped: false,
    });
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
});
