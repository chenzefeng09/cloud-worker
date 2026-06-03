import { create } from 'zustand';
export const useChatStore = create((set) => ({
    sessions: [],
    activeSessionId: null,
    messages: {},
    isStreaming: false,
    isLoggedIn: false,
    userProfile: null,
    bootstrapped: false,
    setSessions: (sessions) => set({ sessions }),
    addSession: (session) => set((s) => ({ sessions: [session, ...s.sessions] })),
    setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),
    setMessages: (sessionId, messages) => set((s) => ({ messages: { ...s.messages, [sessionId]: messages } })),
    addMessage: (sessionId, message) => set((s) => {
        // Dedup by id — SSE and the post-refresh /messages hydrate both arrive
        // for the same row and we used to render each message twice.
        const existing = s.messages[sessionId] || [];
        if (message.id && existing.some((m) => m.id === message.id))
            return s;
        return {
            messages: { ...s.messages, [sessionId]: [...existing, message] },
        };
    }),
    updateLastMessage: (sessionId, content) => set((s) => {
        const msgs = s.messages[sessionId] || [];
        if (msgs.length === 0)
            return s;
        const updated = [...msgs];
        updated[updated.length - 1] = { ...updated[updated.length - 1], content };
        return { messages: { ...s.messages, [sessionId]: updated } };
    }),
    setIsStreaming: (isStreaming) => set({ isStreaming }),
    login: (profile) => set({ isLoggedIn: true, userProfile: profile, bootstrapped: true }),
    logout: () => set({
        isLoggedIn: false,
        userProfile: null,
        sessions: [],
        messages: {},
        activeSessionId: null,
        bootstrapped: true,
    }),
    setBootstrapped: (value) => set({ bootstrapped: value }),
}));
