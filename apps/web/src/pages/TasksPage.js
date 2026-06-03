import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { useSessions } from '../hooks/useSessions';
import { formatRelativeTimeZh, sessionStatusZh } from '../lib/format';
const STATUS_ORDER = ['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED'];
/** Static progress placeholder per status — purely cosmetic, no backend data. */
function staticProgress(status) {
    switch (status) {
        case 'ACTIVE': return 0.6;
        case 'PAUSED': return 0.4;
        case 'COMPLETED': return 1.0;
        case 'ERROR': return 0.3;
        case 'ARCHIVED': return 1.0;
    }
}
export function TasksPage() {
    const { sessions, loading, createSession, error } = useSessions();
    const navigate = useNavigate();
    const handleNew = async () => {
        const s = await createSession();
        if (s)
            navigate(`/tasks/${s.id}`);
    };
    const groups = STATUS_ORDER.map((status) => ({
        status,
        items: sessions.filter((s) => s.status === status),
    })).filter((g) => g.items.length > 0);
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [{ label: '首页', to: '/home' }, { label: '任务' }], actions: _jsx("button", { onClick: handleNew, disabled: loading, className: "cw-btn cw-btn-accent cw-btn-sm disabled:opacity-50", children: "+ \u65B0\u5EFA\u4EFB\u52A1" }) }), _jsx("div", { className: "cw-scroll flex-1 px-8 py-5", style: { overflowX: 'auto' }, children: _jsxs("div", { style: { minWidth: 700, maxWidth: 1080, display: 'flex', flexDirection: 'column', gap: 16 }, children: [loading && sessions.length === 0 && (_jsx("div", { className: "font-mono text-[11px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" })), error && (_jsxs("div", { className: "px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent", children: ["\u52A0\u8F7D\u4EFB\u52A1\u5931\u8D25\uFF1A", error] })), !loading && sessions.length === 0 && !error && (_jsx("div", { className: "font-mono italic text-dim text-[12px]", children: "\u6682\u65E0\u4EFB\u52A1" })), groups.map((g) => (_jsxs("div", { children: [_jsxs("div", { className: "cw-label", style: { marginBottom: 8 }, children: [sessionStatusZh(g.status), " \u00B7 ", g.items.length] }), _jsx("div", { style: { display: 'flex', flexDirection: 'column', gap: 6 }, children: g.items.map((s) => {
                                        const progress = staticProgress(s.status);
                                        const isActive = s.status === 'ACTIVE';
                                        return (_jsxs("div", { onClick: () => navigate(`/tasks/${s.id}`), className: "border border-line bg-surface rounded-md cursor-pointer hover:border-ink-2", style: {
                                                display: 'flex',
                                                flexDirection: 'column',
                                                gap: 8,
                                                padding: '12px 14px',
                                            }, children: [_jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 10 }, children: [_jsx(StatusDot, { status: sessionToDesignStatus(s.status) }), _jsx("div", { style: { flex: 1, minWidth: 0 }, children: _jsx("div", { className: "font-medium", style: {
                                                                    fontSize: 13.5,
                                                                    whiteSpace: 'nowrap',
                                                                    overflow: 'hidden',
                                                                    textOverflow: 'ellipsis',
                                                                }, children: s.title || '未命名任务' }) }), _jsx("span", { className: "font-mono text-dim", style: { fontSize: 11, flexShrink: 0 }, children: formatRelativeTimeZh(s.updated_at) })] }), _jsxs("div", { className: "text-dim", style: { fontSize: 12 }, children: ["\u4F1A\u8BDD ", s.id.slice(0, 12)] }), _jsx("div", { style: { display: 'flex', alignItems: 'center', gap: 10 }, children: _jsxs("div", { style: {
                                                            flex: 1,
                                                            display: 'flex',
                                                            alignItems: 'center',
                                                            gap: 8,
                                                        }, children: [_jsx("div", { style: {
                                                                    flex: 1,
                                                                    height: 3,
                                                                    background: 'var(--line-2)',
                                                                    borderRadius: 2,
                                                                    overflow: 'hidden',
                                                                }, children: _jsx("div", { style: {
                                                                        width: `${progress * 100}%`,
                                                                        height: '100%',
                                                                        background: isActive ? 'var(--accent)' : 'var(--ink)',
                                                                    } }) }), _jsxs("span", { className: "font-mono text-dim", style: { fontSize: 11, width: 36, textAlign: 'right' }, children: [Math.round(progress * 100), "%"] })] }) })] }, s.id));
                                    }) })] }, g.status)))] }) })] }));
}
