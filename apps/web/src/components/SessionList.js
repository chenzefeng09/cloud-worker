import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useSessions } from '../hooks/useSessions';
export function SessionList() {
    const { sessions, loading, createSession, setActiveSession, activeSessionId } = useSessions();
    return (_jsxs("div", { className: "p-2", children: [_jsx("button", { onClick: () => createSession(), disabled: loading, className: "w-full py-2 mb-2 text-sm font-medium bg-primary-50 text-primary-700 rounded-lg hover:bg-primary-100 disabled:opacity-50", children: "+ New Session" }), loading && _jsx("p", { className: "text-xs text-gray-400 p-2", children: "Loading..." }), sessions.map((session) => (_jsxs("button", { onClick: () => setActiveSession(session.id), className: `w-full text-left px-3 py-2 rounded-lg mb-1 text-sm truncate ${session.id === activeSessionId
                    ? 'bg-primary-50 text-primary-700'
                    : 'text-gray-600 hover:bg-surface-100'}`, children: [_jsx("div", { className: "font-medium truncate", children: session.title || 'New Session' }), _jsx("div", { className: "text-xs text-gray-400", children: session.status })] }, session.id))), sessions.length === 0 && !loading && (_jsx("p", { className: "text-xs text-gray-400 p-2", children: "No sessions yet" }))] }));
}
