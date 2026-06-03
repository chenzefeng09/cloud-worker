import { useState, useCallback } from 'react';
import { useChatStore } from '../stores/chatStore';
import { api } from '../api/client';
export function useSessions() {
    const { sessions, setSessions, addSession, setActiveSession, activeSessionId } = useChatStore();
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const fetchSessions = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const page = await api.sessions.list();
            setSessions(page.items);
        }
        catch (e) {
            setError(e.message);
        }
        finally {
            setLoading(false);
        }
    }, [setSessions]);
    const createSession = useCallback(async (title) => {
        try {
            const session = await api.sessions.create(title);
            addSession(session);
            setActiveSession(session.id);
            return session;
        }
        catch (e) {
            setError(e.message);
        }
    }, [addSession, setActiveSession]);
    const deleteSession = useCallback(async (id) => {
        try {
            await api.sessions.delete(id);
            setSessions(sessions.filter((s) => s.id !== id));
            if (activeSessionId === id)
                setActiveSession(null);
        }
        catch (e) {
            setError(e.message);
        }
    }, [sessions, activeSessionId, setSessions, setActiveSession]);
    return { sessions, activeSessionId, loading, error, fetchSessions, createSession, deleteSession, setActiveSession };
}
