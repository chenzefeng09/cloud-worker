import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
export function MessageBubble({ message, friendly }) {
    // toolExpanded tracks per-tool-call row expansion within a TOOL message.
    // key = index string of each tool_call entry.
    const [toolExpanded, setToolExpanded] = useState({});
    // groupOpen tracks whether the entire tool group is expanded.
    const [groupOpen, setGroupOpen] = useState(() => !friendly);
    const isUser = message.role === 'USER';
    // ── USER ─────────────────────────────────────────────────────────────────
    if (isUser) {
        return (_jsxs("div", { className: "flex gap-3 items-start", children: [_jsx("span", { className: "cw-avatar shrink-0", style: { marginTop: 2 }, children: "Y" }), _jsx("div", { className: "flex-1 text-[14px] leading-[1.55] tracking-[-0.005em]", style: {
                        background: 'var(--paper-2)',
                        padding: '10px 14px',
                        borderRadius: 'var(--r-md)',
                        border: '1px solid var(--line-2)',
                    }, children: message.content })] }));
    }
    const agentAvatar = (_jsx("span", { className: "cw-avatar accent lg shrink-0 font-mono", style: { marginTop: 2 }, children: "\u25C6" }));
    // ── TOOL ─────────────────────────────────────────────────────────────────
    if (message.role === 'TOOL') {
        // Derive tool items from tool_calls or fall back to content snippet
        const toolCalls = message.tool_calls;
        const itemCount = toolCalls?.length ?? 1;
        return (_jsxs("div", { className: "flex gap-3", children: [agentAvatar, _jsxs("div", { className: "flex-1 min-w-0", children: [_jsxs("div", { onClick: () => setGroupOpen((v) => !v), className: "inline-flex items-center gap-2 cursor-pointer text-dim", style: friendly
                                ? {
                                    padding: '6px 10px',
                                    border: '1px solid var(--line-2)',
                                    borderRadius: 'var(--r-sm)',
                                    background: 'var(--paper)',
                                }
                                : { padding: '4px 8px' }, children: [_jsx("span", { className: "font-mono text-[10.5px]", children: groupOpen ? '▾' : '▸' }), _jsx("span", { className: "cw-label", children: friendly
                                        ? `worked through ${itemCount} step${itemCount !== 1 ? 's' : ''}`
                                        : `${itemCount} tool call${itemCount !== 1 ? 's' : ''}` }), friendly && !groupOpen && toolCalls && (_jsxs("span", { className: "font-mono text-[10.5px] text-dim", children: ["\u2014 ", toolCalls.slice(0, 2).map((tc) => tc.name ?? 'tool').join(', '), toolCalls.length > 2 ? '…' : ''] }))] }), groupOpen && (_jsx("div", { className: "flex flex-col gap-1 mt-1", children: toolCalls && toolCalls.length > 0 ? (toolCalls.map((tc, i) => {
                                const key = String(i);
                                const exp = toolExpanded[key];
                                const toolName = tc.name ?? 'tool';
                                const toolTarget = tc.input
                                    ? String(Object.values(tc.input)[0] ?? '')
                                    : '';
                                return (_jsxs("div", { children: [_jsxs("div", { onClick: () => setToolExpanded((prev) => ({ ...prev, [key]: !exp })), className: "flex items-center gap-2 cursor-pointer font-mono text-[12px]", style: {
                                                padding: '6px 10px',
                                                border: '1px solid var(--line)',
                                                borderRadius: 'var(--r-sm)',
                                                background: exp ? 'var(--surface)' : 'var(--paper)',
                                            }, children: [_jsx("span", { className: "text-dim text-[10.5px]", children: exp ? '▾' : '▸' }), _jsx("span", { style: {
                                                        fontWeight: 600,
                                                        color: toolName === 'edit' || toolName === 'write'
                                                            ? 'var(--accent)'
                                                            : 'var(--ink)',
                                                    }, children: toolName }), toolTarget && (_jsx("span", { className: "text-ink-2 truncate", children: toolTarget }))] }), exp && (_jsx("div", { className: "mt-1 ml-4 font-mono text-[11.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap overflow-x-auto", style: {
                                                padding: '10px 12px',
                                                background: 'var(--paper-2)',
                                                border: '1px solid var(--line-2)',
                                                borderRadius: 'var(--r-sm)',
                                            }, children: JSON.stringify(tc.input ?? {}, null, 2) }))] }, i));
                            })) : (
                            /* fallback: show raw content when no structured tool_calls */
                            _jsx("div", { className: "border border-line-2 rounded-md overflow-hidden", children: _jsx("pre", { className: "px-4 py-3 font-mono text-[11.5px] text-ink-2 leading-[1.6] overflow-x-auto bg-surface whitespace-pre-wrap", children: message.content }) })) }))] })] }));
    }
    // ── SYSTEM ────────────────────────────────────────────────────────────────
    if (message.role === 'SYSTEM') {
        return (_jsxs("div", { className: "flex items-center gap-2 px-3 py-1.5 border border-dashed border-line rounded-md font-mono text-[11px] text-dim", children: [_jsx("span", { className: "text-[9px]", children: "\u25C6" }), _jsx("span", { children: message.content })] }));
    }
    // ── ASSISTANT ─────────────────────────────────────────────────────────────
    return (_jsxs("div", { className: "flex gap-3", children: [agentAvatar, _jsxs("div", { className: "flex-1 min-w-0", style: { paddingTop: 3 }, children: [_jsx("div", { className: "prose-cw text-[14px] leading-[1.6] text-ink", children: _jsx(ReactMarkdown, { components: {
                                code: ({ className, children, ...props }) => {
                                    const isBlock = className?.includes('language-');
                                    if (isBlock) {
                                        return (_jsx("pre", { className: "my-3 rounded-md border border-line overflow-x-auto bg-surface", children: _jsx("code", { className: "block px-4 py-3 font-mono text-[12.5px] text-ink-2 leading-[1.6]", children: children }) }));
                                    }
                                    return (_jsx("code", { ...props, className: "px-1.5 py-0.5 rounded font-mono text-[12.5px] bg-paper-2 border border-line-2 text-ink", children: children }));
                                },
                                p: ({ children }) => (_jsx("p", { className: "mb-3 last:mb-0", children: children })),
                                h1: ({ children }) => (_jsx("h1", { className: "mt-4 mb-2 text-[18px] font-semibold tracking-[-0.02em]", children: children })),
                                h2: ({ children }) => (_jsx("h2", { className: "mt-4 mb-2 text-[15px] font-semibold tracking-[-0.01em]", children: children })),
                                h3: ({ children }) => (_jsx("h3", { className: "mt-3 mb-1.5 text-[13.5px] font-semibold", children: children })),
                                ul: ({ children }) => (_jsx("ul", { className: "mb-3 pl-4 list-disc space-y-1", children: children })),
                                ol: ({ children }) => (_jsx("ol", { className: "mb-3 pl-4 list-decimal space-y-1", children: children })),
                                li: ({ children }) => (_jsx("li", { className: "text-[14px]", children: children })),
                                blockquote: ({ children }) => (_jsx("blockquote", { className: "pl-3 border-l-2 border-accent text-ink-2 italic my-2", children: children })),
                                a: ({ href, children }) => (_jsx("a", { href: href, className: "text-accent underline underline-offset-2 hover:opacity-80", target: "_blank", rel: "noopener noreferrer", children: children })),
                            }, children: message.content }) }), message.tool_calls && (_jsxs("div", { className: "mt-2", children: [_jsxs("button", { onClick: () => setGroupOpen((v) => !v), className: "flex items-center gap-1.5 font-mono text-[10.5px] text-dim hover:text-ink-2", children: [_jsx("span", { className: "text-[8px]", children: groupOpen ? '▾' : '▸' }), "\u5DE5\u5177\u8C03\u7528\u8BE6\u60C5"] }), groupOpen && (_jsx("pre", { className: "mt-1 px-3 py-2 font-mono text-[11px] text-dim bg-paper-2 border border-line-2 rounded-md overflow-x-auto", children: JSON.stringify(message.tool_calls, null, 2) }))] }))] })] }));
}
