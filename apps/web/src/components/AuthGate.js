import { jsx as _jsx } from "react/jsx-runtime";
import { useEffect } from 'react';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
import { LoginPage } from './LoginPage';
import { WorkspaceLayout } from './WorkspaceLayout';
/**
 * Boot the app: on first mount we ask the BFF to re-resolve the user from
 * the signed session cookie via `/auth/me`.  The BFF in turn forwards the
 * embedded access token to foundation identity for JWKS verification, so
 * this single round-trip is the only authoritative way to know whether the
 * browser still has a valid session.  We refuse to render either the
 * login page or the workspace until the bootstrap call resolves to avoid
 * flashing the login form for users who are already authenticated.
 */
export function AuthGate() {
    const isLoggedIn = useChatStore((s) => s.isLoggedIn);
    const bootstrapped = useChatStore((s) => s.bootstrapped);
    const login = useChatStore((s) => s.login);
    const setBootstrapped = useChatStore((s) => s.setBootstrapped);
    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const profile = await api.auth.me();
                if (cancelled)
                    return;
                login(profile);
            }
            catch (err) {
                if (cancelled)
                    return;
                // 401 from /auth/me just means no valid cookie — render the login
                // page.  Anything else is a real error; log it but still proceed
                // to the login page so the user is not stuck on a blank screen.
                if (!(err instanceof ApiError) || err.status !== 401) {
                    // eslint-disable-next-line no-console
                    console.warn('auth bootstrap failed', err);
                }
                setBootstrapped(true);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, [login, setBootstrapped]);
    if (!bootstrapped) {
        return (_jsx("div", { className: "min-h-screen flex items-center justify-center bg-surface-50 text-gray-500", children: "Loading\u2026" }));
    }
    return isLoggedIn ? _jsx(WorkspaceLayout, {}) : _jsx(LoginPage, {});
}
