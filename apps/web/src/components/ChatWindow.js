import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useRef } from 'react';
import { useChat } from '../hooks/useChat';
import { MessageBubble } from './MessageBubble';
import { InputBar } from './InputBar';
import { api } from '../api/client';
import { SHOW_DESIGN_FIXTURES, AGENT_TRANSCRIPT_FIXTURE, } from '../lib/designFixtures';
// ── Design-only fixture renderer ──────────────────────────────────────────────
// Renders AGENT_TRANSCRIPT_FIXTURE when there are no real messages yet.
// DESIGN FIXTURE — delete this component (and its usage below) once the backend
// provides real transcript data.
function FixtureMessage({ m, friendly, }) {
    return _jsx(FixtureMessageInner, { m: m, friendly: friendly });
}
import { useState } from 'react';
function FixtureMessageInner({ m, friendly, }) {
    const [open, setOpen] = useState(() => {
        // thoughts + tool groups: collapsed by default in friendly mode
        if (m.from === 'agent' && (m.kind === 'thought' || m.kind === 'tools')) {
            return !friendly;
        }
        return true;
    });
    const [toolExpanded, setToolExpanded] = useState({});
    const avatar = (_jsx("span", { className: "cw-avatar accent lg shrink-0 font-mono", style: { marginTop: 2 }, children: "\u25C6" }));
    if (m.from === 'user') {
        return (_jsxs("div", { className: "flex gap-3 items-start", children: [_jsx("span", { className: "cw-avatar shrink-0", style: { marginTop: 2 }, children: "Y" }), _jsx("div", { className: "flex-1 text-[14px] leading-[1.55] tracking-[-0.005em]", style: {
                        background: 'var(--paper-2)',
                        padding: '10px 14px',
                        borderRadius: 'var(--r-md)',
                        border: '1px solid var(--line-2)',
                    }, children: m.text })] }));
    }
    if (m.kind === 'thought') {
        return (_jsxs("div", { className: "flex gap-3", children: [avatar, _jsxs("div", { className: "flex-1 min-w-0", children: [_jsxs("div", { onClick: () => setOpen((v) => !v), className: "flex items-center gap-2 cursor-pointer text-ink-2", style: {
                                padding: '6px 10px',
                                border: '1px dashed var(--line)',
                                borderRadius: 'var(--r-sm)',
                                background: open ? 'var(--surface)' : 'transparent',
                            }, children: [_jsx("span", { className: "font-mono text-[10.5px] text-dim", children: open ? '▾' : '▸' }), _jsx("span", { className: "cw-label", children: friendly ? 'thinking' : 'Thought' }), _jsx("span", { className: "text-[13px]", children: m.title })] }), open && (_jsx("div", { className: "flex flex-col gap-1.5 mt-1.5 ml-1.5 pl-3.5 py-1", style: { borderLeft: '2px solid var(--accent-line)' }, children: m.body.map((p, i) => (_jsxs("div", { className: "flex gap-2 items-start", children: [_jsxs("span", { className: "font-mono text-dim text-[11px] mt-0.5 min-w-[16px]", children: [i + 1, "."] }), _jsx("span", { className: "text-[13px] text-ink-2 leading-[1.5]", children: p })] }, i))) }))] })] }));
    }
    if (m.kind === 'tools') {
        const itemCount = m.items.length;
        return (_jsxs("div", { className: "flex gap-3", children: [avatar, _jsxs("div", { className: "flex-1 min-w-0", children: [_jsxs("div", { onClick: () => setOpen((v) => !v), className: "inline-flex items-center gap-2 cursor-pointer text-dim", style: friendly
                                ? {
                                    padding: '6px 10px',
                                    border: '1px solid var(--line-2)',
                                    borderRadius: 'var(--r-sm)',
                                    background: 'var(--paper)',
                                }
                                : { padding: '4px 8px' }, children: [_jsx("span", { className: "font-mono text-[10.5px]", children: open ? '▾' : '▸' }), _jsx("span", { className: "cw-label", children: friendly
                                        ? `worked through ${itemCount} step${itemCount > 1 ? 's' : ''}`
                                        : `${itemCount} tool call${itemCount > 1 ? 's' : ''}` }), friendly && !open && (_jsxs("span", { className: "font-mono text-[10.5px] text-dim", children: ["\u2014 ", m.items.slice(0, 2).map((it) => it.name).join(', '), m.items.length > 2 ? '…' : ''] }))] }), open && (_jsx("div", { className: "flex flex-col gap-1 mt-1", children: m.items.map((it, i) => {
                                const key = `${m.id}:${i}`;
                                const exp = toolExpanded[key];
                                return (_jsxs("div", { children: [_jsxs("div", { onClick: () => setToolExpanded((prev) => ({ ...prev, [key]: !exp })), className: "flex items-center gap-2 cursor-pointer font-mono text-[12px]", style: {
                                                padding: '6px 10px',
                                                border: '1px solid var(--line)',
                                                borderRadius: 'var(--r-sm)',
                                                background: exp ? 'var(--surface)' : 'var(--paper)',
                                            }, children: [_jsx("span", { className: "text-dim text-[10.5px]", children: exp ? '▾' : '▸' }), _jsx("span", { style: {
                                                        fontWeight: 600,
                                                        color: it.name === 'edit' || it.name === 'write'
                                                            ? 'var(--accent)'
                                                            : 'var(--ink)',
                                                    }, children: it.name }), _jsx("span", { className: "text-ink-2", children: it.target }), _jsx("div", { className: "flex-1" }), _jsx("span", { className: "text-dim text-[10.5px]", children: it.lines })] }), exp && (_jsxs("div", { className: "mt-1 ml-4 font-mono text-[11.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap", style: {
                                                padding: '10px 12px',
                                                background: 'var(--paper-2)',
                                                border: '1px solid var(--line-2)',
                                                borderRadius: 'var(--r-sm)',
                                            }, children: [it.name === 'read' &&
                                                    `// ${it.target}\nexport async function validateSession(token: string) {\n  const session = await db.session.findUnique({ where: { token } });\n  ⋯\n}`, it.name === 'search' &&
                                                    `9 matches in 4 files\n  src/auth/session.ts:23\n  src/auth/login.tsx:8\n  ⋯`, it.name === 'edit' &&
                                                    `// ${it.target}\n−  if (await bcrypt.compare(password, user.passwordHash)) {\n+  const credential = await db.credential.findUnique({\n+    where: { userId, id: credentialId }\n+  });`, it.name === 'write' &&
                                                    `// new file: ${it.target}\nimport { generateRegistrationOptions } from "@simplewebauthn/server";\n⋯ 48 lines`, it.name === 'run' &&
                                                    `$ ${it.target}\n  Generated Prisma Client (5.18.0)\n  ✓ done in 1.4s`] }))] }, i));
                            }) }))] })] }));
    }
    if (m.kind === 'text') {
        return (_jsxs("div", { className: "flex gap-3", children: [avatar, _jsx("div", { className: "flex-1 text-[14px] leading-[1.6] text-ink", style: { paddingTop: 3 }, children: m.text.split(/(`[^`]+`)/).map((part, i) => part.startsWith('`') ? (_jsx("span", { className: "font-mono text-[12.5px] px-1.5 py-0.5 bg-paper-2 border border-line-2 rounded", children: part.slice(1, -1) }, i)) : (_jsx("span", { children: part }, i))) })] }));
    }
    if (m.kind === 'live') {
        return (_jsxs("div", { className: "flex gap-3", children: [avatar, _jsx("div", { className: "flex-1 min-w-0", children: _jsxs("div", { className: "flex items-start gap-2.5", style: {
                            padding: '10px 12px',
                            border: '1.5px solid var(--accent-line)',
                            background: 'var(--accent-soft)',
                            borderRadius: 'var(--r-sm)',
                        }, children: [_jsx("span", { className: "w-1.5 h-1.5 rounded-full bg-accent animate-blip mt-1.5 shrink-0" }), _jsxs("div", { className: "flex-1", children: [_jsx("div", { className: friendly ? 'text-[13.5px] font-semibold tracking-[-0.005em]' : 'font-mono text-[12px] font-semibold', style: { color: 'var(--accent)' }, children: friendly ? 'Working on it…' : m.label }), !friendly && m.body && (_jsxs("div", { className: "font-mono text-[11.5px] text-ink-2 mt-1 leading-[1.55] whitespace-pre-wrap", children: [m.body, _jsx("span", { className: "inline-block bg-accent ml-0.5 align-[-2px]", style: {
                                                    width: 6,
                                                    height: 12,
                                                    animation: 'blip 1s steps(2) infinite',
                                                } })] }))] })] }) })] }));
    }
    return null;
}
// ── ChatWindow ────────────────────────────────────────────────────────────────
export function ChatWindow({ sessionId, friendly, }) {
    const { messages, isStreaming, sendMessage, cancel } = useChat();
    const bottomRef = useRef(null);
    useEffect(() => {
        bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [messages, isStreaming]);
    const handleUpload = async (file) => {
        return api.attachments.upload(sessionId, file);
    };
    const handleSend = (text, attachmentIds) => {
        sendMessage(text, attachmentIds);
    };
    // Show fixture fallback only when no real messages exist, not streaming,
    // and the design-fixture flag is on. DESIGN FIXTURE — remove the fixture
    // block when backend provides real data.
    const showFixture = SHOW_DESIGN_FIXTURES && messages.length === 0 && !isStreaming;
    return (_jsxs("div", { className: "flex-1 flex flex-col min-h-0", children: [_jsx("div", { className: "cw-scroll flex-1 px-8 py-5 flex flex-col gap-[18px]", children: _jsxs("div", { style: { maxWidth: 780, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 18 }, children: [messages.map((msg) => (_jsx(MessageBubble, { message: msg, friendly: friendly }, msg.id))), showFixture &&
                            AGENT_TRANSCRIPT_FIXTURE.map((m) => (_jsx(FixtureMessage, { m: m, friendly: friendly }, m.id))), !showFixture && messages.length === 0 && !isStreaming && (_jsx("div", { className: "flex-1 flex items-center justify-center py-16", children: _jsx("span", { className: "font-mono italic text-dim text-[12px]", children: "\u5F00\u59CB\u4E0E AI Agent \u5BF9\u8BDD" }) })), isStreaming && (_jsxs("div", { className: "flex gap-3", children: [_jsx("span", { className: "cw-avatar accent lg shrink-0 font-mono", style: { marginTop: 2 }, children: "\u25C6" }), _jsx("div", { className: "flex-1 min-w-0", children: _jsxs("div", { className: "flex items-start gap-2.5", style: {
                                            padding: '10px 12px',
                                            border: '1.5px solid var(--accent-line)',
                                            background: 'var(--accent-soft)',
                                            borderRadius: 'var(--r-sm)',
                                        }, children: [_jsx("span", { className: "w-1.5 h-1.5 rounded-full bg-accent animate-blip mt-1.5 shrink-0" }), _jsx("div", { className: friendly
                                                    ? 'text-[13.5px] font-semibold tracking-[-0.005em]'
                                                    : 'font-mono text-[12px] font-semibold', style: { color: 'var(--accent)' }, children: friendly ? 'Working on it…' : 'Running…' })] }) })] })), _jsx("div", { ref: bottomRef })] }) }), _jsx(InputBar, { onSend: handleSend, disabled: isStreaming, isStreaming: isStreaming, onStop: cancel, onUpload: handleUpload })] }));
}
