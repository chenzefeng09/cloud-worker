import { useCallback, useRef } from 'react';
import { useChatStore } from '../stores/chatStore';
import type { Message } from '../api/types';

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
function handleSSEEvent(
  sessionId: string,
  eventType: string,
  data: Record<string, unknown>,
): void {
  const store = useChatStore.getState();
  switch (eventType) {
    case 'message_created':
    case 'agent_message':
      store.addMessage(sessionId, data as unknown as Message);
      break;
    case 'tool_call':
      store.addMessage(sessionId, {
        id: `tool_${Date.now()}`,
        session_id: sessionId,
        role: 'TOOL',
        content: `Tool: ${(data as Record<string, unknown>).name || 'unknown'}`,
        tool_calls: data as Record<string, unknown>,
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
        content: `Error: ${(data as Record<string, unknown>).detail || 'Unknown error'}`,
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
  const abortRef = useRef<AbortController | null>(null);

  const sendMessage = useCallback(
    async (text: string) => {
      if (!activeSessionId || isStreaming) return;
      // Snapshot the session id so SSE events route to the correct
      // transcript even if the user switches sessions mid-stream.
      const sessionId = activeSessionId;

      setIsStreaming(true);

      try {
        abortRef.current = new AbortController();

        const res = await fetch(`/api/bff/v1/sessions/${sessionId}/chat`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ message: text }),
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
                handleSSEEvent(sessionId, eventType, data);
              } catch {
                // Non-JSON data line, skip
              }
              eventType = '';
            }
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
          // eslint-disable-next-line no-console
          console.error('Chat error:', e);
        }
      } finally {
        setIsStreaming(false);
      }
    },
    [activeSessionId, isStreaming, setIsStreaming],
  );

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setIsStreaming(false);
  }, [setIsStreaming]);

  return {
    sendMessage,
    cancel,
    isStreaming,
    messages: activeSessionId ? messages[activeSessionId] || [] : [],
  };
}
