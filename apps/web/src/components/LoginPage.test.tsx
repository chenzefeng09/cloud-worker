/**
 * LoginPage — what a failed sign-in tells the user:
 *  - an account with pending required actions (identity's
 *    ACCOUNT_SETUP_REQUIRED) gets told how to resolve it, not "wrong password"
 *  - refused credentials stay deliberately generic
 *  - anything else is a "try again later"
 * plus the notice AuthGate passes after a session expired.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useChatStore } from '../stores/chatStore';

const auth = vi.hoisted(() => ({
  login: vi.fn(),
  me: vi.fn(),
  ssoOptions: vi.fn(),
}));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: { auth } };
});

import { ApiError } from '../api/client';
import { ACCOUNT_SETUP_REQUIRED_MESSAGE, LoginPage } from './LoginPage';

const PROFILE = {
  user_id: 'usr_1',
  email: 'u@example.com',
  display_name: 'U',
  tenant_id: 'tnt_1',
  roles: [],
};

async function signIn() {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText('Email'), 'u@example.com');
  await user.type(screen.getByLabelText('Password'), 'pw');
  await user.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('LoginPage', () => {
  beforeEach(() => {
    auth.login.mockReset();
    auth.me.mockReset();
    auth.ssoOptions.mockReset().mockResolvedValue({ items: [] });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('explains how to finish account setup instead of blaming the password', async () => {
    auth.login.mockRejectedValue(
      new ApiError(401, 'FOUNDATION.IDENTITY.AUTH.ACCOUNT_SETUP_REQUIRED', '账号需要先完成更新才能登录。'),
    );
    render(<LoginPage />);
    await signIn();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(ACCOUNT_SETUP_REQUIRED_MESSAGE);
    expect(alert).toHaveTextContent(/password reset email/);
    expect(alert).toHaveTextContent('Admin → Users → Reset password');
    expect(screen.queryByText(/Incorrect email or password/)).not.toBeInTheDocument();
    expect(useChatStore.getState().isLoggedIn).toBe(false);
  });

  it('keeps refused credentials generic', async () => {
    auth.login.mockRejectedValue(new ApiError(401, 'AGENT.BFF.AUTH_FAILED', 'Invalid credentials'));
    render(<LoginPage />);
    await signIn();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Incorrect email or password. Please try again.',
    );
  });

  it('reports other failures as temporary', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    auth.login.mockRejectedValue(new ApiError(503, 'AGENT.BFF.SESSION_UNAVAILABLE', 'unavailable'));
    render(<LoginPage />);
    await signIn();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Sign-in failed. Please try again later.',
    );
  });

  it('signs in with the profile from /auth/me', async () => {
    auth.login.mockResolvedValue({});
    auth.me.mockResolvedValue(PROFILE);
    render(<LoginPage />);
    await signIn();

    await waitFor(() => expect(useChatStore.getState().isLoggedIn).toBe(true));
    expect(useChatStore.getState().userProfile).toEqual(PROFILE);
    expect(auth.login).toHaveBeenCalledWith('u@example.com', 'pw');
  });

  it('shows the notice it was given until a sign-in attempt fails', async () => {
    auth.login.mockRejectedValue(new ApiError(401, 'AGENT.BFF.AUTH_FAILED', 'Invalid credentials'));
    render(<LoginPage notice="Your session has expired. Please sign in again." />);

    expect(screen.getByRole('status')).toHaveTextContent(
      'Your session has expired. Please sign in again.',
    );
    await signIn();

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });
});
