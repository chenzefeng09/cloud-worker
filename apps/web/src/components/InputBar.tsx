import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { Attachment } from '../api/types';

interface Props {
  onSend: (text: string, attachmentIds?: string[], attachments?: Attachment[]) => void;
  disabled?: boolean;
  isStreaming?: boolean;
  onStop?: () => void;
  onUpload?: (file: File) => Promise<Attachment>;
  /** Pre-populate the input (e.g. after a rewind). Cleared after consumed. */
  prefill?: string;
  onPrefillConsumed?: () => void;
}

export function InputBar({ onSend, disabled, isStreaming, onStop, onUpload, prefill, onPrefillConsumed }: Props) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Apply prefill when it arrives (e.g. after rewind).
  useEffect(() => {
    if (!prefill) return;
    setText(prefill);
    onPrefillConsumed?.();
    // Focus and resize the textarea after filling.
    requestAnimationFrame(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
      el.focus();
    });
  }, [prefill, onPrefillConsumed]);

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    if ((!text.trim() && attachments.length === 0) || disabled) return;
    const ids = attachments.map((a) => a.id);
    // Pass full attachment objects so callers can populate optimistic messages.
    onSend(text.trim(), ids, attachments.slice());
    setText('');
    setAttachments([]);
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length || !onUpload) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const file of files) {
        const att = await onUpload(file);
        setAttachments((prev) => [...prev, att]);
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上传失败，请重试';
      setUploadError(msg);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const isImage = (mime: string) => mime.startsWith('image/');

  return (
    <div className="border-t border-line bg-paper px-4 py-3 shrink-0">
      {/* Attachment chips */}
      {(attachments.length > 0 || uploading) && (
        <div className="flex flex-wrap gap-1.5 mb-2 max-w-3xl">
          {attachments.map((att) => (
            <div
              key={att.id}
              className="flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface"
            >
              <span className="text-ink-2">{isImage(att.mime) ? '🖼' : '📎'}</span>
              <span className="max-w-[120px] truncate text-ink">{att.filename}</span>
              <button
                type="button"
                onClick={() => removeAttachment(att.id)}
                className="font-mono text-dim hover:text-ink ml-0.5 leading-none"
                aria-label="移除附件"
              >
                ×
              </button>
            </div>
          ))}
          {uploading && (
            <div className="flex items-center px-2 py-1 text-[11px] text-dim font-mono">
              <span className="animate-pulse">上传中…</span>
            </div>
          )}
        </div>
      )}

      {/* Upload error */}
      {uploadError && (
        <div className="flex items-center gap-2 mb-2 max-w-3xl text-[11px] font-mono text-red-600">
          <span>⚠ {uploadError}</span>
          <button
            type="button"
            onClick={() => setUploadError(null)}
            className="text-dim hover:text-ink leading-none"
            aria-label="关闭错误提示"
          >
            ×
          </button>
        </div>
      )}

      <div className="flex gap-2 items-end bg-surface border border-line rounded-lg overflow-hidden shadow-[0_4px_12px_-4px_rgba(15,17,20,0.06)]">
        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,.pdf,.txt,.csv,.md,.json,.py,.js,.ts,.html,.xml,.yaml,.yml"
          className="hidden"
          onChange={handleFileChange}
          disabled={!onUpload || uploading}
        />

        {/* File button */}
        {onUpload && (
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            className="px-3 py-3 font-mono text-[13px] text-ink-2 hover:bg-paper rounded-l-lg disabled:opacity-40 shrink-0"
            title="上传文件或图片"
          >
            ⊕
          </button>
        )}

        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          onInput={handleInput}
          placeholder="输入消息…（Enter 发送，Shift+Enter 换行）"
          rows={1}
          disabled={disabled && !isStreaming}
          className="flex-1 resize-none border-none outline-none bg-transparent text-ink text-[14px] leading-[1.5] px-4 py-3 min-h-[44px] max-h-[200px] disabled:opacity-50 font-sans"
          style={{ scrollbarWidth: 'none' }}
        />
        <div className="flex items-center gap-1 px-3 py-2.5 shrink-0">
          {isStreaming ? (
            <button
              type="button"
              onClick={onStop}
              className="flex items-center gap-1.5 px-2.5 py-1 font-mono text-[11.5px] text-accent border border-accent-line bg-accent-soft rounded-md hover:bg-accent/10"
            >
              <span className="inline-block w-2.5 h-2.5 bg-accent rounded-[2px] shrink-0" />
              停止
            </button>
          ) : (
            <button
              type="button"
              onClick={() => handleSubmit()}
              disabled={disabled || (!text.trim() && attachments.length === 0)}
              className="px-2.5 py-1 bg-accent text-white rounded-md font-mono text-[11.5px] font-medium hover:opacity-90 disabled:opacity-40"
            >
              发送 ↵
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
