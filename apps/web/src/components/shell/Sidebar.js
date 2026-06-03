import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { NavLink, useNavigate } from 'react-router-dom';
import { useWorkspace } from '../../lib/workspaceContext';
import { useChatStore } from '../../stores/chatStore';
import { useSessions } from '../../hooks/useSessions';
import { api } from '../../api/client';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';
const NAV = [
    { to: '/home', label: '首页', glyph: '◇' },
    { to: '/tasks', label: '任务', glyph: '▤', countFrom: 'tasks' },
    { to: '/files', label: '基础文件', glyph: '⌗', countFrom: 'files' },
    { to: '/library', label: '资源库', glyph: '✦', count: '13' },
    { to: '/fleet', label: '工作节点', glyph: '◈', count: '6' },
];
export function Sidebar() {
    const { ws, files } = useWorkspace();
    const { sessions } = useSessions();
    const userProfile = useChatStore((s) => s.userProfile);
    const logout = useChatStore((s) => s.logout);
    const navigate = useNavigate();
    const navCounts = {
        tasks: String(sessions.length),
        files: String(files.length),
    };
    const recent = sessions.slice(0, 3);
    const handleLogout = async () => {
        try {
            await api.auth.logout();
        }
        catch {
            // The BFF logout endpoint is best-effort; even on failure we clear local
            // state so the user lands back on the login page.
        }
        logout();
        navigate('/home');
    };
    return (_jsxs("aside", { className: "w-[220px] shrink-0 border-r border-line p-3.5 flex flex-col gap-5 bg-paper overflow-hidden", children: [_jsxs(NavLink, { to: "/home", className: "flex items-center gap-2.5 px-1 no-underline", children: [_jsx("div", { className: "relative w-[22px] h-[22px] bg-ink rounded-sm", children: _jsx("span", { className: "absolute inset-[5px] border-[1.25px] border-paper rounded-[1px]" }) }), _jsxs("div", { className: "font-mono text-[12px] font-semibold tracking-[-0.01em] text-ink", children: ["cloud", _jsx("em", { className: "not-italic text-accent font-medium", children: "\u00B7" }), "worker"] })] }), _jsx(WorkspaceSwitcher, {}), _jsxs("nav", { className: "flex flex-col gap-0.5", children: [NAV.map((n) => {
                        const count = n.countFrom ? navCounts[n.countFrom] : n.count;
                        return (_jsx(NavLink, { to: n.to, className: ({ isActive }) => [
                                'relative flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-[13px] tracking-[-0.01em] border select-none cursor-pointer no-underline',
                                isActive
                                    ? 'bg-surface border-line text-ink'
                                    : 'text-ink-2 border-transparent hover:bg-paper-2 hover:text-ink',
                            ].join(' '), end: n.to === '/home', children: ({ isActive }) => (_jsxs(_Fragment, { children: [isActive && (_jsx("span", { className: "absolute -left-px top-2 bottom-2 w-0.5 bg-accent rounded-[1px]" })), _jsx("span", { className: `w-4 text-center font-mono text-[11px] ${isActive ? 'text-accent' : 'text-dim'}`, children: n.glyph }), _jsx("span", { className: "flex-1 truncate", children: n.label }), count && (_jsx("span", { className: "ml-auto font-mono text-[10px] text-dim", children: count }))] })) }, n.to));
                    }), _jsxs("div", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim px-2 pt-3 pb-1", children: ["\u6700\u8FD1 \u00B7 ", ws.name] }), recent.length === 0 && (_jsx("div", { className: "font-mono italic text-dim text-[11px] px-2.5 py-1", children: "\u6682\u65E0\u4EFB\u52A1" })), recent.map((s) => (_jsxs(NavLink, { to: `/tasks/${s.id}`, className: ({ isActive }) => [
                            'flex items-center gap-2.5 px-2 py-1.5 rounded-sm border cursor-pointer no-underline truncate',
                            isActive
                                ? 'bg-surface border-line text-ink'
                                : 'border-transparent text-ink-2 hover:bg-paper-2 hover:text-ink',
                        ].join(' '), children: [_jsx("span", { className: "w-4 text-center font-mono text-[11px]", style: {
                                    color: s.status === 'ACTIVE'
                                        ? 'var(--cw-accent, #d23a1f)'
                                        : s.status === 'COMPLETED'
                                            ? '#2c8f5d'
                                            : '#75756f',
                                }, children: s.status === 'ACTIVE'
                                    ? '●'
                                    : s.status === 'COMPLETED'
                                        ? '✓'
                                        : s.status === 'PAUSED'
                                            ? '‖'
                                            : '◷' }), _jsx("span", { className: "truncate text-[12.5px]", children: s.title || '未命名任务' })] }, s.id)))] }), userProfile?.roles?.includes('super_admin') && (_jsx("div", { className: "pt-2 border-t border-line-2", children: _jsx(NavLink, { to: "/admin", className: ({ isActive }) => [
                        'relative flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-[13px] tracking-[-0.01em] border select-none cursor-pointer no-underline',
                        isActive
                            ? 'bg-surface border-line text-ink'
                            : 'text-ink-2 border-transparent hover:bg-paper-2 hover:text-ink',
                    ].join(' '), children: ({ isActive }) => (_jsxs(_Fragment, { children: [isActive && (_jsx("span", { className: "absolute -left-px top-2 bottom-2 w-0.5 bg-accent rounded-[1px]" })), _jsx("span", { className: `w-4 text-center font-mono text-[11px] ${isActive ? 'text-accent' : 'text-dim'}`, children: "\u229E" }), _jsx("span", { className: "flex-1 truncate", children: "\u7BA1\u7406\u540E\u53F0" })] })) }) })), _jsxs("div", { className: "mt-auto flex flex-col gap-2 pt-3.5 border-t border-line", children: [_jsxs("div", { className: "flex items-center gap-2", children: [_jsx("span", { className: "w-[7px] h-[7px] rounded-full bg-accent shadow-[0_0_0_3px_rgba(210,58,31,0.08)]" }), _jsx("span", { className: "font-mono text-[11px] text-ink-2", children: "1 \u4E2A\u5DE5\u4F5C\u8282\u70B9 \u00B7 \u8FD0\u884C\u4E2D" })] }), _jsx("div", { className: "font-mono text-[10.5px] text-dim", children: "$4.21 \u4ECA\u65E5 \u00B7 $50 \u9884\u7B97" }), _jsxs(NavLink, { to: "/profile", className: ({ isActive }) => [
                            'flex items-center gap-2 mt-1 text-left text-[12.5px] no-underline',
                            isActive ? 'text-ink' : 'text-ink-2 hover:text-ink',
                        ].join(' '), children: [_jsx("span", { className: "inline-flex w-[22px] h-[22px] items-center justify-center rounded-full border border-line bg-surface font-mono text-[10px] font-semibold text-ink shrink-0", children: userProfile?.display_name?.[0]?.toUpperCase() ?? 'Y' }), _jsx("span", { className: "truncate", children: userProfile?.email ?? 'you@acme.co' })] }), _jsx("button", { onClick: handleLogout, className: "text-left font-mono text-[10px] text-dim hover:text-ink-2 pl-[30px]", children: "\u767B\u51FA" })] })] }));
}
