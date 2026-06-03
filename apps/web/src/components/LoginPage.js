import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from 'react';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
export function LoginPage() {
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(false);
    const login = useChatStore((s) => s.login);
    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        setLoading(true);
        try {
            await api.auth.login(email, password);
            // Resolve the authoritative profile via /auth/me so the store never
            // ends up with stale data the login endpoint happened to return.
            const profile = await api.auth.me();
            login(profile);
        }
        catch (err) {
            // Show a friendly message; the API error class carries enough detail
            // for ops to diagnose via request_id but the raw `.message` is fine
            // for end users (foundation surfaces localised text in `detail`).
            if (err instanceof ApiError && err.status === 401) {
                setError('邮箱或密码不正确，请重试。');
            }
            else {
                setError('登录失败，请稍后重试。');
                // eslint-disable-next-line no-console
                console.warn('login failed', err);
            }
        }
        finally {
            setLoading(false);
        }
    };
    return (_jsx("div", { className: "min-h-screen flex items-center justify-center bg-surface-50", children: _jsxs("div", { className: "w-full max-w-sm p-8 bg-white rounded-xl shadow-sm border border-surface-200", children: [_jsx("h1", { className: "text-2xl font-semibold text-center mb-8", children: "Agent \u5E73\u53F0" }), _jsxs("form", { onSubmit: handleSubmit, className: "space-y-4", children: [_jsxs("div", { children: [_jsx("label", { htmlFor: "email", className: "block text-sm font-medium text-gray-700 mb-1", children: "\u90AE\u7BB1" }), _jsx("input", { id: "email", type: "email", value: email, onChange: (e) => setEmail(e.target.value), className: "w-full px-3 py-2 border border-surface-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500", required: true, autoFocus: true })] }), _jsxs("div", { children: [_jsx("label", { htmlFor: "password", className: "block text-sm font-medium text-gray-700 mb-1", children: "\u5BC6\u7801" }), _jsx("input", { id: "password", type: "password", value: password, onChange: (e) => setPassword(e.target.value), className: "w-full px-3 py-2 border border-surface-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-primary-500", required: true })] }), error && _jsx("p", { className: "text-red-500 text-sm", children: error }), _jsx("button", { type: "submit", disabled: loading, className: "w-full py-2 bg-primary-600 text-white rounded-lg hover:bg-primary-700 disabled:opacity-50 font-medium", children: loading ? '登录中…' : '登录' })] })] }) }));
}
