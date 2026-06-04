import { create } from 'zustand';
import type { Message, Session, UserProfile } from '../api/types';
import type { PlanStep } from '../lib/designFixtures';

interface ChatState {
  sessions: Session[];
  activeSessionId: string | null;
  messages: Record<string, Message[]>;
  /**
   * Real-time structured plan steps keyed by sessionId.
   * Populated from the SSE ``plan`` event while a run is in progress.
   * Shape matches the frontend ``PlanStep`` type: ``{n, t, d, live}``.
   */
  planSteps: Record<string, PlanStep[]>;
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
  setPlanSteps: (sessionId: string, steps: PlanStep[]) => void;
  setIsStreaming: (streaming: boolean) => void;
  login: (profile: UserProfile) => void;
  logout: () => void;
  setBootstrapped: (value: boolean) => void;
}

export const useChatStore = create<ChatState>((set) => ({
  sessions: [],
  activeSessionId: null,
  messages: {},
  planSteps: {},
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
    set((s) => {
      // Dedup by id — SSE and the post-refresh /messages hydrate both arrive
      // for the same row and we used to render each message twice.
      const existing = s.messages[sessionId] || [];
      if (message.id && existing.some((m) => m.id === message.id)) return s;
      return {
        messages: { ...s.messages, [sessionId]: [...existing, message] },
      };
    }),
  updateLastMessage: (sessionId, content) =>
    set((s) => {
      const msgs = s.messages[sessionId] || [];
      if (msgs.length === 0) return s;
      const updated = [...msgs];
      updated[updated.length - 1] = { ...updated[updated.length - 1], content };
      return { messages: { ...s.messages, [sessionId]: updated } };
    }),
  setPlanSteps: (sessionId, steps) =>
    set((s) => ({ planSteps: { ...s.planSteps, [sessionId]: steps } })),
  setIsStreaming: (isStreaming) => set({ isStreaming }),
  login: (profile) => set({ isLoggedIn: true, userProfile: profile, bootstrapped: true }),
  logout: () =>
    set({
      isLoggedIn: false,
      userProfile: null,
      sessions: [],
      messages: {},
      planSteps: {},
      activeSessionId: null,
      bootstrapped: true,
    }),
  setBootstrapped: (value) => set({ bootstrapped: value }),
}));
