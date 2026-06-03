import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { useChatStore } from '../stores/chatStore';
import { TenantsPage } from './pages/TenantsPage';
import { UsersPage } from './pages/UsersPage';
import { QuotaPage } from './pages/QuotaPage';
import { AuditPage } from './pages/AuditPage';
const ADMIN_NAV = [
    { to: '/admin/tenants', label: '租户管理', glyph: '◫' },
    { to: '/admin/users', label: '用户管理', glyph: '◎' },
    { to: '/admin/quota', label: '配额套餐', glyph: '◷' },
    { to: '/admin/audit', label: '审计日志', glyph: '≡' },
];
/**
 * Admin-only shell. Rendered only when the authenticated user has
 * the 'super_admin' role — enforced at the route level in WorkspaceLayout.
 */
export function AdminLayout() {
    const userProfile = useChatStore((s) => s.userProfile);
    // Double-check gate: belt-and-suspenders, route-level gate should already block
    if (!userProfile?.roles?.includes('super_admin')) {
        return _jsx(Navigate, { to: "/home", replace: true });
    }
    return (_jsxs("div", { className: "h-full flex min-w-0", children: [_jsxs("aside", { className: "w-[220px] shrink-0 border-r border-line p-3.5 flex flex-col gap-5 bg-paper overflow-hidden", children: [_jsx("div", { className: "flex items-center gap-2 px-1", children: _jsx("span", { className: "font-mono text-[10px] font-semibold tracking-[0.12em] uppercase text-dim", children: "\u7BA1\u7406\u540E\u53F0" }) }), _jsx("nav", { className: "flex flex-col gap-0.5", children: ADMIN_NAV.map((n) => (_jsx(NavLink, { to: n.to, className: ({ isActive }) => [
                                'relative flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-[13px] tracking-[-0.01em] border select-none cursor-pointer no-underline',
                                isActive
                                    ? 'bg-surface border-line text-ink'
                                    : 'text-ink-2 border-transparent hover:bg-paper-2 hover:text-ink',
                            ].join(' '), children: ({ isActive }) => (_jsxs(_Fragment, { children: [isActive && (_jsx("span", { className: "absolute -left-px top-2 bottom-2 w-0.5 bg-accent rounded-[1px]" })), _jsx("span", { className: `w-4 text-center font-mono text-[11px] ${isActive ? 'text-accent' : 'text-dim'}`, children: n.glyph }), _jsx("span", { className: "flex-1 truncate", children: n.label })] })) }, n.to))) }), _jsxs("div", { className: "mt-auto pt-3.5 border-t border-line", children: [_jsxs(NavLink, { to: "/home", className: "flex items-center gap-2 text-[12px] text-dim hover:text-ink no-underline", children: [_jsx("span", { className: "font-mono text-[10px]", children: "\u2190" }), _jsx("span", { children: "\u8FD4\u56DE\u5DE5\u4F5C\u533A" })] }), _jsx("div", { className: "mt-2 font-mono text-[10px] text-dim truncate", children: userProfile?.email })] })] }), _jsx("main", { className: "flex-1 flex flex-col min-w-0 overflow-hidden", children: _jsxs(Routes, { children: [_jsx(Route, { path: "tenants", element: _jsx(TenantsPage, {}) }), _jsx(Route, { path: "users", element: _jsx(UsersPage, {}) }), _jsx(Route, { path: "quota", element: _jsx(QuotaPage, {}) }), _jsx(Route, { path: "audit", element: _jsx(AuditPage, {}) }), _jsx(Route, { path: "*", element: _jsx(Navigate, { to: "/admin/tenants", replace: true }) })] }) })] }));
}
