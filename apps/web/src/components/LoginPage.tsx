import { useEffect, useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';

type SsoOption = { id: string; name: string; kind: string };

export function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [ssoOptions, setSsoOptions] = useState<SsoOption[]>([]);
  const login = useChatStore((s) => s.login);

  // Surface a failed enterprise-login round-trip: the BFF callback bounces
  // the browser back here with ?sso_error=... on any failure.  Show a
  // generic message (the raw code is for ops, not end users) and scrub it
  // from the URL so a refresh doesn't keep re-showing it.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has('sso_error')) {
      setError('Enterprise sign-in failed. Please try again or contact your administrator.');
      params.delete('sso_error');
      const qs = params.toString();
      window.history.replaceState(
        {},
        '',
        window.location.pathname + (qs ? `?${qs}` : '') + window.location.hash,
      );
    }
  }, []);

  // Fetch the configured enterprise SSO options once on mount.  Failures
  // (and an empty list) are silent — the password form still works and we
  // simply render no extra buttons.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { items } = await api.auth.ssoOptions();
        if (!cancelled && Array.isArray(items)) setSsoOptions(items);
      } catch {
        // SSO not configured / identity unreachable — ignore.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSsoLogin = (id: string) => {
    window.location.href =
      '/api/bff/v1/auth/sso/' +
      encodeURIComponent(id) +
      '/start?return_to=' +
      encodeURIComponent(window.location.origin);
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await api.auth.login(email, password);
      // Resolve the authoritative profile via /auth/me so the store never
      // ends up with stale data the login endpoint happened to return.
      const profile = await api.auth.me();
      login(profile);
    } catch (err) {
      // Show a friendly message; the API error class carries enough detail
      // for ops to diagnose via request_id but the raw `.message` is fine
      // for end users (foundation surfaces localised text in `detail`).
      if (err instanceof ApiError && err.status === 401) {
        setError('Incorrect email or password. Please try again.');
      } else {
        setError('Sign-in failed. Please try again later.');
         
        console.warn('login failed', err);
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-surface-50">
      <div className="w-full max-w-sm p-8 bg-white rounded-xl shadow-sm border border-surface-200">
        <h1 className="text-2xl font-semibold text-center mb-8">Agent Platform</h1>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium text-gray-700 mb-1">Email</label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full px-3 py-2 border border-surface-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              required
              autoFocus
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-sm font-medium text-gray-700 mb-1">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full px-3 py-2 border border-surface-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500"
              required
            />
          </div>
          {error && <p className="text-red-500 text-sm">{error}</p>}
          <button
            type="submit"
            disabled={loading}
            className="w-full py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 font-medium"
          >
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        {ssoOptions.length > 0 && (
          <div className="mt-6">
            <div className="flex items-center gap-3 mb-4">
              <span className="flex-1 h-px bg-surface-200" />
              <span className="text-xs text-gray-400">or sign in with your enterprise account</span>
              <span className="flex-1 h-px bg-surface-200" />
            </div>
            <div className="space-y-2">
              {ssoOptions.map((opt) => (
                <button
                  key={opt.id}
                  type="button"
                  onClick={() => handleSsoLogin(opt.id)}
                  className="w-full py-2 px-3 flex items-center justify-center gap-2 border border-surface-200 rounded-lg hover:bg-surface-50 font-medium text-gray-700"
                >
                  <span>{opt.name}</span>
                  {opt.kind && (
                    <span className="text-xs text-gray-400 uppercase">
                      {opt.kind}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
