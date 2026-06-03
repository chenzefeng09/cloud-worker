import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWorkspace } from '../../lib/workspaceContext';
/**
 * Top-of-sidebar workspace dropdown. Closes when the user clicks outside,
 * mirrors cloud-worker's `WorkspaceSwitcher` JSX but uses CSS vars to drive
 * the per-workspace accent stripe so the markup stays Tailwind-friendly.
 */
export function WorkspaceSwitcher() {
    const { wsId, setWsId, ws, allWs } = useWorkspace();
    const [open, setOpen] = useState(false);
    const ref = useRef(null);
    const navigate = useNavigate();
    useEffect(() => {
        if (!open)
            return;
        const fn = (e) => {
            if (ref.current && !ref.current.contains(e.target))
                setOpen(false);
        };
        document.addEventListener('mousedown', fn);
        return () => document.removeEventListener('mousedown', fn);
    }, [open]);
    // Use CSS vars so the accent stripe can live in plain class-based markup.
    const accentStyle = {
        '--ws-accent': ws.accent,
        '--ws-accent-soft': ws.accent + '14',
        '--ws-accent-line': ws.accent + '55',
    };
    return (_jsxs("div", { ref: ref, className: "relative", children: [_jsxs("div", { onClick: () => setOpen(!open), style: accentStyle, className: "relative flex items-center gap-2.5 px-2.5 py-2 border border-line bg-surface rounded-md cursor-pointer hover:border-ink-2 hover:bg-paper-2 transition-colors", children: [_jsx("span", { className: "absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-sm", style: { background: 'var(--ws-accent)' } }), _jsx("span", { className: "inline-flex w-[26px] h-[26px] items-center justify-center rounded-sm font-mono text-[13px] shrink-0 border", style: {
                            background: 'var(--ws-accent-soft)',
                            borderColor: 'var(--ws-accent-line)',
                            color: 'var(--ws-accent)',
                        }, children: ws.glyph }), _jsxs("div", { className: "flex-1 min-w-0", children: [_jsx("div", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-px", children: "\u5DE5\u4F5C\u533A" }), _jsx("div", { className: "text-[13.5px] font-semibold tracking-[-0.01em] text-ink truncate", children: ws.name })] }), _jsx("span", { className: "font-mono text-[10px] text-dim", children: open ? '▴' : '▾' })] }), open && (_jsxs("div", { className: "absolute top-[calc(100%+6px)] inset-x-0 z-50 bg-surface border border-line rounded-md shadow-[0_16px_40px_-10px_rgba(15,17,20,0.18)] p-1.5 flex flex-col gap-px", children: [_jsx("div", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim px-2.5 pt-1.5 pb-1", children: "\u5207\u6362\u5DE5\u4F5C\u533A" }), allWs.map((w) => (_jsxs("div", { onClick: () => {
                            setWsId(w.id);
                            setOpen(false);
                        }, className: `flex items-center gap-2.5 px-2 py-1.5 rounded-sm cursor-pointer hover:bg-paper-2 ${w.id === wsId ? 'bg-paper-2' : ''}`, children: [_jsx("span", { className: "inline-flex w-[22px] h-[22px] items-center justify-center rounded-sm font-mono text-[11px] shrink-0 border", style: {
                                    background: w.accent + '14',
                                    borderColor: w.accent + '55',
                                    color: w.accent,
                                }, children: w.glyph }), _jsxs("div", { className: "flex-1 min-w-0", children: [_jsx("div", { className: "text-[12.5px] font-semibold tracking-[-0.01em] text-ink truncate", children: w.name }), _jsxs("div", { className: "font-mono text-[10px] text-dim", children: [w.fileCount, " \u4E2A\u6587\u4EF6 \u00B7 ", w.taskCount, " \u4E2A\u8FDB\u884C\u4E2D"] })] }), w.id === wsId && (_jsx("span", { className: "font-mono text-[9px]", style: { color: w.accent }, children: "\u25CF" }))] }, w.id))), _jsx("div", { className: "my-1.5 mx-2.5 border-t border-dashed border-line" }), _jsxs("div", { onClick: () => {
                            setOpen(false);
                            navigate('/workspace');
                        }, className: "flex items-center gap-2.5 px-2 py-1.5 rounded-sm cursor-pointer hover:bg-paper-2", children: [_jsx("span", { className: "inline-flex w-[22px] h-[22px] items-center justify-center rounded-sm font-mono text-[11px] shrink-0 border border-line bg-paper-2 text-ink-2", children: "\u2318" }), _jsx("span", { className: "text-[12.5px] text-ink-2", children: "\u7BA1\u7406\u57FA\u7840\u6587\u4EF6\u2026" })] }), _jsxs("div", { onClick: () => {
                            setOpen(false);
                            navigate('/workspace?new=1');
                        }, className: "flex items-center gap-2.5 px-2 py-1.5 rounded-sm cursor-pointer hover:bg-paper-2", children: [_jsx("span", { className: "inline-flex w-[22px] h-[22px] items-center justify-center rounded-sm font-mono text-[11px] shrink-0 border border-line bg-paper-2 text-ink-2", children: "+" }), _jsx("span", { className: "text-[12.5px] text-ink-2", children: "\u65B0\u5EFA\u5DE5\u4F5C\u533A\u2026" })] })] }))] }));
}
