/**
 * Vitest global setup — registers jest-dom matchers and resets Zustand store
 * state between tests so each test gets a clean slate.
 */
import '@testing-library/jest-dom/vitest';
import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
import { useChatStore } from '../stores/chatStore';

afterEach(() => {
  cleanup();
  // Reset store to initial state — Zustand keeps a single module-level
  // instance, so without this each test inherits state from the previous one.
  useChatStore.setState({
    sessions: [],
    activeSessionId: null,
    messages: {},
    streamingSessions: {},
    isStreaming: false,
    isLoggedIn: false,
    userProfile: null,
    bootstrapped: false,
  });
});
