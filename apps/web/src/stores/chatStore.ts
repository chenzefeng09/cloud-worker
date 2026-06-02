import { create } from 'zustand';
import type { Message, Session, UserProfile } from '../api/types';

interface ChatState {
  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  isStreaming: boolean;
  isLoggedIn: boolean;
  userProfile: UserProfile | null;
  /**
   * False until the very first `/auth/me` call has resolved on app load.
   * Components like AuthGate gate their render on this so we don't flash the
   * login screen for users who actually have a valid signed cookie.
   */
  bootstrapped: boolean;

  setSessions: (sessions: Session[]) => void;
  addSession: (session: Session) => void;
  setActiveSession: (sessionId: string | null) => void;
  setMessages: (sessionId: string, messages: Message[]) => void;
  addMessage: (sessionId: string, message: Message) => void;
  updateLastMessage: (sessionId: string, content: string) => void;
  setIsStreaming: (streaming: boolean) => void;
  login: (profile: UserProfile) => void;
  logout: () => void;
  setBootstrapped: (value: boolean) => void;
}

export const useChatStore = create<ChatState>((set) => ({
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
  setMessages: (sessionId, messages) =>
    set((s) => ({ messages: { ...s.messages, [sessionId]: messages } })),
  addMessage: (sessionId, message) =>
    set((s) => ({
      messages: {
        ...s.messages,
        [sessionId]: [...(s.messages[sessionId] || []), message],
      },
    })),
  updateLastMessage: (sessionId, content) =>
    set((s) => {
      const msgs = s.messages[sessionId] || [];
      if (msgs.length === 0) return s;
      const updated = [...msgs];
      updated[updated.length - 1] = { ...updated[updated.length - 1], content };
      return { messages: { ...s.messages, [sessionId]: updated } };
    }),
  setIsStreaming: (isStreaming) => set({ isStreaming }),
  login: (profile) => set({ isLoggedIn: true, userProfile: profile, bootstrapped: true }),
  logout: () =>
    set({
      isLoggedIn: false,
      userProfile: null,
      sessions: [],
      messages: {},
      activeSessionId: null,
      bootstrapped: true,
    }),
  setBootstrapped: (value) => set({ bootstrapped: value }),
}));
