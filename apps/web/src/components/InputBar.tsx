import { useEffect, useMemo, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import type { Attachment, ExecutionMode, InstalledSkill } from '../api/types';
import { Chip } from './shell/Chip';
import { Dropdown } from './shell/Dropdown';
import { HoverHint } from './shell/HoverHint';
import { MODEL_OPTIONS, EFFORT_OPTIONS } from '../lib/modelOptions';
import { useCanUploadAttachment } from '../hooks/useCanUploadAttachment';
import {
  detectMentionTrigger,
  filterMentionCandidates,
  activeWorkspaceFileIds,
  type MentionCandidate,
  type MentionReference,
  type MentionTrigger,
} from '../lib/mentionTrigger';

interface Props {
  onSend: (
    text: string,
    attachmentIds?: string[],
    attachments?: Attachment[],
    workspaceFileIds?: string[],
  ) => void;
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
  /** Per-session execution mode. Omit either prop to hide the control. */
  executionMode?: ExecutionMode;
  onExecutionModeChange?: (mode: ExecutionMode) => void;
  /**
   * Candidate files for the "@" mention popover — workspace base files +
   * this session's input attachments. Omit/empty to disable the feature.
   */
  mentionCandidates?: MentionCandidate[];
}

const EXECUTION_MODE_OPTIONS = [
  { id: 'sandbox', label: '智能体' },
  { id: 'inline', label: '对话' },
];

export function InputBar({ onSend, disabled, isStreaming, onStop, onUpload, prefill, onPrefillConsumed, skills, selectedSkills, onToggleSkill, model, effort, onModelChange, onEffortChange, executionMode, onExecutionModeChange, mentionCandidates }: Props) {
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

  // ── "@ file" mention state ──────────────────────────────────────────────
  // `mentionTrigger` is the live popover anchor+query (null when closed);
  // `mentionIndex` is the keyboard highlight; `mentionRefs` records inserted
  // references so we can resolve workspace ids on send.
  const [mentionTrigger, setMentionTrigger] = useState<MentionTrigger | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [mentionRefs, setMentionRefs] = useState<MentionReference[]>([]);
  const mentionWrapRef = useRef<HTMLDivElement>(null);
  const candidates = mentionCandidates ?? [];
  const filteredMentions = useMemo(
    () => (mentionTrigger ? filterMentionCandidates(candidates, mentionTrigger.query) : []),
    [mentionTrigger, candidates],
  );
  const mentionOpen = mentionTrigger !== null && filteredMentions.length > 0;
  const activeMentionIndex = filteredMentions.length
    ? Math.min(mentionIndex, filteredMentions.length - 1)
    : 0;

  // Recompute the mention trigger from the current text + caret. Called only on
  // typing (onChange) and mouse caret moves (onClick), so resetting the
  // highlight to 0 here never fights ArrowUp/Down navigation (those keep the
  // caret still and are intercepted before any recompute).
  const recomputeMention = (value: string, caret: number) => {
    setMentionTrigger(detectMentionTrigger(value, caret));
    setMentionIndex(0);
  };

  // Replace the "@query" span with "@filename " and record the reference.
  const selectMention = (cand: MentionCandidate) => {
    const el = textareaRef.current;
    const trigger = mentionTrigger;
    if (!trigger) return;
    const caret = el?.selectionStart ?? text.length;
    const before = text.slice(0, trigger.at);
    const after = text.slice(caret);
    const insert = `@${cand.filename} `;
    const next = before + insert + after;
    setText(next);
    setMentionRefs((prev) =>
      prev.some((r) => r.id === cand.id && r.filename === cand.filename)
        ? prev
        : [...prev, { id: cand.id, filename: cand.filename, kind: cand.kind }],
    );
    setMentionTrigger(null);
    const nextCaret = before.length + insert.length;
    requestAnimationFrame(() => {
      const e2 = textareaRef.current;
      if (!e2) return;
      e2.focus();
      e2.setSelectionRange(nextCaret, nextCaret);
      e2.style.height = 'auto';
      e2.style.height = `${Math.min(e2.scrollHeight, 200)}px`;
    });
  };

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

  // Close the mention popover when clicking outside the composer.
  useEffect(() => {
    if (!mentionTrigger) return;
    const onDocClick = (e: MouseEvent) => {
      if (mentionWrapRef.current && !mentionWrapRef.current.contains(e.target as Node)) {
        setMentionTrigger(null);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [mentionTrigger]);
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
    // Only workspace-kind references whose "@filename" still survives in the
    // text count. Attachment references are text-only (the backend injects all
    // session attachments already).
    const workspaceFileIds = activeWorkspaceFileIds(mentionRefs, text);
    // Pass full attachment objects so callers can populate optimistic messages.
    onSend(text.trim(), ids, ready.slice(), workspaceFileIds.length ? workspaceFileIds : undefined);
    setText('');
    setAttachments([]);
    setMentionRefs([]);
    setMentionTrigger(null);
    uploadRef.current = null;
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // The mention popover owns these keys while it's open — critically, Enter
    // selects a file and must NOT fall through to send.
    if (mentionOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setMentionIndex((i) => (i + 1) % filteredMentions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setMentionIndex((i) => (i - 1 + filteredMentions.length) % filteredMentions.length);
        return;
      }
      if (e.key === 'Enter') {
        e.preventDefault();
        selectMention(filteredMentions[activeMentionIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setMentionTrigger(null);
        return;
      }
    }
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

      <div
        data-testid="session-composer"
        className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center bg-surface border border-line rounded-[20px] shadow-[0_14px_32px_-22px_rgba(15,17,20,0.32)]"
      >
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

        <div className="col-start-1 row-start-2 ml-3 mb-3 flex items-center gap-3 shrink-0">
          {/* "+" popover — upload + per-session skill toggles */}
          {(onUpload || showSkills) && (
          <div ref={menuRef} className="relative shrink-0">
            <HoverHint label="添加文件或启用技能" align="left">
              <button
                type="button"
                onClick={() => setMenuOpen((o) => !o)}
                disabled={uploading}
                className="w-9 h-9 inline-flex items-center justify-center rounded-full bg-paper text-ink text-[21px] leading-none hover:bg-paper-2 transition-colors disabled:opacity-40"
                aria-label="添加文件或启用技能"
                aria-haspopup="menu"
                aria-expanded={menuOpen}
              >
                <span aria-hidden className="font-sans -translate-y-px">+</span>
              </button>
            </HoverHint>
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
          {executionMode && onExecutionModeChange && (
            <HoverHint label="选择智能体或对话模式" align="left">
              <Dropdown
                value={executionMode}
                options={EXECUTION_MODE_OPTIONS}
                onChange={(mode) => onExecutionModeChange(mode as ExecutionMode)}
                disabled={disabled && !isStreaming}
                ariaLabel="选择运行模式"
                align="left"
                triggerClassName="text-[12px] text-accent"
              />
            </HoverHint>
          )}
        </div>

        <div ref={mentionWrapRef} className="relative col-span-3 row-start-1 flex min-w-0">
          {mentionOpen && (
            <div
              role="listbox"
              aria-label="文件补全"
              className="absolute bottom-full left-0 mb-2 w-72 max-h-64 overflow-y-auto z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1"
            >
              {filteredMentions.map((c, i) => {
                const on = i === activeMentionIndex;
                return (
                  <button
                    key={`${c.kind}:${c.id}`}
                    type="button"
                    role="option"
                    aria-selected={on}
                    // mousedown (not click) + preventDefault keeps the textarea
                    // focused and beats the outside-close listener.
                    onMouseDown={(e) => {
                      e.preventDefault();
                      selectMention(c);
                    }}
                    onMouseEnter={() => setMentionIndex(i)}
                    className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[12px] ${
                      on ? 'bg-accent-soft text-accent' : 'text-ink-2 hover:bg-paper'
                    }`}
                  >
                    <span aria-hidden className="shrink-0">
                      {c.kind === 'workspace' ? '🗂' : '📎'}
                    </span>
                    <span className="flex-1 truncate">{c.filename}</span>
                    <span className="shrink-0 font-mono text-[10px] text-dim">
                      {c.kind === 'workspace' ? '工作区' : '附件'}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => {
              const el = e.target;
              setText(el.value);
              recomputeMention(el.value, el.selectionStart ?? el.value.length);
            }}
            onClick={(e) => {
              const el = e.currentTarget;
              recomputeMention(el.value, el.selectionStart ?? el.value.length);
            }}
            onKeyDown={handleKeyDown}
            onInput={handleInput}
            placeholder="输入消息…（Enter 发送，Shift+Enter 换行，@ 引用文件）"
            rows={1}
            disabled={disabled && !isStreaming}
            className="flex-1 resize-none border-none outline-none bg-transparent text-ink text-[14px] leading-[1.5] px-4 pt-4 pb-2 min-h-[76px] max-h-[200px] rounded-t-[20px] disabled:opacity-50 font-sans"
            style={{ scrollbarWidth: 'none' }}
          />
        </div>
        <div className="col-start-3 row-start-2 flex items-center gap-3 mr-3 mb-3 shrink-0">
          {onModelChange && (
            <HoverHint label="选择本次会话使用的模型">
              <Dropdown
                value={model ?? ''}
                options={MODEL_OPTIONS}
                onChange={onModelChange}
                ariaLabel="选择模型"
                triggerClassName="text-[12.5px] text-ink-2"
              />
            </HoverHint>
          )}
          {onEffortChange && (
            <HoverHint label="调整模型的思考深度">
              <Dropdown
                value={effort ?? ''}
                options={EFFORT_OPTIONS}
                onChange={onEffortChange}
                ariaLabel="调整推理强度"
                triggerClassName="text-[12.5px] text-ink-2"
              />
            </HoverHint>
          )}
          {isStreaming ? (
            <HoverHint label="停止当前回复" align="right">
              <button
                type="button"
                onClick={onStop}
                className="w-10 h-10 inline-flex items-center justify-center rounded-full border border-accent-line bg-accent-soft text-accent hover:bg-accent/10 transition-colors"
                aria-label="停止当前回复"
              >
                <span aria-hidden className="inline-block w-3 h-3 bg-current rounded-[2px]" />
              </button>
            </HoverHint>
          ) : (
            <HoverHint label={pendingSend ? '正在等待附件上传' : '发送消息（Enter）'} align="right">
              <button
                type="button"
                onClick={() => handleSubmit()}
                disabled={
                  disabled ||
                  pendingSend ||
                  (!text.trim() && attachments.length === 0 && !uploading)
                }
                className="w-10 h-10 inline-flex items-center justify-center rounded-full bg-accent text-white shadow-[0_6px_16px_-8px_rgba(210,58,31,0.8)] hover:brightness-95 hover:-translate-y-px transition-all disabled:bg-line disabled:text-dim disabled:shadow-none disabled:translate-y-0 disabled:cursor-not-allowed"
                aria-label={pendingSend ? '正在等待附件上传' : '发送消息'}
              >
                {pendingSend ? (
                  <span aria-hidden className="font-mono text-[13px] animate-pulse">•••</span>
                ) : (
                  <svg aria-hidden viewBox="0 0 24 24" className="w-5 h-5" fill="none">
                    <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                )}
              </button>
            </HoverHint>
          )}
        </div>
      </div>
    </div>
  );
}
