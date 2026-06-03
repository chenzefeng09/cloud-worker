import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useEffect, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { StatusDot } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { api, ApiError } from '../api/client';
import { formatRelativeTimeZh } from '../lib/format';
import { FLEET_TOPOLOGY, } from '../lib/designFixtures';
// ── Topology helpers ──────────────────────────────────────────────
function statusStroke(s) {
    switch (s) {
        case 'thinking': return '#d23a1f'; // accent
        case 'awaiting': return '#c68a17'; // warn
        case 'paused': return '#75756f'; // dim
        case 'done': return '#2c8f5d'; // ok
        default: return '#d8d4c6'; // line (idle)
    }
}
// ── Static topology canvas (DESIGN FIXTURE — remove when backend provides topology) ──
function TopologyCanvas() {
    const { workers, tasks, mcps, links } = FLEET_TOPOLOGY;
    // Build lookup maps by id
    const workerById = Object.fromEntries(workers.map(w => [w.id, w]));
    const taskById = Object.fromEntries(tasks.map(t => [t.id, t]));
    const mcpById = Object.fromEntries(mcps.map(m => [m.id, m]));
    return (_jsxs("div", { style: { position: 'relative', overflow: 'hidden', background: '#f7f5ee' }, className: "flex-1 min-h-0", children: [_jsx("div", { "aria-hidden": true, style: {
                    position: 'absolute', inset: 0,
                    backgroundImage: 'linear-gradient(#d8d4c6 1px, transparent 1px), linear-gradient(90deg, #d8d4c6 1px, transparent 1px)',
                    backgroundSize: '32px 32px',
                    opacity: 0.35,
                    pointerEvents: 'none',
                } }), _jsx("svg", { viewBox: "0 0 800 460", preserveAspectRatio: "xMidYMid meet", style: { position: 'absolute', inset: 0, width: '100%', height: '100%' }, children: links.map((link, i) => {
                    const a = workerById[link.fromWorker];
                    const b = link.toTask ? taskById[link.toTask] : link.toMcp ? mcpById[link.toMcp] : null;
                    if (!a || !b)
                        return null;
                    const live = !!link.live;
                    const pathD = `M${a.x},${a.y} L${b.x},${b.y}`;
                    return (_jsxs("g", { children: [_jsx("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: live ? '#d23a1f' : '#d8d4c6', strokeWidth: live ? 1.5 : 1, strokeDasharray: live ? '0' : '3,4', opacity: live ? 0.85 : 0.55 }), live && (_jsx("circle", { r: "3", fill: "#d23a1f", children: _jsx("animateMotion", { dur: "2.4s", repeatCount: "indefinite", path: pathD }) }))] }, i));
                }) }), tasks.map(t => (_jsxs("div", { style: {
                    position: 'absolute',
                    left: `calc(${(t.x / 800) * 100}% - 75px)`,
                    top: `calc(${(t.y / 460) * 100}% - 18px)`,
                    width: 150,
                    background: '#fffdf7',
                    border: '1px dashed #d8d4c6',
                    borderRadius: 4,
                    padding: '5px 8px',
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    fontSize: 11,
                    display: 'flex',
                    alignItems: 'center',
                    gap: 6,
                    zIndex: 1,
                }, children: [_jsx("span", { style: { color: '#75756f' }, children: "\u25A4" }), _jsx("span", { style: { flex: 1, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }, children: t.label })] }, t.id))), mcps.map(m => (_jsxs("div", { style: {
                    position: 'absolute',
                    left: `calc(${(m.x / 800) * 100}% - 50px)`,
                    top: `calc(${(m.y / 460) * 100}% - 14px)`,
                    width: 100,
                    background: '#f0ede3',
                    border: '1px solid #d8d4c6',
                    borderRadius: 999,
                    padding: '3px 8px',
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    fontSize: 10.5,
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: 5,
                    zIndex: 1,
                }, children: [_jsx("span", { style: { color: m.mcpStatus === 'auth needed' ? '#d23a1f' : '#75756f' }, children: "\u229E" }), _jsx("span", { children: m.label })] }, m.id))), workers.map(w => {
                const active = w.status === 'thinking';
                const stroke = statusStroke(w.status);
                return (_jsxs("div", { style: {
                        position: 'absolute',
                        left: `calc(${(w.x / 800) * 100}% - 32px)`,
                        top: `calc(${(w.y / 460) * 100}% - 32px)`,
                        width: 64,
                        height: 64,
                        borderRadius: '50%',
                        background: '#fffdf7',
                        border: `1.5px solid ${stroke}`,
                        boxShadow: active ? '0 0 0 6px rgba(210,58,31,0.10)' : 'none',
                        display: 'flex',
                        flexDirection: 'column',
                        alignItems: 'center',
                        justifyContent: 'center',
                        zIndex: 2,
                    }, children: [_jsx("span", { style: {
                                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                fontSize: 18,
                                fontWeight: 600,
                                color: active ? '#d23a1f' : '#3c3d40',
                                letterSpacing: '-0.05em',
                            }, children: w.glyph }), _jsx("span", { style: {
                                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                fontSize: 10,
                                color: '#75756f',
                            }, children: w.id }), _jsx("span", { className: active ? 'cw-dot animate-blip' : 'cw-dot', style: {
                                position: 'absolute',
                                bottom: 3,
                                right: 3,
                                background: stroke,
                                border: '2px solid #fffdf7',
                            } })] }, w.id));
            }), _jsxs("div", { style: {
                    position: 'absolute',
                    left: 16,
                    bottom: 16,
                    background: '#fffdf7',
                    border: '1px solid #d8d4c6',
                    borderRadius: 4,
                    padding: 10,
                    display: 'flex',
                    flexDirection: 'column',
                    gap: 5,
                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                    fontSize: 10.5,
                    zIndex: 3,
                }, children: [_jsx("div", { className: "cw-label", style: { marginBottom: 2 }, children: "\u56FE\u4F8B" }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: { display: 'inline-block', width: 22, height: 1.5, background: '#d23a1f' } }), _jsx("span", { children: "\u6D3B\u8DC3\u94FE\u63A5" })] }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: { display: 'inline-block', width: 22, height: 0, borderTop: '1px dashed #d8d4c6' } }), _jsx("span", { children: "\u7A7A\u95F2\u94FE\u63A5" })] }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: { display: 'inline-block', width: 12, height: 12, borderRadius: '50%', border: '1px solid #3c3d40' } }), _jsx("span", { children: "\u8282\u70B9" })] }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: { display: 'inline-block', width: 16, height: 9, border: '1px dashed #3c3d40', borderRadius: 2 } }), _jsx("span", { children: "\u4EFB\u52A1" })] }), _jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 8 }, children: [_jsx("span", { style: { display: 'inline-block', width: 16, height: 9, border: '1px solid #3c3d40', borderRadius: 99 } }), _jsx("span", { children: "\u5DE5\u5177" })] })] })] }));
}
// ── Main page ─────────────────────────────────────────────────────
export function FleetPage() {
    const [pool, setPool] = useState(null);
    const [sandboxes, setSandboxes] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const fetchAll = () => {
        setLoading(true);
        setError(null);
        let cancelled = false;
        const run = async () => {
            try {
                const [poolRes, sbRes] = await Promise.all([
                    api.fleet.poolSnapshot(),
                    api.fleet.listSandboxes(),
                ]);
                if (cancelled)
                    return;
                setPool(poolRes);
                setSandboxes(sbRes.items);
            }
            catch (e) {
                if (cancelled)
                    return;
                setError(e instanceof ApiError ? e.message : '加载工作节点数据失败，请稍后重试。');
            }
            finally {
                if (!cancelled)
                    setLoading(false);
            }
        };
        run();
        return () => { cancelled = true; };
    };
    useEffect(fetchAll, []);
    const totalSandboxes = pool?.total ?? 0;
    const idleTotal = pool
        ? Object.values(pool.by_template).reduce((sum, t) => sum + t.idle, 0)
        : 0;
    const busyTotal = totalSandboxes - idleTotal;
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [{ label: '首页', to: '/home' }, { label: '工作节点' }], actions: _jsx("button", { className: "cw-btn cw-btn-sm", onClick: fetchAll, children: "\u5237\u65B0" }) }), _jsxs("div", { className: "flex-1 min-h-0", style: { display: 'grid', gridTemplateColumns: '1fr 280px' }, children: [_jsx("div", { className: "flex flex-col min-h-0", children: _jsx(TopologyCanvas, {}) }), _jsxs("aside", { className: "flex flex-col min-h-0", style: { borderLeft: '1px solid #d8d4c6', background: '#f7f5ee' }, children: [_jsxs("div", { style: { padding: 16, borderBottom: '1px solid #e8e4d8' }, children: [_jsx("div", { className: "cw-label", style: { marginBottom: 8 }, children: "\u5DE5\u4F5C\u8282\u70B9" }), error && (_jsx("div", { className: "font-mono text-[11px]", style: { color: '#d23a1f', marginBottom: 8 }, children: error })), _jsxs("div", { style: { display: 'flex', alignItems: 'baseline', gap: 6 }, children: [_jsx("span", { style: {
                                                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                                    fontSize: 28,
                                                    fontWeight: 600,
                                                    letterSpacing: '-0.02em',
                                                }, children: loading ? '…' : totalSandboxes }), _jsx("span", { className: "cw-label", style: { fontSize: 12, letterSpacing: 0 }, children: "\u6C99\u7BB1" })] }), !loading && (_jsxs("div", { style: { display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }, children: [busyTotal > 0 && _jsxs(Chip, { accent: true, children: ["\u25CF ", busyTotal, " \u8FD0\u884C\u4E2D"] }), idleTotal > 0 && _jsxs(Chip, { dashed: true, children: [idleTotal, " \u7A7A\u95F2"] }), totalSandboxes === 0 && _jsx(Chip, { dashed: true, children: "\u65E0\u6D3B\u8DC3\u6C99\u7BB1" })] }))] }), !loading && pool && Object.keys(pool.by_template).length > 0 && (_jsxs("div", { style: { padding: '12px 16px', borderBottom: '1px solid #e8e4d8' }, children: [_jsx("div", { className: "cw-label", style: { marginBottom: 8 }, children: "\u6A21\u677F\u6C60" }), _jsx("div", { style: { display: 'flex', flexDirection: 'column', gap: 6 }, children: Object.entries(pool.by_template).map(([tmpl, info]) => (_jsxs("div", { style: {
                                                display: 'grid',
                                                gridTemplateColumns: '1fr auto auto',
                                                gap: 8,
                                                alignItems: 'center',
                                                padding: '6px 10px',
                                                border: '1px solid #d8d4c6',
                                                borderRadius: 4,
                                                background: '#fffdf7',
                                            }, children: [_jsx("span", { style: {
                                                        fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                                        fontSize: 11,
                                                        fontWeight: 600,
                                                        overflow: 'hidden',
                                                        whiteSpace: 'nowrap',
                                                        textOverflow: 'ellipsis',
                                                    }, children: tmpl }), _jsxs(Chip, { children: [info.idle, " \u7A7A\u95F2"] }), _jsx("span", { className: "cw-label", style: { fontSize: 10 }, children: info.total })] }, tmpl))) })] })), _jsxs("div", { className: "cw-scroll flex-1 min-h-0", children: [loading && (_jsx("div", { style: { padding: '12px 16px' }, className: "cw-label", children: "\u52A0\u8F7D\u4E2D\u2026" })), !loading && sandboxes.length > 0 && (_jsxs(_Fragment, { children: [_jsx("div", { style: { padding: '10px 16px 4px' }, children: _jsxs("div", { className: "cw-label", children: ["\u8FD0\u884C\u4E2D\u7684\u8282\u70B9 \u00B7 ", sandboxes.length] }) }), sandboxes.map((sb, idx) => {
                                                const sbId = sb.container_id || sb.id || '';
                                                const isRunning = sb.status?.toLowerCase() === 'running';
                                                return (_jsxs("div", { style: {
                                                        padding: '10px 16px',
                                                        borderBottom: '1px solid #e8e4d8',
                                                        display: 'flex',
                                                        alignItems: 'center',
                                                        gap: 10,
                                                    }, children: [_jsx("span", { className: `cw-avatar${isRunning ? ' accent' : ''}`, children: (sb.name || sbId).slice(0, 1).toUpperCase() }), _jsxs("div", { style: { flex: 1, minWidth: 0 }, children: [_jsxs("div", { style: { display: 'flex', alignItems: 'center', gap: 6 }, children: [_jsx("span", { style: {
                                                                                fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                                                                fontSize: 11.5,
                                                                                fontWeight: 600,
                                                                                overflow: 'hidden',
                                                                                whiteSpace: 'nowrap',
                                                                                textOverflow: 'ellipsis',
                                                                            }, children: sb.name || (sbId ? sbId.slice(0, 12) : '—') }), _jsx(StatusDot, { status: isRunning ? 'thinking' : 'idle', label: sb.status })] }), sb.session_id && (_jsxs("div", { className: "cw-label", style: { marginTop: 2, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }, children: ["\u4F1A\u8BDD \u00B7 ", sb.session_id.slice(0, 12)] })), sb.created_at && (_jsx("div", { className: "cw-label", style: { marginTop: 2 }, children: formatRelativeTimeZh(sb.created_at) }))] })] }, sbId || idx));
                                            })] })), !loading && sandboxes.length === 0 && !error && (_jsx("div", { style: { padding: '12px 16px' }, children: _jsx("span", { className: "cw-label", style: { fontStyle: 'italic' }, children: "\u6682\u65E0\u8FD0\u884C\u4E2D\u7684\u5DE5\u4F5C\u8282\u70B9" }) }))] }), _jsxs("div", { style: { padding: 14, borderTop: '1px solid #d8d4c6' }, children: [_jsx("div", { className: "cw-label", style: { marginBottom: 6 }, children: "\u4ECA\u65E5\u8D39\u7528" }), _jsxs("div", { style: { display: 'flex', alignItems: 'baseline', gap: 6 }, children: [_jsx("span", { style: {
                                                    fontFamily: "'JetBrains Mono', ui-monospace, monospace",
                                                    fontSize: 22,
                                                    fontWeight: 600,
                                                }, children: "$4.21" }), _jsx("span", { className: "cw-label", style: { letterSpacing: 0 }, children: "/ $50 \u4E0A\u9650" })] }), _jsx("div", { style: {
                                            height: 3,
                                            background: '#e8e4d8',
                                            borderRadius: 2,
                                            overflow: 'hidden',
                                            marginTop: 6,
                                        }, children: _jsx("div", { style: { width: '8.4%', height: '100%', background: '#d23a1f' } }) })] })] })] })] }));
}
