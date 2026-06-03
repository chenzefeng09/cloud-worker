import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useChatStore } from '../stores/chatStore';
import { useWorkspace } from '../lib/workspaceContext';
import { api } from '../api/client';
import { PF_USAGE_WEEK, PF_API_KEYS, PF_SESSIONS, PF_NOTIFS, PF_PLAN, PF_STATS, } from '../lib/designFixtures';
function PfSection({ title, children, action }) {
    return (_jsxs("div", { style: {
            border: '1px solid var(--line)',
            borderRadius: 'var(--r-md)',
            background: 'var(--surface)',
            overflow: 'hidden',
        }, children: [_jsxs("div", { style: {
                    padding: '9px 16px',
                    borderBottom: '1px solid var(--line-2)',
                    background: 'var(--paper)',
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                }, children: [_jsx("span", { className: "cw-label", children: title }), _jsx("div", { style: { flex: 1 } }), action] }), _jsx("div", { style: { padding: '14px 16px' }, children: children })] }));
}
function PfRow({ label, sub, children }) {
    return (_jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 16 }, children: [_jsxs("div", { style: { flex: 1, minWidth: 0 }, children: [_jsx("div", { style: { fontSize: 13, fontWeight: 500, letterSpacing: '-0.01em', marginBottom: sub ? 2 : 0 }, children: label }), sub && (_jsx("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }, children: sub }))] }), children] }));
}
function PfField({ label, value, onChange, readonly }) {
    return (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 5 }, children: [_jsx("label", { className: "cw-label", children: label }), _jsx("input", { value: value, onChange: onChange ? (e) => onChange(e.target.value) : undefined, readOnly: readonly, style: {
                    padding: '7px 10px',
                    border: '1px solid var(--line)',
                    borderRadius: 'var(--r-sm)',
                    fontFamily: 'var(--font-sans)',
                    fontSize: 13.5,
                    width: '100%',
                    background: readonly ? 'var(--paper)' : 'var(--surface)',
                    outline: 'none',
                    color: readonly ? 'var(--ink-2)' : 'var(--ink)',
                    cursor: readonly ? 'default' : 'text',
                } })] }));
}
function PfToggle({ on, onChange }) {
    return (_jsx("div", { onClick: onChange, style: {
            width: 30,
            height: 17,
            borderRadius: 9,
            background: on ? 'var(--accent)' : 'var(--line)',
            position: 'relative',
            cursor: onChange ? 'pointer' : 'default',
            transition: 'background 150ms ease',
            flexShrink: 0,
        }, children: _jsx("div", { style: {
                position: 'absolute',
                top: 2,
                left: on ? 13 : 2,
                width: 13,
                height: 13,
                borderRadius: '50%',
                background: 'white',
                boxShadow: '0 1px 3px rgba(0,0,0,0.22)',
                transition: 'left 150ms ease',
            } }) }));
}
function SegPicker({ options, value, onChange }) {
    return (_jsx("div", { style: { display: 'flex', gap: 4 }, children: options.map((o) => {
            const active = o === value;
            return (_jsx("span", { onClick: () => onChange(o), style: {
                    padding: '4px 11px',
                    borderRadius: 999,
                    cursor: 'pointer',
                    border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
                    background: active ? 'var(--accent-soft)' : 'var(--surface)',
                    color: active ? 'var(--accent)' : 'var(--ink-2)',
                    fontFamily: 'var(--font-mono)',
                    fontSize: 10.5,
                    fontWeight: active ? 600 : 400,
                    transition: 'all 120ms',
                }, children: o }, o));
        }) }));
}
function SparkBars({ days }) {
    const m = Math.max(...days.map((d) => d.amount));
    const BAR_H = 60;
    return (_jsx("div", { style: { display: 'flex', gap: 7, height: BAR_H + 22, alignItems: 'flex-end' }, children: days.map((d, i) => {
            const isToday = i === days.length - 1;
            return (_jsxs("div", { title: `${d.day} · $${d.amount.toFixed(2)}`, style: { flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 5 }, children: [_jsx("div", { style: {
                            width: '100%',
                            height: `${(d.amount / m) * BAR_H}px`,
                            background: isToday ? 'var(--accent)' : 'var(--line)',
                            borderRadius: '3px 3px 0 0',
                            transition: 'background 120ms',
                        } }), _jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 9, color: 'var(--dim)' }, children: d.day.slice(0, 2) })] }, d.day));
        }) }));
}
function RingGauge({ value, max, label, sub, size = 90 }) {
    const pct = Math.min(value / max, 1);
    const r = (size - 12) / 2;
    const circ = 2 * Math.PI * r;
    const warn = pct > 0.85;
    return (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 8 }, children: [_jsxs("div", { style: { position: 'relative', width: size, height: size }, children: [_jsxs("svg", { width: size, height: size, style: { transform: 'rotate(-90deg)', display: 'block' }, children: [_jsx("circle", { cx: size / 2, cy: size / 2, r: r, fill: "none", stroke: "var(--line)", strokeWidth: 5 }), _jsx("circle", { cx: size / 2, cy: size / 2, r: r, fill: "none", stroke: warn ? 'var(--warn)' : 'var(--accent)', strokeWidth: 5, strokeDasharray: `${circ * pct} ${circ * (1 - pct)}`, strokeLinecap: "round" })] }), _jsxs("div", { style: {
                            position: 'absolute',
                            inset: 0,
                            display: 'flex',
                            flexDirection: 'column',
                            alignItems: 'center',
                            justifyContent: 'center',
                        }, children: [_jsxs("span", { style: {
                                    fontFamily: 'var(--font-mono)',
                                    fontSize: 15,
                                    fontWeight: 700,
                                    letterSpacing: '-0.03em',
                                    lineHeight: 1,
                                }, children: ["$", value.toFixed(2)] }), _jsxs("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 8.5, color: 'var(--dim)' }, children: ["of $", max] })] })] }), _jsxs("div", { style: { textAlign: 'center' }, children: [_jsx("div", { style: { fontSize: 12, fontWeight: 600, letterSpacing: '-0.01em' }, children: label }), _jsx("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }, children: sub })] })] }));
}
const TABS = [
    { id: 'account', label: '账户' },
    { id: 'usage', label: '用量与账单' },
    { id: 'security', label: '安全' },
    { id: 'notifications', label: '通知' },
];
export function ProfilePage() {
    const navigate = useNavigate();
    const userProfile = useChatStore((s) => s.userProfile);
    const logout = useChatStore((s) => s.logout);
    const { allWs } = useWorkspace();
    const displayName = userProfile?.display_name ?? 'You';
    const email = userProfile?.email ?? 'you@acme.co';
    const initial = displayName[0]?.toUpperCase() ?? 'Y';
    const [tab, setTab] = useState('account');
    const [name, setName] = useState(displayName);
    const [model, setModel] = useState('sonnet 4.5');
    const [autonomy, setAuto] = useState('assist');
    const [showKey, setShowKey] = useState(null);
    const [sessions, setSessions] = useState(PF_SESSIONS);
    const [notifs, setNotifs] = useState(PF_NOTIFS);
    const [digestOn, setDigest] = useState(true);
    const toggleN = (event, field) => setNotifs((prev) => prev.map((n) => (n.event === event ? { ...n, [field]: !n[field] } : n)));
    const handleLogout = async () => {
        try {
            await api.auth.logout();
        }
        catch {
            // best-effort
        }
        logout();
        navigate('/home');
    };
    return (_jsxs("div", { className: "flex flex-col h-full", "data-screen-label": "Profile", children: [_jsx(PageHead, { crumbs: [{ label: '首页', to: '/home' }, { label: '个人' }] }), _jsx("div", { className: "flex-1 overflow-y-auto", style: { padding: '28px 32px 52px' }, children: _jsxs("div", { style: { maxWidth: 860, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 18 }, children: [_jsxs("div", { style: {
                                border: '1px solid var(--line)',
                                borderRadius: 'var(--r-lg)',
                                background: 'var(--surface)',
                                padding: '22px 22px 18px',
                                display: 'flex',
                                alignItems: 'flex-start',
                                gap: 20,
                                position: 'relative',
                                overflow: 'hidden',
                            }, children: [_jsx("div", { style: {
                                        position: 'absolute', left: 0, top: 0, bottom: 0, width: 3, background: 'var(--accent)',
                                    } }), _jsx("div", { style: {
                                        width: 62,
                                        height: 62,
                                        borderRadius: '50%',
                                        background: 'var(--accent-soft)',
                                        border: '1.5px solid var(--accent-line)',
                                        display: 'flex',
                                        alignItems: 'center',
                                        justifyContent: 'center',
                                        fontFamily: 'var(--font-display)',
                                        fontStyle: 'italic',
                                        fontSize: 28,
                                        color: 'var(--accent)',
                                        flexShrink: 0,
                                        marginLeft: 8,
                                    }, children: initial }), _jsxs("div", { style: { flex: 1, minWidth: 0 }, children: [_jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 9, marginBottom: 3 }, children: [_jsx("span", { style: {
                                                        fontFamily: 'var(--font-display)',
                                                        fontSize: 22,
                                                        letterSpacing: '-0.01em',
                                                        lineHeight: 1.15,
                                                        color: 'var(--ink)',
                                                    }, children: displayName }), _jsx(Chip, { accent: true, children: "Pro" })] }), _jsx("div", { style: {
                                                fontSize: 13,
                                                color: 'var(--ink-2)',
                                                marginBottom: 14,
                                                fontFamily: 'var(--font-mono)',
                                            }, children: email }), _jsx("div", { style: { display: 'flex', gap: 8, flexWrap: 'wrap' }, children: [
                                                { l: '运行任务数', v: String(PF_STATS.tasksRun) },
                                                { l: '本月 Token', v: PF_STATS.tokensThisMonth },
                                                { l: '工作节点', v: String(PF_STATS.workers) },
                                                { l: '注册时间', v: PF_STATS.memberSince },
                                            ].map((s) => (_jsxs("div", { style: {
                                                    padding: '5px 12px',
                                                    border: '1px solid var(--line)',
                                                    borderRadius: 'var(--r-sm)',
                                                    background: 'var(--paper)',
                                                    display: 'flex',
                                                    flexDirection: 'column',
                                                    gap: 2,
                                                }, children: [_jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }, children: s.l }), _jsx("span", { style: {
                                                            fontWeight: 700,
                                                            fontSize: 14.5,
                                                            fontFamily: 'var(--font-mono)',
                                                            letterSpacing: '-0.03em',
                                                        }, children: s.v })] }, s.l))) })] }), _jsx("button", { className: "cw-btn cw-btn-sm cw-btn-ghost", onClick: () => setTab('account'), children: "\u7F16\u8F91\u8D44\u6599" })] }), _jsx("div", { style: { display: 'flex', borderBottom: '1px solid var(--line)' }, children: TABS.map((t) => (_jsx("button", { onClick: () => setTab(t.id), style: {
                                    padding: '7px 15px',
                                    border: 'none',
                                    borderBottom: `2px solid ${tab === t.id ? 'var(--accent)' : 'transparent'}`,
                                    background: 'transparent',
                                    fontFamily: 'var(--font-sans)',
                                    fontSize: 13,
                                    fontWeight: tab === t.id ? 600 : 400,
                                    color: tab === t.id ? 'var(--ink)' : 'var(--ink-2)',
                                    cursor: 'pointer',
                                    letterSpacing: '-0.005em',
                                    marginBottom: -1,
                                    transition: 'color 120ms, border-color 120ms',
                                    whiteSpace: 'nowrap',
                                }, children: t.label }, t.id))) }), tab === 'account' && (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 14 }, children: [_jsx(PfSection, { title: "\u4E2A\u4EBA\u4FE1\u606F", action: _jsx("button", { className: "cw-btn cw-btn-sm cw-btn-primary", children: "\u4FDD\u5B58\u66F4\u6539" }), children: _jsxs("div", { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }, children: [_jsx(PfField, { label: "\u663E\u793A\u540D\u79F0", value: name, onChange: setName }), _jsx(PfField, { label: "\u90AE\u7BB1\u5730\u5740", value: email, readonly: true }), _jsx(PfField, { label: "\u65F6\u533A", value: "Asia / Shanghai (GMT+8)", readonly: true }), _jsx(PfField, { label: "\u8BED\u8A00", value: "\u7B80\u4F53\u4E2D\u6587", readonly: true })] }) }), _jsx(PfSection, { title: "\u5DE5\u4F5C\u8282\u70B9\u9ED8\u8BA4\u8BBE\u7F6E", children: _jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 13 }, children: [_jsx(PfRow, { label: "\u9ED8\u8BA4\u6A21\u578B", sub: "\u672A\u6307\u5B9A\u6A21\u677F\u65F6\u4F7F\u7528\u7684\u6A21\u578B", children: _jsx(SegPicker, { options: ['haiku 3.5', 'sonnet 4.5', 'opus 4'], value: model, onChange: setModel }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u9ED8\u8BA4\u81EA\u4E3B\u5EA6", sub: "\u5DE5\u4F5C\u8282\u70B9\u65E0\u9700\u786E\u8BA4\u5373\u53EF\u6267\u884C\u7684\u7A0B\u5EA6", children: _jsx(SegPicker, { options: ['辅助', '自动', '完全'], value: autonomy, onChange: setAuto }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u9ED8\u8BA4\u8C03\u5EA6", sub: "\u672A\u88AB\u6A21\u677F\u8986\u76D6\u65F6\u7684\u4EFB\u52A1\u89E6\u53D1\u65B9\u5F0F", children: _jsx(Chip, { children: "\u4E00\u6B21 \u00B7 \u624B\u52A8" }) })] }) }), _jsx(PfSection, { title: "\u5DE5\u4F5C\u7A7A\u95F4", children: _jsx("div", { style: { display: 'flex', flexDirection: 'column', gap: 6 }, children: allWs.map((w) => (_jsxs("div", { style: {
                                                display: 'flex',
                                                alignItems: 'center',
                                                gap: 12,
                                                padding: '9px 12px',
                                                border: '1px solid var(--line-2)',
                                                borderRadius: 'var(--r-sm)',
                                                background: 'var(--paper)',
                                            }, children: [_jsx("span", { style: {
                                                        display: 'inline-flex',
                                                        alignItems: 'center',
                                                        justifyContent: 'center',
                                                        width: 24,
                                                        height: 24,
                                                        borderRadius: 'var(--r-sm)',
                                                        fontFamily: 'var(--font-mono)',
                                                        fontSize: 12,
                                                        fontWeight: 600,
                                                        flexShrink: 0,
                                                        background: w.accent + '14',
                                                        border: `1px solid ${w.accent}55`,
                                                        color: w.accent,
                                                    }, children: w.glyph }), _jsxs("div", { style: { flex: 1, minWidth: 0 }, children: [_jsx("div", { style: { fontSize: 13, fontWeight: 600, letterSpacing: '-0.01em' }, children: w.name }), _jsx("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }, children: w.desc })] }), _jsxs(Chip, { children: [w.fileCount, " \u6587\u4EF6"] }), _jsxs(Chip, { accent: w.taskCount > 1, children: [w.taskCount, " \u6D3B\u8DC3"] })] }, w.id))) }) })] })), tab === 'usage' && (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 14 }, children: [_jsxs("div", { style: {
                                        border: '1px solid var(--line)',
                                        borderRadius: 'var(--r-md)',
                                        background: 'var(--surface)',
                                        padding: '20px 28px',
                                        display: 'flex',
                                        alignItems: 'center',
                                        gap: 32,
                                    }, children: [_jsx(RingGauge, { value: PF_STATS.todaySpend, max: PF_STATS.dailyCap, label: "\u4ECA\u65E5", sub: "\u6BCF\u65E5\u9884\u7B97" }), _jsx("div", { style: { width: 1, background: 'var(--line)', alignSelf: 'stretch' } }), _jsx(RingGauge, { value: PF_STATS.monthSpend, max: PF_STATS.monthlyCap, label: "\u672C\u6708", sub: "\u6708\u5EA6\u9884\u7B97" }), _jsx("div", { style: { flex: 1 } }), _jsx("div", { style: { display: 'flex', flexDirection: 'column', gap: 16, alignItems: 'flex-end' }, children: [
                                                ['每日上限', `$${PF_STATS.dailyCap}`],
                                                ['月度上限', `$${PF_STATS.monthlyCap}`],
                                            ].map(([lbl, v]) => (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-end' }, children: [_jsx("span", { className: "cw-label", children: lbl }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: {
                                                                    fontFamily: 'var(--font-mono)',
                                                                    fontSize: 20,
                                                                    fontWeight: 700,
                                                                    letterSpacing: '-0.04em',
                                                                }, children: v }), _jsx("button", { className: "cw-btn cw-btn-sm", children: "\u4FEE\u6539" })] })] }, lbl))) })] }), _jsx(PfSection, { title: `本周消费 — $${PF_USAGE_WEEK.reduce((a, d) => a + d.amount, 0).toFixed(2)} 合计`, children: _jsx(SparkBars, { days: PF_USAGE_WEEK }) }), _jsx(PfSection, { title: "\u5957\u9910", action: _jsx("button", { className: "cw-btn cw-btn-sm", style: { color: 'var(--accent)' }, children: "\u5347\u7EA7 \u2192" }), children: _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 16 }, children: [_jsxs("div", { style: {
                                                    border: '1.5px solid var(--accent-line)',
                                                    borderRadius: 'var(--r-md)',
                                                    padding: '14px 18px',
                                                    background: 'var(--accent-soft)',
                                                    flex: 1,
                                                }, children: [_jsxs("div", { style: { display: 'flex', alignItems: 'baseline', gap: 10, marginBottom: 10 }, children: [_jsx("span", { style: {
                                                                    fontFamily: 'var(--font-display)',
                                                                    fontSize: 22,
                                                                    color: 'var(--accent)',
                                                                    fontStyle: 'italic',
                                                                }, children: PF_PLAN.name }), _jsxs("span", { style: {
                                                                    fontFamily: 'var(--font-mono)',
                                                                    fontSize: 13,
                                                                    color: 'var(--ink-2)',
                                                                    fontWeight: 600,
                                                                }, children: ["$", PF_PLAN.pricePerMonth, " / \u6708"] }), _jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 10, color: 'var(--dim)' }, children: PF_PLAN.renewsLabel })] }), _jsx("div", { style: { display: 'flex', gap: 6, flexWrap: 'wrap' }, children: PF_PLAN.features.map((f) => (_jsx(Chip, { children: f }, f))) })] }), _jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 6, flexShrink: 0 }, children: [_jsx("button", { className: "cw-btn cw-btn-sm", children: "\u8D26\u5355\u95E8\u6237 \u2192" }), _jsx("button", { className: "cw-btn cw-btn-sm cw-btn-ghost", style: { fontSize: 11, color: 'var(--dim)' }, children: "\u4E0B\u8F7D\u53D1\u7968" })] })] }) })] })), tab === 'security' && (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 14 }, children: [_jsx(PfSection, { title: "API \u5BC6\u94A5", action: _jsx("button", { className: "cw-btn cw-btn-sm cw-btn-primary", children: "+ \u65B0\u5EFA\u5BC6\u94A5" }), children: _jsxs("div", { style: { display: 'flex', flexDirection: 'column' }, children: [_jsx("div", { style: {
                                                    display: 'grid',
                                                    gridTemplateColumns: '1.3fr 1fr 0.65fr 0.65fr auto',
                                                    gap: '6px 14px',
                                                    padding: '5px 4px 9px',
                                                    borderBottom: '1px solid var(--line-2)',
                                                }, children: ['名称', '前缀', '创建时间', '最近使用', ''].map((h) => (_jsx("span", { className: "cw-label", children: h }, h))) }), PF_API_KEYS.map((k) => (_jsxs("div", { style: {
                                                    display: 'grid',
                                                    gridTemplateColumns: '1.3fr 1fr 0.65fr 0.65fr auto',
                                                    gap: '6px 14px',
                                                    padding: '10px 4px',
                                                    borderBottom: '1px solid var(--line-2)',
                                                    alignItems: 'center',
                                                }, children: [_jsxs("div", { children: [_jsx("div", { style: { fontSize: 13, fontWeight: 500 }, children: k.name }), _jsx("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 9.5, color: 'var(--dim)' }, children: k.scopes })] }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 6 }, children: [_jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 11.5, color: 'var(--ink-2)' }, children: showKey === k.name ? 'cw_live_4xKm3••••••' : k.prefix }), _jsx("span", { onClick: () => setShowKey(showKey === k.name ? null : k.name), style: { fontSize: 10, color: 'var(--dim)', cursor: 'pointer', userSelect: 'none' }, children: showKey === k.name ? '隐藏' : '显示' })] }), _jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--dim)' }, children: k.created }), _jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--ok)' }, children: k.lastUsed }), _jsx("button", { className: "cw-btn cw-btn-sm cw-btn-ghost", style: { fontSize: 11, color: 'var(--dim)', padding: '3px 8px' }, children: "\u64A4\u9500" })] }, k.name)))] }) }), _jsx(PfSection, { title: "\u6D3B\u8DC3\u4F1A\u8BDD", action: _jsx("button", { className: "cw-btn cw-btn-sm", style: { color: 'var(--accent)' }, onClick: () => setSessions((p) => p.filter((s) => s.current)), children: "\u767B\u51FA\u5176\u4ED6\u8BBE\u5907" }), children: _jsx("div", { style: { display: 'flex', flexDirection: 'column', gap: 8 }, children: sessions.map((s) => (_jsxs("div", { style: {
                                                display: 'flex',
                                                alignItems: 'center',
                                                gap: 12,
                                                padding: '10px 12px',
                                                border: `1px solid ${s.current ? 'var(--accent-line)' : 'var(--line-2)'}`,
                                                borderRadius: 'var(--r-sm)',
                                                background: s.current ? 'var(--accent-soft)' : 'var(--paper)',
                                            }, children: [_jsx("div", { style: {
                                                        width: 32,
                                                        height: 32,
                                                        border: '1px solid var(--line)',
                                                        borderRadius: 'var(--r-sm)',
                                                        background: 'var(--surface)',
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        justifyContent: 'center',
                                                        fontFamily: 'var(--font-mono)',
                                                        fontSize: 14,
                                                        color: s.current ? 'var(--accent)' : 'var(--dim)',
                                                        flexShrink: 0,
                                                    }, children: s.device.includes('iOS') ? '▣' : '□' }), _jsxs("div", { style: { flex: 1, minWidth: 0 }, children: [_jsxs("div", { style: {
                                                                fontSize: 13,
                                                                fontWeight: 500,
                                                                display: 'flex',
                                                                alignItems: 'center',
                                                                gap: 8,
                                                            }, children: [s.device, s.current && (_jsx("span", { style: {
                                                                        fontFamily: 'var(--font-mono)',
                                                                        fontSize: 9,
                                                                        color: 'var(--accent)',
                                                                        letterSpacing: '0.06em',
                                                                    }, children: "\u5F53\u524D\u8BBE\u5907" }))] }), _jsxs("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }, children: [s.location, " \u00B7 ", s.time] })] }), !s.current && (_jsx("button", { className: "cw-btn cw-btn-sm cw-btn-ghost", style: { fontSize: 11, color: 'var(--dim)' }, onClick: () => setSessions((p) => p.filter((x) => x.device !== s.device || x.time !== s.time)), children: "\u767B\u51FA" }))] }, s.device + s.time))) }) }), _jsx(PfSection, { title: "\u8EAB\u4EFD\u9A8C\u8BC1", children: _jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 13 }, children: [_jsx(PfRow, { label: "\u5BC6\u7801", sub: "3 \u4E2A\u6708\u524D\u4FEE\u6539", children: _jsx("button", { className: "cw-btn cw-btn-sm", children: "\u4FEE\u6539\u5BC6\u7801" }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u53CC\u56E0\u7D20\u8BA4\u8BC1", sub: "\u5DF2\u7ED1\u5B9A\u8EAB\u4EFD\u9A8C\u8BC1\u5668\u5E94\u7528", children: _jsx(Chip, { accent: true, children: "\u5DF2\u542F\u7528" }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u901A\u884C\u5BC6\u94A5", sub: "\u65E0\u9700\u5BC6\u7801\u767B\u5F55 \u2014 \u4F7F\u7528\u8BBE\u5907\u751F\u7269\u8BC6\u522B", children: _jsx("button", { className: "cw-btn cw-btn-sm", children: "\u6DFB\u52A0\u901A\u884C\u5BC6\u94A5" }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u9000\u51FA\u767B\u5F55", sub: "\u6E05\u9664\u672C\u5730\u4F1A\u8BDD\u5E76\u8FD4\u56DE\u767B\u5F55\u9875", children: _jsx("button", { className: "cw-btn cw-btn-sm", style: { color: 'var(--accent)' }, onClick: handleLogout, children: "\u767B\u51FA" }) })] }) })] })), tab === 'notifications' && (_jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 14 }, children: [_jsx(PfSection, { title: "\u4E8B\u4EF6\u901A\u77E5", children: _jsxs("div", { style: { display: 'flex', flexDirection: 'column' }, children: [_jsxs("div", { style: {
                                                    display: 'grid',
                                                    gridTemplateColumns: '1fr 58px 58px',
                                                    padding: '5px 4px 9px',
                                                    borderBottom: '1px solid var(--line-2)',
                                                }, children: [_jsx("span", { className: "cw-label", children: "\u4E8B\u4EF6" }), _jsx("span", { className: "cw-label", style: { textAlign: 'center' }, children: "\u90AE\u4EF6" }), _jsx("span", { className: "cw-label", style: { textAlign: 'center' }, children: "\u63A8\u9001" })] }), notifs.map((n) => (_jsxs("div", { style: {
                                                    display: 'grid',
                                                    gridTemplateColumns: '1fr 58px 58px',
                                                    padding: '11px 4px',
                                                    borderBottom: '1px solid var(--line-2)',
                                                    alignItems: 'center',
                                                }, children: [_jsxs("div", { children: [_jsx("div", { style: { fontSize: 13, fontWeight: 500 }, children: n.event }), _jsx("div", { style: { fontFamily: 'var(--font-mono)', fontSize: 10.5, color: 'var(--dim)' }, children: n.sub })] }), _jsx("div", { style: { display: 'flex', justifyContent: 'center' }, children: _jsx(PfToggle, { on: n.email, onChange: () => toggleN(n.event, 'email') }) }), _jsx("div", { style: { display: 'flex', justifyContent: 'center' }, children: _jsx(PfToggle, { on: n.push, onChange: () => toggleN(n.event, 'push') }) })] }, n.event)))] }) }), _jsx(PfSection, { title: "\u6BCF\u5468\u6458\u8981", children: _jsxs("div", { style: { display: 'flex', flexDirection: 'column', gap: 13 }, children: [_jsx(PfRow, { label: "\u53D1\u9001\u6BCF\u5468\u62A5\u544A", sub: "\u4EFB\u52A1\u3001\u6D88\u8D39\u4E0E\u5DE5\u4F5C\u8282\u70B9\u6C47\u603B \u2014 \u6BCF\u5468\u4E00\u4E0A\u5348 9 \u70B9", children: _jsx(PfToggle, { on: digestOn, onChange: () => setDigest((v) => !v) }) }), _jsx("div", { className: "cw-divider" }), _jsx(PfRow, { label: "\u6458\u8981\u6536\u4EF6\u5730\u5740", sub: "\u6458\u8981\u90AE\u4EF6\u53D1\u9001\u81F3", children: _jsx("span", { style: { fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--ink-2)' }, children: email }) })] }) })] }))] }) })] }));
}
