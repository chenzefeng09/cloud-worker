import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { useWorkspace } from '../lib/workspaceContext';
import { useSessions } from '../hooks/useSessions';
import { formatRelativeTimeZh } from '../lib/format';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
const PLACEHOLDERS = [
    '帮我安排明天的团队站会…',
    '把这份 PDF 概括成 5 个要点？',
    '草拟一条回复供应商邮件的中文回信…',
    '迁移到 passkey 的最佳路径是什么？',
];
const SUGGESTIONS = ['帮我规划明天', '概括这份 PDF', '回复供应商邮件', 'Q3 发布命名头脑风暴'];
const TEMPLATES = [
    { id: 'code-refactor', name: '代码重构', desc: '指定仓库，描述变更。工作节点会读取、规划并逐步修改。', glyph: '⌥', kind: 'code' },
    { id: 'research', name: '研究简报', desc: '收集来源、评估并撰写一份结构化的单页报告。', glyph: '◐', kind: 'research' },
    { id: 'deck', name: '演示构建', desc: '把笔记或文档转换成可用于幻灯片的提纲。', glyph: '▤', kind: 'deck' },
    { id: 'inbox', name: '收件箱处理', desc: '整理消息、草拟回复，其余的升级处理。', glyph: '✉', kind: 'ops' },
    { id: 'data', name: '数据整理', desc: 'CSV 输入，整洁的表格 + 摘要输出。', glyph: '⌗', kind: 'data' },
    { id: 'blank', name: '空白任务', desc: '从零开始 — 用日常语言描述目标。', glyph: '+', kind: '—' },
];
export function HomePage() {
    const { ws } = useWorkspace();
    const { sessions, createSession } = useSessions();
    const navigate = useNavigate();
    const [draft, setDraft] = useState('');
    const [phIdx, setPhIdx] = useState(0);
    const [submitError, setSubmitError] = useState(null);
    const [submitting, setSubmitting] = useState(false);
    useEffect(() => {
        if (draft)
            return;
        const t = setInterval(() => setPhIdx((p) => (p + 1) % PLACEHOLDERS.length), 3200);
        return () => clearInterval(t);
    }, [draft]);
    const startChat = async () => {
        setSubmitError(null);
        setSubmitting(true);
        try {
            const draftText = draft.trim();
            const session = await createSession(draftText || undefined);
            if (session) {
                // Pass the first user message through router state so AgentPage can
                // dispatch it once the chat store is bound to the new session.  Without
                // this the draft was being thrown away.
                navigate(`/tasks/${session.id}?mode=friendly`, { state: { initialMessage: draftText } });
            }
        }
        catch (err) {
            setSubmitError('创建任务失败，请稍后重试。');
            // eslint-disable-next-line no-console
            console.warn('create session failed', err);
        }
        finally {
            setSubmitting(false);
        }
    };
    const dateLabel = new Date().toLocaleDateString('zh-CN', {
        weekday: 'long',
        month: 'long',
        day: 'numeric',
    });
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [{ label: '首页' }], actions: _jsxs(_Fragment, { children: [_jsxs(Chip, { children: [_jsx("span", { className: "w-1.5 h-1.5 rounded-full bg-accent animate-blip" }), "1 \u4E2A\u5DE5\u4F5C\u8282\u70B9\u8FD0\u884C\u4E2D"] }), _jsx("button", { className: "cw-btn cw-btn-ghost cw-btn-sm", children: "\u2318K \u542F\u52A8" })] }) }), _jsxs("div", { className: "relative flex-1 min-h-0 flex flex-col", children: [_jsx("div", { "aria-hidden": true, className: "absolute inset-0 pointer-events-none opacity-60", style: {
                            backgroundImage: 'linear-gradient(#e6e3d6 1px, transparent 1px), linear-gradient(90deg, #e6e3d6 1px, transparent 1px)',
                            backgroundSize: '32px 32px',
                        } }), _jsxs("div", { className: "cw-scroll flex-1 px-[5vw] pt-8 pb-8 flex flex-col items-center gap-7 relative", children: [_jsxs("div", { className: "text-center shrink-0", children: [_jsx("div", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-2", children: dateLabel }), _jsxs("h1", { className: "m-0 font-display font-normal tracking-[-0.02em] leading-[1.15] text-[clamp(24px,3.4vw,40px)]", children: ["\u4F60\u597D\u3002", _jsx("br", {}), _jsx("span", { className: "text-dim", children: "\u9700\u8981\u6211\u505A\u70B9\u4EC0\u4E48\uFF1F" })] })] }), _jsxs("div", { className: "w-[min(720px,95%)] bg-surface border border-line rounded-lg shadow-[0_10px_30px_-10px_rgba(15,17,20,0.08)] overflow-hidden shrink-0", children: [_jsx("textarea", { value: draft, onChange: (e) => setDraft(e.target.value), placeholder: PLACEHOLDERS[phIdx], onKeyDown: (e) => {
                                            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey))
                                                startChat();
                                        }, className: "w-full min-h-[110px] resize-none border-none outline-none bg-transparent text-ink text-[17px] leading-[1.45] tracking-[-0.01em] px-5 pt-[18px] pb-2" }), _jsxs("div", { className: "flex items-center gap-1.5 px-3.5 py-2.5 border-t border-line-2 bg-paper", children: [_jsxs("button", { className: "cw-btn cw-btn-ghost cw-btn-sm", children: [_jsx("span", { className: "font-mono", children: "\u2295" }), " \u6587\u4EF6"] }), _jsx("div", { className: "flex-1" }), _jsxs("span", { className: "font-mono text-[10.5px] text-dim", children: ["sonnet 4.5 \u00B7 ", ws.name] }), _jsx("button", { onClick: startChat, disabled: submitting, className: "cw-btn cw-btn-accent cw-btn-sm disabled:opacity-50", children: submitting ? '创建中…' : '发送 ↵' })] })] }), submitError && (_jsx("div", { className: "w-[min(720px,95%)] px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent", children: submitError })), _jsxs("div", { className: "flex gap-2 flex-wrap justify-center max-w-[720px]", children: [_jsx("span", { className: "cw-label self-center", children: "\u8BD5\u8BD5\uFF1A" }), SUGGESTIONS.map((s) => (_jsx(Chip, { dashed: true, onClick: () => setDraft(s), children: s }, s)))] }), _jsxs("div", { className: "w-[min(960px,95%)] flex items-center gap-3.5 mt-1", children: [_jsx("div", { className: "flex-1 h-px bg-line" }), _jsx("span", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim", children: "\u6216\u4ECE\u6A21\u677F\u5F00\u59CB" }), _jsx("div", { className: "flex-1 h-px bg-line" })] }), _jsx("div", { className: "w-[min(960px,95%)] grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3", children: TEMPLATES.map((t) => (_jsxs("div", { onClick: () => navigate('/tasks'), className: "border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px", children: [_jsxs("div", { className: "flex items-center gap-2.5 mb-0.5", children: [_jsx("div", { className: "w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper", children: t.glyph }), _jsx("div", { className: "text-[14px] font-semibold tracking-[-0.01em]", children: t.name }), _jsx("div", { className: "flex-1" }), _jsx("span", { className: "font-mono text-[10.5px] text-dim", children: "\u2192" })] }), _jsx("div", { className: "text-[12px] text-dim leading-[1.45]", children: t.desc }), _jsx("div", { className: "flex-1" }), _jsx("div", { className: "flex gap-1 flex-wrap", children: _jsx(Chip, { children: t.kind }) })] }, t.id))) })] }), _jsxs("div", { className: "border-t border-line bg-paper px-6 py-3 shrink-0 relative", children: [_jsxs("div", { className: "flex items-baseline justify-between mb-2.5", children: [_jsx("span", { className: "font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim", children: "\u6700\u8FD1 \u00B7 \u8FDB\u884C\u4E2D" }), _jsx("a", { onClick: () => navigate('/tasks'), className: "cursor-pointer font-mono text-[11px] text-ink-2 hover:text-ink", children: "\u67E5\u770B\u5168\u90E8\u4EFB\u52A1 \u2192" })] }), _jsxs("div", { className: "grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-2.5", children: [sessions.slice(0, 4).map((s) => (_jsxs("div", { onClick: () => navigate(`/tasks/${s.id}`), className: "border border-line rounded-md p-3 bg-surface cursor-pointer flex flex-col gap-2 hover:border-ink-2 hover:-translate-y-px transition-all", children: [_jsxs("div", { className: "flex items-center gap-2", children: [_jsx(StatusDot, { status: sessionToDesignStatus(s.status) }), _jsx("div", { className: "flex-1" }), _jsx("span", { className: "font-mono text-[10.5px] text-dim", children: formatRelativeTimeZh(s.updated_at) })] }), _jsx("div", { className: "text-[13px] font-medium tracking-[-0.01em] leading-[1.3] truncate", children: s.title || '未命名任务' })] }, s.id))), sessions.length === 0 && (_jsx("div", { className: "font-mono italic text-dim text-[11px] px-2", children: "\u6682\u65E0\u4EFB\u52A1" }))] })] })] })] }));
}
