import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useEffect, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { api } from '../api/client';
import { ApiError } from '../api/client';
import { formatRelativeTimeZh } from '../lib/format';
const TAB_LABELS = {
    mcp: 'MCP 工具',
    skills: '技能',
    docs: '知识源',
};
const DOC_STATUS_ZH = {
    PENDING: '待处理',
    INGESTING: '摄入中',
    READY: '就绪',
    FAILED: '失败',
};
export function LibraryPage() {
    const [tab, setTab] = useState('mcp');
    const [mcpServers, setMcpServers] = useState([]);
    const [skills, setSkills] = useState([]);
    const [docs, setDocs] = useState([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    useEffect(() => {
        let cancelled = false;
        setLoading(true);
        setError(null);
        const fetchAll = async () => {
            try {
                const [mcpRes, skillRes, docRes] = await Promise.all([
                    api.library.listMcpServers(),
                    api.library.listSkills(),
                    api.library.listDocuments(),
                ]);
                if (cancelled)
                    return;
                setMcpServers(mcpRes.items);
                setSkills(skillRes.items);
                setDocs(docRes.items);
            }
            catch (e) {
                if (cancelled)
                    return;
                setError(e instanceof ApiError ? e.message : '加载资源库失败，请稍后重试。');
            }
            finally {
                if (!cancelled)
                    setLoading(false);
            }
        };
        fetchAll();
        return () => { cancelled = true; };
    }, []);
    const counts = {
        mcp: mcpServers.length,
        skills: skills.length,
        docs: docs.length,
    };
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [{ label: '首页', to: '/home' }, { label: '资源库' }], actions: _jsx("button", { className: "cw-btn cw-btn-accent cw-btn-sm", children: "+ \u5B89\u88C5" }) }), _jsx("div", { className: "cw-scroll flex-1 px-8 py-5", children: _jsxs("div", { className: "max-w-[960px] flex flex-col gap-4", children: [_jsx("div", { className: "grid grid-cols-3 gap-px bg-line border border-line rounded-md overflow-hidden", children: ['mcp', 'skills', 'docs'].map((t) => (_jsxs("div", { onClick: () => setTab(t), className: `bg-surface px-4 py-3 cursor-pointer hover:bg-paper-2 transition-colors ${tab === t ? 'border-b-2 border-accent' : ''}`, children: [_jsx("div", { className: "cw-label mb-1", children: TAB_LABELS[t] }), _jsx("div", { className: "font-mono text-[24px] font-semibold tracking-[-0.01em]", children: loading ? '…' : counts[t] })] }, t))) }), error && (_jsx("div", { className: "px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent", children: error })), _jsx("div", { className: "flex gap-0.5 border-b border-line", children: ['mcp', 'skills', 'docs'].map((t) => (_jsxs("button", { onClick: () => setTab(t), className: `px-3 py-1.5 font-mono text-[11px] rounded-t-md border-b-2 -mb-px transition-colors ${tab === t
                                    ? 'border-accent text-accent bg-paper'
                                    : 'border-transparent text-ink-2 hover:text-ink hover:bg-paper-2'}`, children: [TAB_LABELS[t], _jsx("span", { className: "ml-1.5 text-[10px] text-dim", children: loading ? '…' : counts[t] })] }, t))) }), loading && (_jsx("div", { className: "font-mono text-[11px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" })), !loading && tab === 'mcp' && (_jsx(_Fragment, { children: mcpServers.length === 0 ? (_jsx("div", { className: "font-mono italic text-dim text-[12px]", children: "\u6682\u65E0 MCP \u5DE5\u5177" })) : (_jsx("div", { className: "flex flex-col gap-1.5", children: mcpServers.map((s) => (_jsxs("div", { className: "grid grid-cols-[1fr_80px_80px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all", children: [_jsxs("div", { children: [_jsx("div", { className: "text-[13.5px] font-medium", children: s.name }), _jsx("div", { className: "font-mono text-[11px] text-dim truncate", children: s.transport === 'http' ? s.endpoint : s.command })] }), _jsx("span", { className: "font-mono text-[11px] text-ink-2 uppercase", children: s.transport }), _jsx(Chip, { accent: s.enabled, dashed: !s.enabled, children: s.enabled ? '已启用' : '已禁用' }), _jsx("span", { className: "font-mono text-[11px] text-dim text-right", children: formatRelativeTimeZh(s.updated_at) })] }, s.id))) })) })), !loading && tab === 'skills' && (_jsx(_Fragment, { children: skills.length === 0 ? (_jsx("div", { className: "font-mono italic text-dim text-[12px]", children: "\u6682\u65E0\u6280\u80FD" })) : (_jsx("div", { className: "flex flex-col gap-1.5", children: skills.map((sk) => (_jsxs("div", { className: "grid grid-cols-[1fr_80px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all", children: [_jsxs("div", { children: [_jsx("div", { className: "text-[13.5px] font-medium", children: sk.name }), sk.description && (_jsx("div", { className: "font-mono text-[11px] text-dim truncate", children: sk.description }))] }), _jsx("span", { className: "font-mono text-[11px] text-ink-2", children: sk.owner ?? '—' }), _jsx("span", { className: "font-mono text-[11px] text-dim text-right", children: formatRelativeTimeZh(sk.created_at) })] }, sk.id))) })) })), !loading && tab === 'docs' && (_jsx(_Fragment, { children: docs.length === 0 ? (_jsx("div", { className: "font-mono italic text-dim text-[12px]", children: "\u6682\u65E0\u77E5\u8BC6\u6E90" })) : (_jsx("div", { className: "flex flex-col gap-1.5", children: docs.map((d) => (_jsxs("div", { className: "grid grid-cols-[1fr_80px_60px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all", children: [_jsxs("div", { children: [_jsx("div", { className: "text-[13.5px] font-medium", children: d.title }), _jsx("div", { className: "font-mono text-[11px] text-dim truncate", children: d.mime })] }), _jsx(Chip, { accent: d.status === 'FAILED', dashed: d.status !== 'READY' && d.status !== 'FAILED', children: DOC_STATUS_ZH[d.status] ?? d.status }), _jsxs("span", { className: "font-mono text-[11px] text-dim text-center", children: [d.chunk_count, " \u5757"] }), _jsx("span", { className: "font-mono text-[11px] text-dim text-right", children: formatRelativeTimeZh(d.uploaded_at) })] }, d.id))) })) }))] }) })] }));
}
