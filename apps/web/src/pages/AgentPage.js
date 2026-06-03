import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { Chip } from '../components/shell/Chip';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusZh } from '../lib/format';
import { useWorkspace } from '../lib/workspaceContext';
import { SHOW_DESIGN_FIXTURES, PLAN_STEPS, AGENT_SKILLS, AGENT_WORKER, AGENT_LIVE_STATS, } from '../lib/designFixtures';
/**
 * Single-task agent view. The URL `/tasks/:id` drives `activeSessionId` in the
 * chat store so the existing `ChatWindow` / `useChat` SSE pipeline (B-M1) keeps
 * working unchanged — this page is essentially a Chinese-labelled shell around
 * those primitives plus a thin context rail.
 *
 * Friendly mode: add `?mode=friendly` to the URL.
 */
export function AgentPage() {
    const { id } = useParams();
    const navigate = useNavigate();
    const location = useLocation();
    const [searchParams] = useSearchParams();
    const friendly = searchParams.get('mode') === 'friendly';
    const { ws, files: wsFiles } = useWorkspace();
    const { sessions } = useSessions();
    const activeSessionId = useChatStore((s) => s.activeSessionId);
    const setActiveSession = useChatStore((s) => s.setActiveSession);
    const { sendMessage, cancel, isStreaming } = useChat();
    // Friendly defaults rail hidden; non-friendly defaults rail shown.
    const [showRail, setShowRail] = useState(!friendly);
    // Keep the chat store's active session in sync with the URL. This is what
    // lets useChat() pick up the right transcript without changes to the hook.
    useEffect(() => {
        if (id && id !== activeSessionId)
            setActiveSession(id);
    }, [id, activeSessionId, setActiveSession]);
    // If HomePage routed us here with an `initialMessage` in state, dispatch it
    // once the chat store has bound to this session.  Guard with a ref so React
    // Strict Mode's double-effect doesn't double-send.
    const sentInitialRef = useRef(null);
    useEffect(() => {
        if (!id || activeSessionId !== id)
            return;
        const initial = location.state?.initialMessage?.trim();
        if (!initial)
            return;
        if (sentInitialRef.current === id)
            return;
        sentInitialRef.current = id;
        sendMessage(initial, []);
        // Clear router state so a refresh won't re-send.
        navigate(location.pathname, { replace: true, state: null });
    }, [id, activeSessionId, location.pathname, location.state, navigate, sendMessage]);
    const session = sessions.find((s) => s.id === id);
    if (!id) {
        return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [{ label: '首页', to: '/home' }, { label: '任务' }] }), _jsx("div", { className: "flex-1 flex items-center justify-center text-dim", children: "\u672A\u6307\u5B9A\u4EFB\u52A1\u3002" })] }));
    }
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [
                    { label: '首页', to: '/home' },
                    { label: friendly ? 'Chat' : '任务', to: friendly ? '/home' : '/tasks' },
                    { label: session?.title || '未命名任务' },
                ], status: session && (_jsxs("span", { className: "font-mono text-[11px] text-ink-2", children: ["\u00B7 ", sessionStatusZh(session.status)] })), actions: friendly ? (_jsxs(_Fragment, { children: [_jsxs("button", { onClick: () => setShowRail((v) => !v), className: "cw-btn cw-btn-ghost cw-btn-sm", title: "Toggle technical context \u2014 plan, files, worker stats", children: [_jsx("span", { className: "font-mono", children: showRail ? '◧' : '◦' }), " details"] }), _jsx("button", { className: "cw-btn cw-btn-sm", children: "pause" })] })) : (_jsxs(_Fragment, { children: [_jsxs("button", { onClick: () => navigate('/files'), className: "cw-btn cw-btn-ghost cw-btn-sm", children: [_jsx("span", { className: "font-mono", children: "\u2317" }), " \u6587\u4EF6"] }), isStreaming && (_jsx("button", { onClick: cancel, className: "cw-btn cw-btn-sm", style: { color: '#d23a1f', borderColor: '#f5c2ba', background: '#fef2f0' }, children: "\u505C\u6B62" }))] })) }), _jsxs("div", { className: "min-h-0 flex-1", style: {
                    display: 'grid',
                    gridTemplateColumns: showRail ? '200px 1fr' : '1fr',
                }, children: [showRail && (_jsxs("aside", { className: "border-r border-line p-3 flex flex-col gap-4 bg-paper cw-scroll", style: { overflow: 'hidden' }, children: [_jsxs("div", { children: [_jsx("div", { className: "cw-label mb-1.5", children: "\u5DE5\u4F5C\u533A" }), _jsxs("div", { onClick: () => navigate('/workspace'), className: "flex items-center gap-2 px-2 py-1.5 bg-surface rounded-sm cursor-pointer", style: {
                                            border: `1px solid ${ws.accent}33`,
                                            borderLeft: `2px solid ${ws.accent}`,
                                            borderRadius: 'var(--r-sm)',
                                        }, children: [_jsx("span", { className: "cw-ws-chip sm shrink-0", style: {
                                                    background: ws.accent + '14',
                                                    borderColor: ws.accent + '55',
                                                    color: ws.accent,
                                                }, children: ws.glyph }), _jsxs("div", { className: "min-w-0 flex-1", children: [_jsx("div", { className: "text-[12.5px] font-semibold tracking-[-0.01em] truncate", children: ws.name }), _jsxs("div", { className: "font-mono text-[10px] text-dim", children: [wsFiles.length, " base files"] })] })] }), wsFiles.slice(0, 4).length > 0 && (_jsxs("div", { className: "flex flex-col gap-0.5 mt-1.5 pl-1", children: [wsFiles.slice(0, 4).map((f) => (_jsxs("div", { className: "flex items-center gap-1.5 px-1 font-mono text-[10.5px] text-ink-2 min-w-0", children: [_jsx("span", { style: { color: ws.accent, fontSize: 9 }, children: "\u25CF" }), _jsx("span", { className: "whitespace-nowrap overflow-hidden text-ellipsis flex-1", children: f.n })] }, f.n))), wsFiles.length === 0 && (_jsx("span", { className: "font-mono text-[11px] italic text-dim", children: "\u65E0\u57FA\u7840\u6587\u4EF6" }))] }))] }), SHOW_DESIGN_FIXTURES && (_jsxs("div", { children: [_jsx("div", { className: "cw-label mb-1.5", children: "Worker" }), _jsxs("div", { className: "flex items-center gap-2 px-2 py-1.5 bg-surface border border-line", style: { borderRadius: 'var(--r-sm)' }, children: [_jsx("span", { className: "cw-avatar accent lg shrink-0", children: AGENT_WORKER.glyph }), _jsxs("div", { className: "min-w-0 flex-1", children: [_jsx("div", { className: "font-mono text-[12px] font-semibold", children: AGENT_WORKER.id }), _jsxs("div", { className: "font-mono text-[10px] text-dim", children: ["since ", AGENT_WORKER.ago, " ago"] })] })] })] })), SHOW_DESIGN_FIXTURES && (_jsxs("div", { children: [_jsxs("div", { className: "cw-label mb-1.5", children: ["Skills & uploads \u00B7 ", AGENT_SKILLS.length] }), _jsx("div", { className: "flex flex-col gap-1", children: AGENT_SKILLS.map((s) => (_jsx(Chip, { dashed: s.dashed, children: s.label }, s.label))) })] })), SHOW_DESIGN_FIXTURES && (_jsxs("div", { children: [_jsxs("div", { className: "cw-label mb-1.5", children: ["Plan \u00B7 ", PLAN_STEPS.length, " steps"] }), _jsx("div", { className: "flex flex-col gap-0.5", children: PLAN_STEPS.map((s) => (_jsxs("div", { className: "flex items-center gap-2 px-1.5 py-1", style: {
                                                borderRadius: 4,
                                                background: s.live ? 'var(--accent-soft)' : 'transparent',
                                            }, children: [_jsx("span", { className: "inline-flex items-center justify-center shrink-0 font-mono", style: {
                                                        width: 14,
                                                        height: 14,
                                                        borderRadius: 3,
                                                        border: `1px solid ${s.d ? 'var(--ink)' : s.live ? 'var(--accent)' : 'var(--line)'}`,
                                                        background: s.d ? 'var(--ink)' : 'transparent',
                                                        color: 'var(--paper)',
                                                        fontSize: 9,
                                                    }, children: s.d ? '✓' : '' }), _jsx("span", { className: "text-[12px]", style: {
                                                        color: s.live ? 'var(--accent)' : s.d ? 'var(--dim)' : 'var(--ink)',
                                                        textDecoration: s.d ? 'line-through' : 'none',
                                                        fontWeight: s.live ? 600 : 400,
                                                    }, children: s.t })] }, s.n))) })] })), _jsx("div", { className: "flex-1" }), _jsxs("div", { children: [_jsx("div", { className: "cw-label mb-1.5", children: "Live" }), isStreaming && (_jsxs("div", { className: "flex items-center gap-1.5 mb-1.5 px-2 py-1 border border-dashed border-accent-line bg-accent-soft rounded-sm", children: [_jsx("span", { className: "w-1.5 h-1.5 rounded-full bg-accent animate-blip shrink-0" }), _jsx("span", { className: "font-mono text-[10.5px] text-accent", children: "\u8FD0\u884C\u4E2D\u2026" })] })), SHOW_DESIGN_FIXTURES && (_jsxs("div", { className: "font-mono text-[10.5px] text-ink-2 leading-[1.6]", children: [_jsxs("div", { children: ["elapsed \u00B7 ", AGENT_LIVE_STATS.elapsed] }), _jsxs("div", { children: ["tokens \u00B7 ", AGENT_LIVE_STATS.tokens] }), _jsxs("div", { children: ["cost \u00B7 ", AGENT_LIVE_STATS.cost] })] }))] })] })), _jsxs("section", { className: "flex flex-col min-w-0 min-h-0 bg-surface", children: [friendly && (_jsx("div", { className: "px-8 pt-5", children: _jsxs("div", { className: "flex items-center gap-2.5 px-3.5 py-2.5 text-[12.5px] text-ink-2", style: {
                                        border: '1px dashed var(--line)',
                                        borderRadius: 'var(--r-md)',
                                        background: 'var(--paper)',
                                        maxWidth: 780,
                                        margin: '0 auto',
                                    }, children: [_jsx("span", { className: "cw-avatar accent shrink-0 font-mono", style: { width: 22, height: 22 }, children: "\u25C6" }), _jsxs("div", { className: "flex-1 leading-[1.45]", children: [_jsx("b", { className: "text-ink font-semibold", children: "Friendly mode." }), " I'll keep the chat warm and tuck technical work (tool calls, file edits, terminal output) into collapsed rows you can open if you're curious."] }), _jsxs("button", { className: "cw-btn cw-btn-ghost cw-btn-sm shrink-0", onClick: () => setShowRail((v) => !v), children: [_jsx("span", { className: "font-mono", children: showRail ? '−' : '+' }), " details"] })] }) })), _jsx(ChatWindow, { sessionId: id, friendly: friendly })] })] })] }));
}
