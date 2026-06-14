import { useState, useCallback } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api } from '../api/client';

export function useSessions() {
  const { sessions, setSessions, addSession, setActiveSession, activeSessionId } = useChatStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  const fetchSessions = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await api.sessions.list();
      // The BFF can hand back a 200 whose body is actually a downstream error
      // object (no `items`). Guard so a malformed page never poisons the store
      // with `undefined` — that used to crash <Sidebar> on `sessions.length`.
      if (Array.isArray(page?.items)) {
        setSessions(page.items);
        setCursor(page.next_cursor ?? null);
      } else {
        setSessions([]);
        setCursor(null);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [setSessions]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.sessions.list(cursor);
      const items = Array.isArray(page?.items) ? page.items : [];
      // Append, de-duplicating by id (the store may already hold
      // optimistic/prepended sessions). `setSessions` only accepts a plain
      // array, so compute the next array from the `sessions` closure value —
      // matching the existing `deleteSession` pattern below.
      const seen = new Set(sessions.map((s) => s.id));
      setSessions([...sessions, ...items.filter((s) => !seen.has(s.id))]);
      setCursor(page.next_cursor ?? null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, loadingMore, sessions, setSessions]);

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

  return {
    sessions,
    activeSessionId,
    loading,
    error,
    fetchSessions,
    createSession,
    deleteSession,
    setActiveSession,
    loadMore,
    loadingMore,
    hasMore: cursor !== null,
  };
}
