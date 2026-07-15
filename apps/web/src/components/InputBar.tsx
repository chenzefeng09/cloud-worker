import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { Attachment, ExecutionMode, InstalledSkill } from '../api/types';
import { Chip } from './shell/Chip';
import { Dropdown } from './shell/Dropdown';
import { MODEL_OPTIONS, EFFORT_OPTIONS } from '../lib/modelOptions';
import { useCanUploadAttachment } from '../hooks/useCanUploadAttachment';

interface Props {
  onSend: (text: string, attachmentIds?: string[], attachments?: Attachment[]) => void;
  disabled?: boolean;
  isStreaming?: boolean;
  onStop?: () => void;
  onUpload?: (file: File) => Promise<Attachment>;
  /** Pre-populate the input (e.g. after a rewind). Cleared after consumed. */
  prefill?: string;
  onPrefillConsumed?: () => void;
  /** Per-session skill toggles, surfaced in the "+" popover. */
  sessionId?: string;
  skills?: InstalledSkill[];
  selectedSkills?: string[];
  onToggleSkill?: (name: string) => void;
  /** Per-session model + reasoning effort, surfaced as toolbar dropdowns. */
  model?: string;
  effort?: string;
  onModelChange?: (id: string) => void;
  onEffortChange?: (id: string) => void;
  /**
   * Per-session execution mode — 'sandbox' (智能体模式) or 'inline' (对话模式).
   * When both value + onChange are wired, a two-state segmented toggle is shown
   * in the toolbar. Omit to hide the control entirely.
   */
  executionMode?: ExecutionMode;
  onExecutionModeChange?: (mode: ExecutionMode) => void;
}

// Two-state segmented toggle for chat/agent mode. Styled to match the mono
// toolbar controls (same paper/line/accent tokens as the surrounding dropdowns).
export function ModeToggle({
  value,
  onChange,
  disabled,
}: {
  value: ExecutionMode;
  onChange: (mode: ExecutionMode) => void;
  disabled?: boolean;
}) {
  const options: { id: ExecutionMode; label: string; title: string }[] = [
    { id: 'sandbox', label: '智能体', title: '智能体模式：在沙箱中执行工具与任务' },
    { id: 'inline', label: '对话', title: '对话模式：直接对话回复，不启动沙箱' },
  ];
  return (
    <div
      role="radiogroup"
      aria-label="运行模式"
      className="inline-flex items-center rounded-sm border border-line bg-paper p-0.5"
    >
      {options.map((o) => {
        const on = o.id === value;
        return (
          <button
            key={o.id}
            type="button"
            role="radio"
            aria-checked={on}
            disabled={disabled}
            title={o.title}
            onClick={() => !on && onChange(o.id)}
            className={`px-2 py-0.5 rounded-[3px] font-mono text-[10.5px] leading-none transition-colors disabled:opacity-50 ${
              on
                ? 'bg-accent-soft text-accent font-medium'
                : 'text-dim hover:text-ink'
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

export function InputBar({ onSend, disabled, isStreaming, onStop, onUpload, prefill, onPrefillConsumed, skills, selectedSkills, onToggleSkill, model, effort, onModelChange, onEffortChange, executionMode, onExecutionModeChange }: Props) {
  const [text, setText] = useState('');
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [pendingSend, setPendingSend] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const { checkFile } = useCanUploadAttachment();

  // Close the "+" popover when clicking outside it.
  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menuOpen]);
  // Tracks in-flight uploads + the attachments they produce, so a Send that
  // races an upload can await it instead of silently sending an empty list.
  const uploadRef = useRef<Promise<Attachment[]> | null>(null);

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

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault();
    if (disabled || pendingSend) return;

    // If an upload is still in flight, wait for it so we never send with an
    // empty attachment list while a file is mid-upload. The button shows a
    // pending state meanwhile.
    let ready = attachments;
    if (uploadRef.current) {
      setPendingSend(true);
      try {
        ready = await uploadRef.current;
      } catch {
        // Upload failed — handleFileChange already surfaced the error.
        setPendingSend(false);
        return;
      }
      setPendingSend(false);
    }

    if (!text.trim() && ready.length === 0) return;
    const ids = ready.map((a) => a.id);
    // Pass full attachment objects so callers can populate optimistic messages.
    onSend(text.trim(), ids, ready.slice());
    setText('');
    setAttachments([]);
    uploadRef.current = null;
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

    // Snapshot the current attachments so the promise resolves to the full
    // list (existing + newly uploaded), letting a racing Send await it.
    const run = (async (): Promise<Attachment[]> => {
      const added: Attachment[] = [];
      try {
        for (const file of files) {
          // Pre-upload gate: large attachments need the `large-attachment`
          // entitlement. Check BEFORE the upload network call so the user gets
          // a friendly message instead of a late server-side failure.
          const gate = await checkFile(file);
          if (!gate.allowed) {
            setUploadError(
              '该文件较大，需要管理员授予大附件权限后才能上传，请联系管理员。',
            );
            return [...attachments, ...added];
          }
          const att = await onUpload(file);
          added.push(att);
          setAttachments((prev) => [...prev, att]);
        }
        return [...attachments, ...added];
      } catch (err) {
        const name = err instanceof Error ? err.message : '上传失败，请重试';
        setUploadError(name);
        throw err;
      } finally {
        setUploading(false);
        if (fileInputRef.current) fileInputRef.current.value = '';
      }
    })();

    uploadRef.current = run;
    // Swallow rejection here so it doesn't surface as an unhandled rejection;
    // handleSubmit re-awaits and handles the failure path.
    run.catch(() => {});
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const isImage = (mime: string) => mime.startsWith('image/');

  // Only show the skills section when the consumer wires both list + toggle.
  const showSkills = !!skills && !!onToggleSkill;
  const selectedCount = selectedSkills?.length ?? 0;

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

      {/* Upload error — prominent so it isn't missed. */}
      {uploadError && (
        <div
          role="alert"
          className="flex items-start gap-2 mb-2 max-w-3xl px-3 py-2 rounded-md border border-red-300 bg-red-50 text-[12.5px] text-red-700"
        >
          <span className="shrink-0 leading-[1.5]">⚠</span>
          <span className="flex-1 leading-[1.5]">附件上传失败：{uploadError}</span>
          <button
            type="button"
            onClick={() => setUploadError(null)}
            className="shrink-0 text-red-400 hover:text-red-700 leading-none"
            aria-label="关闭错误提示"
          >
            ×
          </button>
        </div>
      )}

      <div className="flex gap-2 items-end bg-surface border border-line rounded-lg shadow-[0_4px_12px_-4px_rgba(15,17,20,0.06)]">
        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept="image/*,.pdf,.txt,.csv,.md,.json,.py,.js,.ts,.html,.xml,.yaml,.yml,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.wps,.et,.dps"
          className="hidden"
          onChange={handleFileChange}
          disabled={!onUpload || uploading}
        />

        {/* "+" popover — upload + per-session skill toggles */}
        {(onUpload || showSkills) && (
          <div ref={menuRef} className="relative shrink-0">
            <button
              type="button"
              onClick={() => setMenuOpen((o) => !o)}
              disabled={uploading}
              className="px-3 py-3 font-mono text-[13px] text-ink-2 hover:bg-paper rounded-l-lg disabled:opacity-40"
              title="附件与 Skill"
              aria-haspopup="menu"
              aria-expanded={menuOpen}
            >
              ⊕
            </button>
            {menuOpen && (
              <div
                role="menu"
                className="absolute bottom-full left-0 mb-2 w-60 z-20 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-2"
              >
                {/* Upload action */}
                {onUpload && (
                  <button
                    type="button"
                    onClick={() => {
                      fileInputRef.current?.click();
                      setMenuOpen(false);
                    }}
                    disabled={uploading}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md font-mono text-[12px] text-ink-2 hover:bg-paper disabled:opacity-40 text-left"
                  >
                    <span aria-hidden>📎</span>
                    上传文件 / 图片
                  </button>
                )}

                {/* Skills — click to enable/disable for this session */}
                {showSkills && (
                  <div className={onUpload ? 'mt-2 pt-2 border-t border-line' : ''}>
                    <div className="cw-label mb-1.5 px-1">
                      Skills · {selectedCount}/{skills!.length}
                    </div>
                    <div className="flex flex-wrap gap-1 px-1">
                      {skills!.map((s) => {
                        const on = selectedSkills?.includes(s.name) ?? false;
                        return (
                          <Chip
                            key={s.id}
                            accent={on}
                            dashed={!on}
                            onClick={() => onToggleSkill?.(s.name)}
                          >
                            {s.name}
                          </Chip>
                        );
                      })}
                      {skills!.length === 0 && (
                        <span className="font-mono text-[11px] italic text-dim">暂无 skill</span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
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
        <div className="flex items-center gap-1.5 px-3 py-2.5 shrink-0">
          {executionMode && onExecutionModeChange && (
            <ModeToggle
              value={executionMode}
              onChange={onExecutionModeChange}
              disabled={disabled && !isStreaming}
            />
          )}
          {onModelChange && (
            <Dropdown
              value={model ?? ''}
              options={MODEL_OPTIONS}
              onChange={onModelChange}
              title="选择模型"
            />
          )}
          {onEffortChange && (
            <Dropdown
              value={effort ?? ''}
              options={EFFORT_OPTIONS}
              onChange={onEffortChange}
              title="推理强度"
            />
          )}
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
              disabled={
                disabled ||
                pendingSend ||
                (!text.trim() && attachments.length === 0 && !uploading)
              }
              className="px-2.5 py-1 bg-accent text-white rounded-md font-mono text-[11.5px] font-medium hover:opacity-90 disabled:opacity-40"
            >
              {pendingSend ? '等待上传…' : '发送 ↵'}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
