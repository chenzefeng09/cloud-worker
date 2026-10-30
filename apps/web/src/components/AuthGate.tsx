import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import { onSessionExpired } from '../api/authEvents';
import { useChatStore } from '../stores/chatStore';
import { LoginPage } from './LoginPage';
import { WorkspaceLayout } from './WorkspaceLayout';

export const SESSION_EXPIRED_NOTICE = 'Your session has expired. Please sign in again.';

/** How long the bootstrap waits before asking `/auth/me` again after a failure. */
export const BOOT_RETRY_MS = 10_000;

/**
 * Boot the app: on first mount we ask the BFF to re-resolve the user from
 * the signed session cookie via `/auth/me`.  The BFF looks up the server-side
 * session the cookie names and forwards its access token to foundation
 * identity for verification, so this single round-trip is the only
 * authoritative way to know whether the browser still has a valid session.
 * We refuse to render either the login page or the workspace until the
 * bootstrap call resolves to avoid flashing the login form for users who
 * are already authenticated.  Only a 401 means "not signed in"; any other
 * failure (the BFF answers 503 while it cannot refresh the session, a 5xx,
 * the network) says nothing about the session, so instead of a sign-in form
 * that would replace it we show that sign-in can't be confirmed and ask again.
 *
 * After that, the BFF keeps the session alive by refreshing its tokens
 * server-side, so a 401 from any API call means it could not (cookie
 * expired, refresh refused, session revoked).  The API layer reports those
 * 401s (`api/authEvents`); we confirm with `/auth/me` — another endpoint's
 * 401 alone is not proof — and only then sign the user out to the login
 * page with a notice.  A failed check (e.g. BFF briefly unavailable) keeps
 * the user where they are.
 */
export function AuthGate() {
  const isLoggedIn = useChatStore((s) => s.isLoggedIn);
  const bootstrapped = useChatStore((s) => s.bootstrapped);
  const login = useChatStore((s) => s.login);
  const logout = useChatStore((s) => s.logout);
  const setBootstrapped = useChatStore((s) => s.setBootstrapped);
  const [notice, setNotice] = useState<string | undefined>();
  // Bumped to run the bootstrap again (automatic retry or "Try again").
  const [bootAttempt, setBootAttempt] = useState(0);
  // The last bootstrap failed for a reason other than "not signed in".
  const [bootUnavailable, setBootUnavailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const profile = await api.auth.me();
        if (cancelled) return;
        setBootUnavailable(false);
        login(profile);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          // No valid session: the login page.
          setBootUnavailable(false);
          setBootstrapped(true);
          return;
        }
        console.warn('auth bootstrap failed', err);
        setBootUnavailable(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [login, setBootstrapped, bootAttempt]);

  // While sign-in can't be confirmed, ask again every BOOT_RETRY_MS.
  useEffect(() => {
    if (!bootUnavailable) return;
    const timer = setTimeout(() => setBootAttempt((n) => n + 1), BOOT_RETRY_MS);
    return () => clearTimeout(timer);
  }, [bootUnavailable, bootAttempt]);

  useEffect(() => {
    let checking = false;
    let disposed = false;
    const unsubscribe = onSessionExpired(() => {
      // Signed-out 401s (bootstrap, a failed sign-in) need no action, and a
      // check already in flight covers 401s that arrive meanwhile — including
      // the one from its own /auth/me call.
      if (checking || !useChatStore.getState().isLoggedIn) return;
      checking = true;
      api.auth
        .me()
        .then(
          () => {
            // Still signed in: that 401 was about something else.
          },
          (err: unknown) => {
            if (disposed || !useChatStore.getState().isLoggedIn) return;
            if (err instanceof ApiError && err.status === 401) {
              setNotice(SESSION_EXPIRED_NOTICE);
              logout();
            }
          },
        )
        .finally(() => {
          checking = false;
        });
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [logout]);

  // The notice explains one sign-out; it must not linger into a later one.
  useEffect(() => {
    if (isLoggedIn) setNotice(undefined);
  }, [isLoggedIn]);

  if (!bootstrapped) {
    if (bootUnavailable) {
      return (
        <SignInUnavailable
          onRetry={() => setBootAttempt((n) => n + 1)}
          onSignIn={() => {
            setBootUnavailable(false);
            setBootstrapped(true);
          }}
        />
      );
    }
    return (
      <div className="min-h-screen flex items-center justify-center bg-surface-50 text-gray-500">
        Loading…
      </div>
    );
  }
  return isLoggedIn ? <WorkspaceLayout /> : <LoginPage notice={notice} />;
}

/** Bootstrap could not reach the sign-in check; the session cookie is kept. */
function SignInUnavailable({ onRetry, onSignIn }: { onRetry: () => void; onSignIn: () => void }) {
  return (
    <div className="min-h-screen flex items-center justify-center bg-surface-50">
      <div role="alert" className="w-full max-w-sm p-8 bg-white rounded-xl shadow-sm border border-surface-200 text-center">
        <h1 className="text-lg font-semibold mb-2">Can’t confirm your sign-in right now</h1>
        <p className="text-sm text-gray-600 mb-6">
          The sign-in service isn’t responding. We’ll check again automatically.
        </p>
        <button
          type="button"
          onClick={onRetry}
          className="w-full py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 font-medium"
        >
          Try again
        </button>
        <button type="button" onClick={onSignIn} className="mt-3 text-sm text-gray-500 hover:text-gray-700 underline">
          Sign in again
        </button>
      </div>
    </div>
  );
}
