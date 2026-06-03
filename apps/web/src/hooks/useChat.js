import { useCallback, useEffect, useRef } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api, ApiError } from '../api/client';
/**
 * SSE event handler bound to a specific sessionId.
 *
 * Why we capture the sessionId at send-time
 * -----------------------------------------
 * The previous implementation read `activeSessionId` from the store *inside*
 * the handler.  If the user switched to a different session while an SSE
 * stream was still running, every subsequent agent message went into the
 * newly-active session — i.e. messages from session A would appear in
 * session B's transcript.  We now snapshot the sessionId when `sendMessage`
 * is called and use that for every event the stream emits, regardless of
 * what the user does in the UI afterwards.
 */
function handleSSEEvent(sessionId, eventType, data) {
    const store = useChatStore.getState();
    switch (eventType) {
        case 'message_created':
        case 'agent_message':
            store.addMessage(sessionId, data);
            break;
        case 'tool_call':
            store.addMessage(sessionId, {
                id: `tool_${Date.now()}`,
                session_id: sessionId,
                role: 'TOOL',
                content: `Tool: ${data.name || 'unknown'}`,
                tool_calls: data,
                run_id: null,
                sequence: 0,
                created_at: new Date().toISOString(),
            });
            break;
        case 'run_complete':
            // Run finalisation handled by the run_complete event in the orchestrator
            // SSE stream; nothing to do client-side here right now.
            break;
        case 'error':
            store.addMessage(sessionId, {
                id: `err_${Date.now()}`,
                session_id: sessionId,
                role: 'SYSTEM',
                content: `Error: ${data.detail || 'Unknown error'}`,
                tool_calls: null,
                run_id: null,
                sequence: 0,
                created_at: new Date().toISOString(),
            });
            break;
    }
}
export function useChat() {
    const { activeSessionId, messages, setIsStreaming, isStreaming } = useChatStore();
    const abortRef = useRef(null);
    const loadedRef = useRef(new Set());
    // Hydrate transcript from server when the active session changes (page
    // refresh, deep-link, session switch).  Without this, refreshing /tasks/:id
    // shows an empty chat even though the messages are in the DB.
    useEffect(() => {
        if (!activeSessionId)
            return;
        if (loadedRef.current.has(activeSessionId))
            return;
        loadedRef.current.add(activeSessionId);
        const sid = activeSessionId;
        (async () => {
            try {
                const page = await api.messages.list(sid);
                const store = useChatStore.getState();
                for (const m of page.items) {
                    store.addMessage(sid, m);
                }
            }
            catch (err) {
                if (!(err instanceof ApiError) || err.status !== 401) {
                    // eslint-disable-next-line no-console
                    console.warn('load messages failed', err);
                }
            }
        })();
    }, [activeSessionId]);
    const sendMessage = useCallback(async (text, attachmentIds) => {
        if (!activeSessionId || isStreaming)
            return;
        // Snapshot the session id so SSE events route to the correct
        // transcript even if the user switches sessions mid-stream.
        const sessionId = activeSessionId;
        setIsStreaming(true);
        try {
            abortRef.current = new AbortController();
            const res = await fetch(`/api/bff/v1/sessions/${sessionId}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: text, attachment_ids: attachmentIds || [] }),
                credentials: 'include',
                signal: abortRef.current.signal,
            });
            if (!res.ok || !res.body) {
                setIsStreaming(false);
                return;
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            while (true) {
                const { done, value } = await reader.read();
                if (done)
                    break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                let eventType = '';
                for (const line of lines) {
                    if (line.startsWith('event: ')) {
                        eventType = line.slice(7).trim();
                    }
                    else if (line.startsWith('data: ') && eventType) {
                        try {
                            const data = JSON.parse(line.slice(6));
                            handleSSEEvent(sessionId, eventType, data);
                        }
                        catch {
                            // Non-JSON data line, skip
                        }
                        eventType = '';
                    }
                }
            }
        }
        catch (e) {
            if (e.name !== 'AbortError') {
                // eslint-disable-next-line no-console
                console.error('Chat error:', e);
            }
        }
        finally {
            setIsStreaming(false);
        }
    }, [activeSessionId, isStreaming, setIsStreaming]);
    const cancel = useCallback(() => {
        // Snapshot the session id before any async work so we cancel the right session
        // even if the user navigates away while the request is in flight.
        const sessionId = useChatStore.getState().activeSessionId;
        // Immediately abort the SSE stream so the UI feels responsive.
        abortRef.current?.abort();
        setIsStreaming(false);
        // Fire-and-forget the backend cancel — we don't await so the UI isn't blocked.
        if (sessionId) {
            api.sessions.cancel(sessionId).catch((err) => {
                // eslint-disable-next-line no-console
                console.warn('backend cancel failed', err);
            });
        }
    }, [setIsStreaming]);
    return {
        sendMessage: sendMessage,
        cancel,
        isStreaming,
        messages: activeSessionId ? messages[activeSessionId] || [] : [],
    };
}
