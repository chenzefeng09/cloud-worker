/**
 * AuthGate — signing out when the browser session ends.
 *
 * The API layer reports every 401 (`notifySessionExpired`); AuthGate checks
 * `/auth/me` and only a 401 there signs the user out, back to the login page
 * with a notice.  A 401 that `/auth/me` contradicts, or a failed check (BFF
 * unavailable), keeps the user signed in.
 */
import { act, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { notifySessionExpired } from '../api/authEvents';
import { ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
import { AuthGate, SESSION_EXPIRED_NOTICE } from './AuthGate';

vi.mock('./LoginPage', () => ({
  LoginPage: ({ notice }: { notice?: string }) => <div data-testid="login">{notice}</div>,
}));
vi.mock('./WorkspaceLayout', () => ({
  WorkspaceLayout: () => <div data-testid="workspace">workspace</div>,
}));

const auth = vi.hoisted(() => ({ me: vi.fn() }));
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: { auth } };
});

const PROFILE = {
  user_id: 'usr_1',
  email: 'u@example.com',
  display_name: 'U',
  tenant_id: 'tnt_1',
  roles: [],
};

const expired = () => new ApiError(401, 'AGENT.BFF.AUTH_FAILED', 'Session expired');

/** Render AuthGate for a user the bootstrap /auth/me call signs in. */
async function renderSignedIn() {
  auth.me.mockResolvedValueOnce(PROFILE);
  render(<AuthGate />);
  await screen.findByTestId('workspace');
}

describe('AuthGate session expiry', () => {
  beforeEach(() => {
    auth.me.mockReset();
  });

  it('signs out to the login page with a notice once /auth/me confirms the 401', async () => {
    await renderSignedIn();
    auth.me.mockRejectedValueOnce(expired());

    act(() => notifySessionExpired());

    expect(await screen.findByTestId('login')).toHaveTextContent(SESSION_EXPIRED_NOTICE);
    expect(useChatStore.getState().isLoggedIn).toBe(false);
    expect(useChatStore.getState().userProfile).toBeNull();
  });

  it('stays signed in when /auth/me still succeeds', async () => {
    await renderSignedIn();
    auth.me.mockResolvedValueOnce(PROFILE);

    act(() => notifySessionExpired());

    await waitFor(() => expect(auth.me).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('workspace')).toBeInTheDocument();
    expect(useChatStore.getState().isLoggedIn).toBe(true);
  });

  it('stays signed in when the check itself fails', async () => {
    await renderSignedIn();
    auth.me.mockRejectedValueOnce(new ApiError(503, 'AGENT.BFF.SESSION_UNAVAILABLE', 'unavailable'));

    act(() => notifySessionExpired());

    await waitFor(() => expect(auth.me).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(screen.getByTestId('workspace')).toBeInTheDocument();
    expect(useChatStore.getState().isLoggedIn).toBe(true);
  });

  it('checks once for a burst of 401s', async () => {
    await renderSignedIn();
    let reject!: (err: unknown) => void;
    auth.me.mockReturnValueOnce(new Promise((_, r) => (reject = r)));

    act(() => {
      notifySessionExpired();
      notifySessionExpired();
      notifySessionExpired();
    });
    expect(auth.me).toHaveBeenCalledTimes(2); // bootstrap + one check

    await act(async () => reject(expired()));
    expect(await screen.findByTestId('login')).toHaveTextContent(SESSION_EXPIRED_NOTICE);
  });

  it('ignores 401s while signed out', async () => {
    auth.me.mockRejectedValueOnce(expired());
    render(<AuthGate />);
    await screen.findByTestId('login');

    act(() => notifySessionExpired());

    expect(auth.me).toHaveBeenCalledTimes(1); // the bootstrap call only
    expect(screen.getByTestId('login')).toBeEmptyDOMElement();
  });

  it('drops the notice after signing in again', async () => {
    await renderSignedIn();
    auth.me.mockRejectedValueOnce(expired());
    act(() => notifySessionExpired());
    await screen.findByText(SESSION_EXPIRED_NOTICE);

    act(() => useChatStore.getState().login(PROFILE));
    await screen.findByTestId('workspace');
    act(() => useChatStore.getState().logout());

    expect(await screen.findByTestId('login')).toBeEmptyDOMElement();
  });
});
