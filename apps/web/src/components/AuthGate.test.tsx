/**
 * AuthGate boot tests.
 *
 * The component decides whether to render LoginPage or WorkspaceLayout based
 * on the result of /auth/me; getting that flow right is the difference between
 * the SPA flashing the login form for already-logged-in users and rendering
 * the workspace immediately.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useChatStore } from '../stores/chatStore';
import { AuthGate, BOOT_RETRY_MS } from './AuthGate';

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

const PROFILE = {
  user_id: 'usr_test',
  email: 't@x.com',
  display_name: 'T',
  tenant_id: 'tnt_test',
  roles: [],
};

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
      user_id: 'usr_test',
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

  // Only a 401 means "signed out".  A 503 (the BFF can't refresh the session
  // while identity is down) or a network error says nothing about the session:
  // a sign-in form would replace a session that may be fine.
  it.each([
    ['a 503 from the BFF', async () => {
      const { ApiError } = await import('../api/client');
      return new ApiError(503, 'AGENT.BFF.SESSION_UNAVAILABLE', 'identity unavailable');
    }],
    ['a network error', async () => new Error('network gone')],
  ])('shows sign-in as unavailable, not the login form, on %s', async (_label, makeError) => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockMe.mockRejectedValue(await makeError());
    render(<AuthGate />);

    expect(await screen.findByRole('alert')).toHaveTextContent(/can’t confirm your sign-in/i);
    expect(screen.queryByTestId('login')).not.toBeInTheDocument();
    expect(useChatStore.getState().bootstrapped).toBe(false);
  });

  it('"Try again" asks /auth/me again and opens the workspace once it answers', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockMe.mockRejectedValueOnce(new Error('network gone')).mockResolvedValueOnce(PROFILE);
    render(<AuthGate />);

    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }));

    expect(await screen.findByTestId('workspace')).toBeInTheDocument();
    expect(mockMe).toHaveBeenCalledTimes(2);
  });

  it('asks again on its own while sign-in stays unconfirmed', async () => {
    vi.useFakeTimers();
    try {
      vi.spyOn(console, 'warn').mockImplementation(() => {});
      mockMe.mockRejectedValue(new Error('network gone'));
      render(<AuthGate />);
      await act(async () => {}); // let the first /auth/me settle
      expect(mockMe).toHaveBeenCalledTimes(1);

      await act(async () => vi.advanceTimersByTimeAsync(BOOT_RETRY_MS));
      expect(mockMe).toHaveBeenCalledTimes(2);

      mockMe.mockResolvedValue(PROFILE);
      await act(async () => vi.advanceTimersByTimeAsync(BOOT_RETRY_MS));
      expect(mockMe).toHaveBeenCalledTimes(3);
      expect(screen.getByTestId('workspace')).toBeInTheDocument();

      // Signed in: no more retries.
      await act(async () => vi.advanceTimersByTimeAsync(BOOT_RETRY_MS * 3));
      expect(mockMe).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('"Sign in again" leaves for the login page', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockMe.mockRejectedValue(new Error('network gone'));
    render(<AuthGate />);

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in again' }));

    expect(screen.getByTestId('login')).toBeInTheDocument();
  });
});
