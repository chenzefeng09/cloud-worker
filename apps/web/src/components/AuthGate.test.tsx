/**
 * AuthGate boot tests.
 *
 * The component decides whether to render LoginPage or WorkspaceLayout based
 * on the result of /auth/me; getting that flow right is the difference between
 * the SPA flashing the login form for already-logged-in users and rendering
 * the workspace immediately.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { useChatStore } from '../stores/chatStore';
import { AuthGate } from './AuthGate';

// Stub the children so we don't drag in the full WorkspaceLayout — its
// hooks make their own network calls which would create noise.
vi.mock('./LoginPage', () => ({ LoginPage: () => <div data-testid="login">login</div> }));
vi.mock('./WorkspaceLayout', () => ({
  WorkspaceLayout: () => <div data-testid="workspace">workspace</div>,
}));

const mockMe = vi.fn();
vi.mock('../api/client', () => ({
  api: { auth: { me: () => mockMe() } },
  // ApiError class with the .status property AuthGate checks
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(status: number, code: string, detail: string) {
      super(detail);
      this.status = status;
      this.code = code;
    }
  },
}));

describe('AuthGate', () => {
  beforeEach(() => {
    useChatStore.setState({
      sessions: [],
      activeSessionId: null,
      messages: {},
      isStreaming: false,
      isLoggedIn: false,
      userProfile: null,
      bootstrapped: false,
    });
    mockMe.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders loading while bootstrap pending', () => {
    mockMe.mockReturnValue(new Promise(() => {})); // never resolves
    render(<AuthGate />);
    expect(screen.getByText(/loading/i)).toBeInTheDocument();
  });

  it('renders workspace when /auth/me succeeds', async () => {
    mockMe.mockResolvedValue({
      id: 'usr_test',
      email: 't@x.com',
      display_name: 'T',
      tenant_id: 'tnt_test',
      roles: [],
    });
    render(<AuthGate />);
    await waitFor(() =>
      expect(screen.getByTestId('workspace')).toBeInTheDocument(),
    );
  });

  it('renders login when /auth/me 401s', async () => {
    const { ApiError } = await import('../api/client');
    mockMe.mockRejectedValue(new ApiError(401, 'UNAUTH', 'no cookie'));
    render(<AuthGate />);
    await waitFor(() => expect(screen.getByTestId('login')).toBeInTheDocument());
  });

  it('still shows login on unexpected error (degrades gracefully)', async () => {
    mockMe.mockRejectedValue(new Error('network gone'));
    render(<AuthGate />);
    await waitFor(() => expect(screen.getByTestId('login')).toBeInTheDocument());
  });
});
