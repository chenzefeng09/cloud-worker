import { useCallback, useEffect, useRef, useState } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api, ApiError } from '../api/client';
import type { Message, MessageAttachment } from '../api/types';

/**
 * SSE event handler bound to a specific sessionId.
 *
 * Why we capture the sessionId at send-time
 * -----------------------------------------
 * The previous implementation read `activeSessionId` from the store *inside*
 * the handler.  If the user switched to a different session while an SSE
 * stream was still running, every subsequent agent message went into the
 * newly-active session.  We now snapshot the sessionId when `sendMessage` is
 * called and use that for every event the stream emits.
 *
 * Ordered streaming pipeline
 * --------------------------
 * SSE events arrive in chronological order from the Redis stream.  We build
 * an ordered block list in the store rather than collapsing everything into
 * two buckets (text + tools).
 *
 * Event → store method mapping:
 *   token    → appendStreamingToken   (appends to open text segment, or opens new one)
 *   tool     → upsertStreamingTool    (seals open text; creates/updates tool block)
 *   thinking → appendStreamingThinking(seals open text; appends thinking block)
 *   message  → clearStreamingBlocks + addMessage  (replaces temp blocks with real DB rows)
 *   done     → setIsStreaming(false)
 */
function handleSSEEvent(
  sessionId: string,
  eventType: string,
  data: Record<string, unknown>,
  onDone: () => void,
): void {
  const store = useChatStore.getState();
  switch (eventType) {
    case 'message_created':
      // Backend echo of the user message — may replace the optimistic entry.
      store.addMessage(sessionId, data as unknown as Message);
      break;

    case 'token': {
      const runId = data.run_id as string | undefined;
      const delta = data.delta as string | undefined;
      if (runId && delta !== undefined) {
        store.appendStreamingToken(sessionId, runId, delta);
      }
      break;
    }

    case 'tool': {
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
      break;
    }

    case 'thinking': {
      const runId = data.run_id as string | undefined;
      const text = data.text as string | undefined;
      if (runId && text) {
        store.appendStreamingThinking(sessionId, runId, text);
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

    case 'run_started':
    case 'run_complete':
      // Lifecycle events — nothing to render.
      break;

    case 'done':
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
  }
}

export function useChat() {
  const { activeSessionId, messages, setIsStreaming, isStreaming } = useChatStore();
  const abortRef = useRef<AbortController | null>(null);
  const loadedRef = useRef<Set<string>>(new Set());
  // Prefill text to inject into InputBar after a rewind (cleared once consumed).
  const [prefillText, setPrefillText] = useState<string>('');
  // Rewind confirmation state — drives the app-level confirm modal.
  const [pendingRewind, setPendingRewind] = useState<Message | null>(null);
  const [rewindBusy, setRewindBusy] = useState<boolean>(false);
  const [rewindError, setRewindError] = useState<string>('');

  // Hydrate transcript from server when the active session changes (page
  // refresh, deep-link, session switch).
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

  const sendMessage = useCallback(
    async (text: string, attachmentIds?: string[], attachmentsMeta?: MessageAttachment[]) => {
      if (!activeSessionId || isStreaming) return;
      const sessionId = activeSessionId;

      setIsStreaming(true);

      // Optimistic USER message.
      const tempUserId = `temp_user_${Date.now()}`;
      useChatStore.getState().addMessage(sessionId, {
        id: tempUserId,
        session_id: sessionId,
        role: 'USER',
        content: text,
        tool_calls: null,
        run_id: null,
        sequence: 0,
        created_at: new Date().toISOString(),
        attachments: attachmentsMeta && attachmentsMeta.length > 0 ? attachmentsMeta : undefined,
      });

      let isDone = false;
      const markDone = () => {
        if (!isDone) {
          isDone = true;
          setIsStreaming(false);
        }
      };

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

        if (!res.ok || !res.body) {
          useChatStore.getState().addMessage(sessionId, {
            id: `err_${Date.now()}`,
            session_id: sessionId,
            role: 'SYSTEM',
            content: `请求失败 (${res.status})`,
            tool_calls: null,
            run_id: null,
            sequence: 0,
            created_at: new Date().toISOString(),
          });
          markDone();
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
                handleSSEEvent(sessionId, eventType, data, markDone);
              } catch {
                // Non-JSON data line, skip
              }
              eventType = '';
            }
          }
        }
      } catch (e) {
        if ((e as Error).name !== 'AbortError') {
           
          console.error('Chat error:', e);
        }
      } finally {
        markDone();
      }
    },
    [activeSessionId, isStreaming, setIsStreaming],
  );

  const cancel = useCallback(() => {
    const sessionId = useChatStore.getState().activeSessionId;
    abortRef.current?.abort();
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
      // eslint-disable-next-line no-console
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
