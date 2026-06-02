import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';

export function LoginPage() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const login = useChatStore((s) => s.login);

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
        setError('邮箱或密码不正确，请重试。');
      } else {
        setError('登录失败，请稍后重试。');
        // eslint-disable-next-line no-console
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
            {loading ? 'Signing in…' : 'Sign In'}
          </button>
        </form>
      </div>
    </div>
  );
}
