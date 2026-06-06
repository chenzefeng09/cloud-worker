import { useState, useCallback } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api } from '../api/client';

export function useSessions() {
  const { sessions, setSessions, addSession, setActiveSession, activeSessionId } = useChatStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.sessions.list();
      // The BFF can hand back a 200 whose body is actually a downstream error
      // object (no `items`). Guard so a malformed page never poisons the store
      // with `undefined` — that used to crash <Sidebar> on `sessions.length`.
      setSessions(Array.isArray(page?.items) ? page.items : []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [setSessions]);

  const createSession = useCallback(async (title?: string) => {
    try {
      const session = await api.sessions.create(title);
      addSession(session);
      setActiveSession(session.id);
      return session;
    } catch (e) {
      setError((e as Error).message);
    }
  }, [addSession, setActiveSession]);

  const deleteSession = useCallback(async (id: string) => {
    try {
      await api.sessions.delete(id);
      setSessions(sessions.filter((s) => s.id !== id));
      if (activeSessionId === id) setActiveSession(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [sessions, activeSessionId, setSessions, setActiveSession]);

  return { sessions, activeSessionId, loading, error, fetchSessions, createSession, deleteSession, setActiveSession };
}
