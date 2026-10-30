/**
 * api/client — 401 handling.
 *
 * A 401 from the BFF means the browser session could not be kept alive, so
 * the client reports it (`notifySessionExpired`) for AuthGate to act on —
 * except on the sign-in calls, where 401 only means the credentials were
 * refused.  It also checks that the problem code survives the BFF's nesting
 * of an upstream identity error, which the login page relies on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { onSessionExpired } from './authEvents';
import { api, ApiError } from './client';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => jsonResponse(status, body));
  vi.stubGlobal('fetch', fn);
  return fn;
}

const SESSION_ENDED = {
  type: 'about:blank',
  title: 'Authentication failed',
  status: 401,
  detail: 'Session expired',
  code: 'AGENT.BFF.AUTH_FAILED',
};

describe('api client — 401 handling', () => {
  const expired = vi.fn();
  let unsubscribe: () => void;

  beforeEach(() => {
    expired.mockReset();
    unsubscribe = onSessionExpired(expired);
  });

  afterEach(() => {
    unsubscribe();
    vi.unstubAllGlobals();
  });

  it('reports a 401 from an API call as a possibly ended session', async () => {
    stubFetch(401, SESSION_ENDED);

    const err = await api.sessions.list().catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(401);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('reports a 401 from a direct upload/download call too', async () => {
    stubFetch(401, SESSION_ENDED);

    await expect(api.workspaces.getContent('ws_1', 'f_1')).rejects.toBeInstanceOf(ApiError);

    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('does not report refused sign-in credentials', async () => {
    stubFetch(401, { ...SESSION_ENDED, detail: 'Invalid credentials' });

    await expect(api.auth.login('u@example.com', 'bad')).rejects.toMatchObject({ status: 401 });

    expect(expired).not.toHaveBeenCalled();
  });

  it('does not report other failures', async () => {
    stubFetch(403, { detail: 'forbidden', code: 'AGENT.BFF.FORBIDDEN' });
    await expect(api.sessions.list()).rejects.toMatchObject({ status: 403 });

    stubFetch(503, { detail: 'unavailable', code: 'AGENT.BFF.SESSION_UNAVAILABLE' });
    await expect(api.sessions.list()).rejects.toMatchObject({ status: 503 });

    expect(expired).not.toHaveBeenCalled();
  });

  it("reads identity's code from the BFF's nested pass-through body", async () => {
    // Shape produced by the BFF's _raise_for_upstream for an identity problem.
    stubFetch(401, {
      detail: {
        detail: {
          code: 'FOUNDATION.IDENTITY.AUTH.ACCOUNT_SETUP_REQUIRED',
          title: 'Account setup required',
          status: 401,
          detail: 'Account is not fully set up (required actions pending)',
          user_message: '账号需要先完成更新才能登录。',
        },
        user_message: '账号需要先完成更新才能登录。',
      },
    });

    const err = await api.auth.login('u@example.com', 'pw').catch((e: unknown) => e);

    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('FOUNDATION.IDENTITY.AUTH.ACCOUNT_SETUP_REQUIRED');
    expect(expired).not.toHaveBeenCalled();
  });
});
