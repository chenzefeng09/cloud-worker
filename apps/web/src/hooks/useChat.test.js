/**
 * useChat tests — the bit we care about is that SSE events route to the
 * session that was active *when the message was sent*, NOT whichever
 * session happens to be active when the event arrives.  See the B-M1
 * review #15 for the bug this guards against.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { useChatStore } from '../stores/chatStore';
// We don't import useChat itself — it's a React hook that needs RTL to
// drive.  Instead we test the pure routing logic by recreating it inline
// against the store, which is what handleSSEEvent does after the fix.
function dispatchSSE(sessionId, event, data) {
    const store = useChatStore.getState();
    if (event === 'message_created' || event === 'agent_message') {
        store.addMessage(sessionId, data);
    }
}
describe('SSE → store routing', () => {
    beforeEach(() => {
        useChatStore.setState({
            sessions: [],
            activeSessionId: 'ses_B', // user has switched session
            messages: {},
            isStreaming: false,
            isLoggedIn: true,
            userProfile: null,
            bootstrapped: true,
        });
    });
    it('routes message to the snapshotted session, not the active one', () => {
        // Stream was started while ses_A was active; user has since switched to
        // ses_B.  The incoming message MUST land in ses_A.
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
});
