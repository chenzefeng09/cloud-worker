import { jsx as _jsx, Fragment as _Fragment, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useMemo, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useWorkspace } from '../lib/workspaceContext';
import { api, ApiError } from '../api/client';
import { useChatStore } from '../stores/chatStore';
import { formatRelativeTimeZh } from '../lib/format';
const KIND_GLYPH = {
    md: '☷',
    json: '{}',
    yaml: '≡',
    env: '·',
    csv: '⊞',
    pdf: '▤',
    xlsx: '⊞',
    docx: '☱',
    pptx: '▤',
    png: '◰',
    jpg: '◰',
    jpeg: '◰',
    txt: '☷',
    ics: '◷',
    folder: '▸',
};
const DOC_STATUS_ZH = {
    PENDING: '待处理',
    INGESTING: '摄入中',
    READY: '就绪',
    FAILED: '失败',
};
function mimeToKind(mime) {
    if (mime.includes('pdf'))
        return 'pdf';
    if (mime.includes('markdown') || mime.includes('text/plain'))
        return 'md';
    if (mime.includes('json'))
        return 'json';
    if (mime.includes('yaml'))
        return 'yaml';
    if (mime.includes('csv'))
        return 'csv';
    if (mime.includes('word') || mime.includes('docx'))
        return 'docx';
    if (mime.includes('spreadsheet') || mime.includes('xlsx'))
        return 'xlsx';
    if (mime.includes('presentation') || mime.includes('pptx'))
        return 'pptx';
    if (mime.includes('image/png'))
        return 'png';
    if (mime.includes('image/jpeg'))
        return 'jpg';
    if (mime.includes('text/'))
        return 'txt';
    return 'md';
}
function extFromFilename(name) {
    const parts = name.split('.');
    return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}
function formatBytes(n) {
    if (n < 1024)
        return `${n} B`;
    if (n < 1048576)
        return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
}
export function FilesPage() {
    const { ws } = useWorkspace();
    const activeSessionId = useChatStore((s) => s.activeSessionId);
    const [query, setQuery] = useState('');
    const [selected, setSelected] = useState(null);
    // Remote knowledge documents
    const [docs, setDocs] = useState([]);
    const [docsLoading, setDocsLoading] = useState(false);
    const [docsError, setDocsError] = useState(null);
    // Session attachments (inputs + outputs)
    const [sessionFiles, setSessionFiles] = useState([]);
    const [filesLoading, setFilesLoading] = useState(false);
    const [filesError, setFilesError] = useState(null);
    // File content for preview
    const [contentLoading, setContentLoading] = useState(false);
    const [contentText, setContentText] = useState(null);
    const [contentError, setContentError] = useState(null);
    useEffect(() => {
        let cancelled = false;
        setDocsLoading(true);
        setDocsError(null);
        api.library.listDocuments().then((res) => {
            if (cancelled)
                return;
            setDocs(res.items);
        }).catch((e) => {
            if (cancelled)
                return;
            setDocsError(e instanceof ApiError ? e.message : '加载知识文档失败');
        }).finally(() => {
            if (!cancelled)
                setDocsLoading(false);
        });
        return () => { cancelled = true; };
    }, []);
    useEffect(() => {
        if (!activeSessionId) {
            setSessionFiles([]);
            return;
        }
        let cancelled = false;
        setFilesLoading(true);
        setFilesError(null);
        api.attachments.list(activeSessionId).then((res) => {
            if (cancelled)
                return;
            setSessionFiles(res.items);
        }).catch((e) => {
            if (cancelled)
                return;
            setFilesError(e instanceof ApiError ? e.message : '加载会话文件失败');
        }).finally(() => {
            if (!cancelled)
                setFilesLoading(false);
        });
        return () => { cancelled = true; };
    }, [activeSessionId]);
    // Load content when an attachment is selected
    useEffect(() => {
        if (!selected || selected.type !== 'att') {
            setContentText(null);
            setContentError(null);
            return;
        }
        const att = selected.att;
        const isPreviewable = att.mime.startsWith('text/') ||
            att.mime.includes('json') ||
            att.mime.includes('yaml') ||
            att.mime.includes('xml') ||
            att.mime.includes('markdown');
        if (!isPreviewable) {
            setContentText(null);
            return;
        }
        let cancelled = false;
        setContentLoading(true);
        setContentError(null);
        api.attachments.getContent(att.session_id, att.id).then((res) => {
            if (cancelled)
                return;
            setContentText(res.text);
        }).catch((e) => {
            if (cancelled)
                return;
            setContentError(e instanceof ApiError ? e.message : '加载文件内容失败');
        }).finally(() => {
            if (!cancelled)
                setContentLoading(false);
        });
        return () => { cancelled = true; };
    }, [selected]);
    const inputFiles = useMemo(() => sessionFiles.filter((f) => f.kind === 'input'), [sessionFiles]);
    const outputFiles = useMemo(() => sessionFiles.filter((f) => f.kind === 'output'), [sessionFiles]);
    const filterDoc = (doc) => !query.trim() || doc.title.toLowerCase().includes(query.trim().toLowerCase());
    const filterAtt = (att) => !query.trim() || att.filename.toLowerCase().includes(query.trim().toLowerCase());
    const totalCount = docs.length + sessionFiles.length;
    return (_jsxs("div", { className: "flex flex-col h-full min-w-0 bg-paper", children: [_jsx(PageHead, { crumbs: [
                    { label: '首页', to: '/home' },
                    { label: '基础文件' },
                ], actions: _jsxs(_Fragment, { children: [_jsx("button", { className: "cw-btn cw-btn-ghost cw-btn-sm", children: "+ \u4E0A\u4F20" }), _jsx("button", { className: "cw-btn cw-btn-sm", children: "\u540C\u6B65\u6E90" })] }) }), _jsxs("div", { className: "grid grid-cols-[260px_1fr] min-h-0 flex-1", children: [_jsxs("aside", { className: "border-r border-line flex flex-col bg-paper", children: [_jsxs("div", { className: "px-3 py-2.5 border-b border-line-2 flex items-center gap-1.5", children: [_jsx("span", { className: "font-mono text-[11px] text-dim", children: "\u2315" }), _jsx("input", { value: query, onChange: (e) => setQuery(e.target.value), placeholder: "\u7B5B\u9009\u6587\u4EF6\u2026", className: "flex-1 border-none bg-transparent outline-none font-mono text-[11.5px] text-ink" }), _jsx("span", { className: "font-mono text-[10px] text-dim", children: totalCount })] }), _jsxs("div", { className: "cw-scroll flex-1 py-1.5", children: [(docsLoading || filesLoading) && (_jsx("div", { className: "px-3 py-2 font-mono text-[11px] text-dim", children: "\u52A0\u8F7D\u4E2D\u2026" })), docsError && (_jsx("div", { className: "px-3 py-2 font-mono text-[11px] text-accent", children: docsError })), filesError && (_jsx("div", { className: "px-3 py-2 font-mono text-[11px] text-accent", children: filesError })), outputFiles.filter(filterAtt).length > 0 && (_jsxs(_Fragment, { children: [_jsxs("div", { className: "cw-label px-3 pt-2 pb-1", children: ["Agent \u4EA7\u7269 \u00B7 ", outputFiles.filter(filterAtt).length] }), outputFiles.filter(filterAtt).map((att) => {
                                                const isSel = selected?.type === 'att' && selected.att.id === att.id;
                                                const ext = extFromFilename(att.filename) || mimeToKind(att.mime);
                                                return (_jsxs("div", { onClick: () => setSelected({ type: 'att', att }), className: `flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${isSel
                                                        ? 'bg-accent-soft border-accent'
                                                        : 'border-transparent hover:bg-paper-2'}`, children: [_jsx("span", { className: "font-mono text-[11px] text-dim w-3 text-center", children: KIND_GLYPH[ext] ?? '·' }), _jsx("span", { className: `font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`, children: att.filename }), _jsx("span", { className: "font-mono text-[9px] text-dim", children: "\u51FA" })] }, `out:${att.id}`));
                                            })] })), inputFiles.filter(filterAtt).length > 0 && (_jsxs(_Fragment, { children: [_jsxs("div", { className: "cw-label px-3 pt-2 pb-1", children: ["\u4F1A\u8BDD\u4E0A\u4F20 \u00B7 ", inputFiles.filter(filterAtt).length] }), inputFiles.filter(filterAtt).map((att) => {
                                                const isSel = selected?.type === 'att' && selected.att.id === att.id;
                                                const ext = extFromFilename(att.filename) || mimeToKind(att.mime);
                                                return (_jsxs("div", { onClick: () => setSelected({ type: 'att', att }), className: `flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${isSel
                                                        ? 'bg-accent-soft border-accent'
                                                        : 'border-transparent hover:bg-paper-2'}`, children: [_jsx("span", { className: "font-mono text-[11px] text-dim w-3 text-center", children: KIND_GLYPH[ext] ?? '·' }), _jsx("span", { className: `font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`, children: att.filename }), _jsx("span", { className: "font-mono text-[9px] text-dim", children: "\u5165" })] }, `in:${att.id}`));
                                            })] })), !docsLoading && docs.filter(filterDoc).length > 0 && (_jsxs(_Fragment, { children: [_jsxs("div", { className: "cw-label px-3 pt-2 pb-1", children: ["\u77E5\u8BC6\u6587\u6863 \u00B7 ", docs.filter(filterDoc).length] }), docs.filter(filterDoc).map((doc) => {
                                                const isSel = selected?.type === 'doc' && selected.doc.id === doc.id;
                                                const ext = mimeToKind(doc.mime);
                                                return (_jsxs("div", { onClick: () => setSelected({ type: 'doc', doc }), className: `flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${isSel
                                                        ? 'bg-accent-soft border-accent'
                                                        : 'border-transparent hover:bg-paper-2'}`, children: [_jsx("span", { className: "font-mono text-[11px] text-dim w-3 text-center", children: KIND_GLYPH[ext] ?? '·' }), _jsx("span", { className: `font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`, children: doc.title })] }, `doc:${doc.id}`));
                                            })] })), totalCount === 0 && !docsLoading && !filesLoading && (_jsx("div", { className: "px-3 py-2 font-mono italic text-dim text-[11px]", children: activeSessionId ? '无文件' : '请先选择一个会话' }))] }), _jsxs("div", { className: "px-3 py-2.5 border-t border-line-2 font-mono text-[10.5px] text-ink-2 leading-[1.7]", children: [_jsxs("div", { className: "cw-label mb-1.5", children: ["\u5DE5\u4F5C\u533A \u00B7 ", ws.name] }), activeSessionId ? (_jsxs("div", { children: [inputFiles.length, " \u4E2A\u4E0A\u4F20 \u00B7 ", outputFiles.length, " \u4E2A\u4EA7\u7269 \u00B7 ", docs.length, " \u4E2A\u77E5\u8BC6\u6587\u6863"] })) : (_jsxs("div", { children: [docs.length, " \u4E2A\u77E5\u8BC6\u6587\u6863"] }))] })] }), _jsx("section", { className: "flex flex-col min-w-0 bg-surface", children: selected ? (selected.type === 'doc' ? (_jsx(DocDetail, { doc: selected.doc })) : (_jsx(AttDetail, { att: selected.att, contentLoading: contentLoading, contentText: contentText, contentError: contentError }))) : (_jsx("div", { className: "flex-1 flex items-center justify-center text-dim text-[13px]", children: "\u4ECE\u5DE6\u4FA7\u9009\u62E9\u4E00\u4E2A\u6587\u4EF6\u67E5\u770B\u8BE6\u60C5" })) })] })] }));
}
function DocDetail({ doc }) {
    return (_jsxs(_Fragment, { children: [_jsxs("div", { className: "px-5 py-3 border-b border-line-2 flex items-center gap-2.5", children: [_jsx("span", { className: "font-mono text-[10px] text-dim", children: KIND_GLYPH[mimeToKind(doc.mime)] ?? '·' }), _jsx("span", { className: "font-mono text-[12.5px] font-semibold", children: doc.title }), _jsx(Chip, { accent: doc.status === 'FAILED', dashed: doc.status !== 'READY' && doc.status !== 'FAILED', children: DOC_STATUS_ZH[doc.status] ?? doc.status }), _jsx("div", { className: "flex-1" }), _jsx("span", { className: "font-mono text-[10.5px] text-dim", children: formatRelativeTimeZh(doc.uploaded_at) })] }), _jsx("div", { className: "cw-scroll flex-1 px-6 py-6 bg-paper", children: _jsxs("div", { className: "max-w-[720px] flex flex-col gap-4", children: [_jsx("div", { className: "grid grid-cols-2 gap-px bg-line border border-line rounded-md overflow-hidden", children: [
                                { label: 'MIME 类型', value: doc.mime },
                                { label: '分块数', value: `${doc.chunk_count} 块` },
                                { label: '摄入状态', value: DOC_STATUS_ZH[doc.status] ?? doc.status },
                                { label: '上传时间', value: formatRelativeTimeZh(doc.uploaded_at) },
                            ].map((row) => (_jsxs("div", { className: "bg-surface px-4 py-3", children: [_jsx("div", { className: "cw-label mb-1", children: row.label }), _jsx("div", { className: "font-mono text-[13px]", children: row.value })] }, row.label))) }), doc.source_uri && (_jsxs("div", { className: "bg-surface border border-line rounded-md px-4 py-3", children: [_jsx("div", { className: "cw-label mb-1", children: "\u6765\u6E90 URI" }), _jsx("div", { className: "font-mono text-[12px] text-ink-2 break-all", children: doc.source_uri })] })), _jsxs("div", { className: "font-mono text-[11px] text-dim italic", children: ["\u6587\u6863 ID \u00B7 ", doc.id] })] }) })] }));
}
function AttDetail({ att, contentLoading, contentText, contentError, }) {
    const ext = att.filename.split('.').pop()?.toLowerCase() ?? '';
    const isImage = att.mime.startsWith('image/');
    return (_jsxs(_Fragment, { children: [_jsxs("div", { className: "px-5 py-3 border-b border-line-2 flex items-center gap-2.5", children: [_jsx("span", { className: "font-mono text-[10px] text-dim", children: KIND_GLYPH[ext] ?? '·' }), _jsx("span", { className: "font-mono text-[12.5px] font-semibold", children: att.filename }), _jsx(Chip, { accent: att.kind === 'output', dashed: att.kind !== 'output', children: att.kind === 'output' ? 'Agent 产物' : '用户上传' }), _jsx("div", { className: "flex-1" }), _jsxs("span", { className: "font-mono text-[10.5px] text-dim", children: [formatBytes(att.size_bytes), " \u00B7 ", formatRelativeTimeZh(att.created_at)] })] }), _jsx("div", { className: "cw-scroll flex-1 px-6 py-6 bg-paper", children: _jsxs("div", { className: "max-w-[720px] flex flex-col gap-4", children: [_jsx("div", { className: "grid grid-cols-2 gap-px bg-line border border-line rounded-md overflow-hidden", children: [
                                { label: '文件名', value: att.filename },
                                { label: 'MIME 类型', value: att.mime },
                                { label: '大小', value: formatBytes(att.size_bytes) },
                                { label: '类型', value: att.kind === 'output' ? 'Agent 产物' : '用户上传' },
                            ].map((row) => (_jsxs("div", { className: "bg-surface px-4 py-3", children: [_jsx("div", { className: "cw-label mb-1", children: row.label }), _jsx("div", { className: "font-mono text-[13px]", children: row.value })] }, row.label))) }), contentLoading && (_jsx("div", { className: "font-mono text-[11px] text-dim", children: "\u52A0\u8F7D\u9884\u89C8\u2026" })), contentError && (_jsx("div", { className: "font-mono text-[11px] text-accent", children: contentError })), isImage && (_jsx("div", { className: "border border-line rounded-md overflow-hidden bg-surface p-2", children: _jsx("img", { src: `/api/bff/v1/sessions/${att.session_id}/files/${att.id}/content`, alt: att.filename, className: "max-w-full max-h-[400px] object-contain" }) })), contentText !== null && !contentLoading && (_jsxs("div", { className: "border border-line rounded-md overflow-hidden bg-surface", children: [_jsx("div", { className: "cw-label px-4 py-2 border-b border-line-2", children: "\u6587\u4EF6\u5185\u5BB9" }), _jsx("pre", { className: "px-4 py-3 font-mono text-[11.5px] text-ink-2 whitespace-pre-wrap break-words overflow-auto max-h-[400px]", children: contentText })] })), !contentLoading && contentText === null && !isImage && !contentError && (_jsx("div", { className: "font-mono text-[11px] text-dim italic", children: "\u2014 \u8BE5\u6587\u4EF6\u7C7B\u578B\u6682\u4E0D\u652F\u6301\u9884\u89C8\uFF0C\u8BF7\u4E0B\u8F7D\u67E5\u770B \u2014" })), _jsxs("div", { className: "font-mono text-[11px] text-dim italic", children: ["\u9644\u4EF6 ID \u00B7 ", att.id] }), att.run_id && (_jsxs("div", { className: "font-mono text-[11px] text-dim italic", children: ["Run ID \u00B7 ", att.run_id] }))] }) })] }));
}
