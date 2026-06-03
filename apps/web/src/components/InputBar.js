import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useRef, useState } from 'react';
export function InputBar({ onSend, disabled, isStreaming, onStop, onUpload }) {
    const [text, setText] = useState('');
    const [attachments, setAttachments] = useState([]);
    const [uploading, setUploading] = useState(false);
    const textareaRef = useRef(null);
    const fileInputRef = useRef(null);
    const handleSubmit = (e) => {
        e?.preventDefault();
        if ((!text.trim() && attachments.length === 0) || disabled)
            return;
        const ids = attachments.map((a) => a.id);
        onSend(text.trim(), ids);
        setText('');
        setAttachments([]);
        if (textareaRef.current) {
            textareaRef.current.style.height = 'auto';
        }
    };
    const handleKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSubmit();
        }
    };
    const handleInput = () => {
        const el = textareaRef.current;
        if (!el)
            return;
        el.style.height = 'auto';
        el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
    };
    const handleFileChange = async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length || !onUpload)
            return;
        setUploading(true);
        try {
            for (const file of files) {
                const att = await onUpload(file);
                setAttachments((prev) => [...prev, att]);
            }
        }
        catch (err) {
            // eslint-disable-next-line no-console
            console.warn('attachment upload failed', err);
        }
        finally {
            setUploading(false);
            if (fileInputRef.current)
                fileInputRef.current.value = '';
        }
    };
    const removeAttachment = (id) => {
        setAttachments((prev) => prev.filter((a) => a.id !== id));
    };
    const isImage = (mime) => mime.startsWith('image/');
    return (_jsxs("div", { className: "border-t border-line bg-paper px-4 py-3 shrink-0", children: [(attachments.length > 0 || uploading) && (_jsxs("div", { className: "flex flex-wrap gap-1.5 mb-2 max-w-3xl", children: [attachments.map((att) => (_jsxs("div", { className: "flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface", children: [_jsx("span", { className: "text-ink-2", children: isImage(att.mime) ? '🖼' : '📎' }), _jsx("span", { className: "max-w-[120px] truncate text-ink", children: att.filename }), _jsx("button", { type: "button", onClick: () => removeAttachment(att.id), className: "font-mono text-dim hover:text-ink ml-0.5 leading-none", "aria-label": "\u79FB\u9664\u9644\u4EF6", children: "\u00D7" })] }, att.id))), uploading && (_jsx("div", { className: "flex items-center px-2 py-1 text-[11px] text-dim font-mono", children: _jsx("span", { className: "animate-pulse", children: "\u4E0A\u4F20\u4E2D\u2026" }) }))] })), _jsxs("div", { className: "flex gap-2 items-end bg-surface border border-line rounded-lg overflow-hidden shadow-[0_4px_12px_-4px_rgba(15,17,20,0.06)]", children: [_jsx("input", { ref: fileInputRef, type: "file", multiple: true, accept: "image/*,.pdf,.txt,.csv,.md,.json,.py,.js,.ts,.html,.xml,.yaml,.yml", className: "hidden", onChange: handleFileChange, disabled: disabled || !onUpload || uploading }), onUpload && (_jsx("button", { type: "button", onClick: () => fileInputRef.current?.click(), disabled: disabled || uploading, className: "px-3 py-3 font-mono text-[13px] text-ink-2 hover:bg-paper rounded-l-lg disabled:opacity-40 shrink-0", title: "\u4E0A\u4F20\u6587\u4EF6\u6216\u56FE\u7247", children: "\u2295" })), _jsx("textarea", { ref: textareaRef, value: text, onChange: (e) => setText(e.target.value), onKeyDown: handleKeyDown, onInput: handleInput, placeholder: "\u8F93\u5165\u6D88\u606F\u2026\uFF08Enter \u53D1\u9001\uFF0CShift+Enter \u6362\u884C\uFF09", rows: 1, disabled: disabled && !isStreaming, className: "flex-1 resize-none border-none outline-none bg-transparent text-ink text-[14px] leading-[1.5] px-4 py-3 min-h-[44px] max-h-[200px] disabled:opacity-50 font-sans", style: { scrollbarWidth: 'none' } }), _jsx("div", { className: "flex items-center gap-1 px-3 py-2.5 shrink-0", children: isStreaming ? (_jsxs("button", { type: "button", onClick: onStop, className: "flex items-center gap-1.5 px-2.5 py-1 font-mono text-[11.5px] text-accent border border-accent-line bg-accent-soft rounded-md hover:bg-accent/10", children: [_jsx("span", { className: "inline-block w-2.5 h-2.5 bg-accent rounded-[2px] shrink-0" }), "\u505C\u6B62"] })) : (_jsx("button", { type: "button", onClick: () => handleSubmit(), disabled: disabled || (!text.trim() && attachments.length === 0), className: "px-2.5 py-1 bg-accent text-white rounded-md font-mono text-[11.5px] font-medium hover:opacity-90 disabled:opacity-40", children: "\u53D1\u9001 \u21B5" })) })] })] }));
}
