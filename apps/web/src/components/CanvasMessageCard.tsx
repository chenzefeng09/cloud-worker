import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent } from 'react';
import {
  CANVAS_OBJECTS,
  getCanvasObject,
  type CanvasDocumentDraft,
  type CanvasEditIntent,
  type CanvasEditRequest,
  type CanvasEditSuggestion,
  type CanvasObjectKind,
  type CanvasTextSelection,
} from '../lib/canvasObjects';
import {
  canvasHtmlToMarkdown,
  getMarkdownSelectionInside,
  markdownToCanvasHtml,
} from '../lib/canvasRichText';
import { CopyIcon, useCopied } from './CopyButton';

interface CanvasMessageCardProps {
  sessionId: string;
  draft: CanvasDocumentDraft;
  /** True while the underlying creation run streams — freezes the editor. */
  aiDisabled?: boolean;
  pending?: boolean;
  /** True while a silent canvas revision request is in flight. */
  revising?: boolean;
  /** Localised error from the last failed revision (chat-invisible). */
  editError?: string | null;
  onDraftChange: (draft: CanvasDocumentDraft) => void;
  onRequestEdit: (request: CanvasEditRequest) => void | Promise<void>;
  onDismissEditError?: () => void;
  onOpenPanel?: () => void;
  suggestion?: CanvasEditSuggestion | null;
  onAcceptSuggestion?: () => void;
  onRejectSuggestion?: () => void;
}

type ToolbarMode = 'menu' | 'input';

interface ToolbarState {
  x: number;
  y: number;
  mode: ToolbarMode;
}

interface InlineSuggestionPreview {
  beforeHtml: string;
  oldTextHtml: string;
  replacementHtml: string;
  afterHtml: string;
}

const HISTORY_LIMIT = 80;
const STORAGE_PREFIX = 'article-canvas-draft-v1';

function storageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}:${sessionId}`;
}

function trimHistory(items: string[]): string[] {
  return items.length > HISTORY_LIMIT ? items.slice(items.length - HISTORY_LIMIT) : items;
}

function escapeInlineHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/\n/g, '<br>');
}

function buildInlineSuggestionPreview(
  content: string,
  suggestion?: CanvasEditSuggestion | null,
): InlineSuggestionPreview | null {
  if (!suggestion || suggestion.request.scope !== 'selection' || !suggestion.request.selection) return null;

  const { start, end, text } = suggestion.request.selection;
  let resolvedStart = start;
  let resolvedEnd = end;
  if (content.slice(start, end) !== text) {
    resolvedStart = content.indexOf(text);
    if (resolvedStart === -1) return null;
    resolvedEnd = resolvedStart + text.length;
  }

  return {
    beforeHtml: markdownToCanvasHtml(content.slice(0, resolvedStart)),
    oldTextHtml: escapeInlineHtml(content.slice(resolvedStart, resolvedEnd)),
    replacementHtml: markdownToCanvasHtml(suggestion.replacement),
    afterHtml: markdownToCanvasHtml(content.slice(resolvedEnd)),
  };
}

function shouldPasteAsRichBlocks(text: string): boolean {
  return (
    /\n{2,}/.test(text) ||
    /^(#{1,3}\s+|>\s?|[-*]\s+|\d+\.\s+)/m.test(text) ||
    /^\s*\|.+\|\s*$/m.test(text)
  );
}

export function CanvasMessageCard({
  sessionId,
  draft,
  aiDisabled,
  pending,
  revising,
  editError,
  onDraftChange,
  onRequestEdit,
  onDismissEditError,
  onOpenPanel,
  suggestion,
  onAcceptSuggestion,
  onRejectSuggestion,
}: CanvasMessageCardProps) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const hydratedDraftIdRef = useRef<string | null>(null);
  const localEditContentRef = useRef<string | null>(null);
  const selectedRangeRef = useRef<Range | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const object = getCanvasObject(draft.kind);
  const { copied, copy } = useCopied();
  const [selection, setSelection] = useState<CanvasTextSelection | null>(null);
  const [toolbar, setToolbar] = useState<ToolbarState | null>(null);
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [future, setFuture] = useState<string[]>([]);
  const inlineSuggestionPreview = useMemo(
    () => buildInlineSuggestionPreview(draft.content, suggestion),
    [draft.content, suggestion],
  );
  const hasInlineSuggestionPreview = Boolean(inlineSuggestionPreview);

  useEffect(() => {
    const el = editorRef.current;
    if (!el) {
      if (hydratedDraftIdRef.current !== draft.id) {
        hydratedDraftIdRef.current = draft.id;
        localEditContentRef.current = null;
        setHistory([]);
        setFuture([]);
        setSelection(null);
        setToolbar(null);
      }
      return;
    }

    if (hydratedDraftIdRef.current !== draft.id) {
      hydratedDraftIdRef.current = draft.id;
      localEditContentRef.current = null;
      el.innerHTML = markdownToCanvasHtml(draft.content);
      setHistory([]);
      setFuture([]);
      setSelection(null);
      setToolbar(null);
      return;
    }

    if (localEditContentRef.current === draft.content) {
      localEditContentRef.current = null;
      return;
    }

    if (!el.innerHTML.trim() && draft.content.trim()) {
      el.innerHTML = markdownToCanvasHtml(draft.content);
      setSelection(null);
      setToolbar(null);
      return;
    }

    const current = canvasHtmlToMarkdown(el);
    if (current !== draft.content) {
      setHistory((prev) => trimHistory([...prev, current]));
      setFuture([]);
      el.innerHTML = markdownToCanvasHtml(draft.content);
      setSelection(null);
      setToolbar(null);
    }
  }, [draft.content, draft.id, hasInlineSuggestionPreview]);

  useEffect(() => {
    try {
      localStorage.setItem(storageKey(sessionId), JSON.stringify(draft));
    } catch {
      // Canvas persistence remains best-effort until backend document APIs land.
    }
  }, [draft, sessionId]);

  useEffect(() => {
    if (toolbar?.mode !== 'input') return;
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [toolbar?.mode]);

  const updateDraft = useCallback(
    (patch: Partial<CanvasDocumentDraft>) => {
      onDraftChange({
        ...draft,
        ...patch,
        updatedAt: new Date().toISOString(),
      });
    },
    [draft, onDraftChange],
  );

  const commitContent = useCallback(
    (next: string, recordHistory = true) => {
      if (next === draft.content) return;
      localEditContentRef.current = next;
      if (recordHistory) {
        setHistory((prev) => trimHistory([...prev, draft.content]));
        setFuture([]);
      }
      updateDraft({ content: next });
    },
    [draft.content, updateDraft],
  );

  const syncEditorContent = useCallback((text: string) => {
    if (editorRef.current && canvasHtmlToMarkdown(editorRef.current) !== text) {
      editorRef.current.innerHTML = markdownToCanvasHtml(text);
    }
  }, []);

  const wordCount = useMemo(
    () => draft.content.trim().split(/\s+/).filter(Boolean).length,
    [draft.content],
  );
  const suggestionPreviewHtml = useMemo(
    () => (suggestion ? markdownToCanvasHtml(suggestion.replacement) : ''),
    [suggestion],
  );

  const showToolbarForRange = useCallback((range: Range, mode: ToolbarMode, fallback?: { x: number; y: number }) => {
    const rect = range.getBoundingClientRect();
    const x = fallback?.x ?? rect.left + rect.width / 2;
    const y = fallback?.y ?? Math.max(76, rect.top - 10);
    selectedRangeRef.current = range.cloneRange();
    setToolbar({
      x: Math.min(window.innerWidth - 24, Math.max(24, x)),
      y: Math.min(window.innerHeight - 24, Math.max(76, y)),
      mode,
    });
  }, []);

  const refreshSelection = useCallback(
    (mode: ToolbarMode = 'menu', fallback?: { x: number; y: number }) => {
      const el = editorRef.current;
      const sel = window.getSelection();
      if (!el || !sel || sel.rangeCount === 0) return;
      const range = sel.getRangeAt(0);
      const next = getMarkdownSelectionInside(el, draft.content);
      if (!next) {
        setSelection(null);
        if (toolbar?.mode !== 'input') setToolbar(null);
        return;
      }
      setSelection(next);
      showToolbarForRange(range, mode, fallback);
    },
    [draft.content, showToolbarForRange, toolbar?.mode],
  );

  const handleInput = useCallback(() => {
    const next = editorRef.current ? canvasHtmlToMarkdown(editorRef.current) : '';
    commitContent(next);
    refreshSelection();
  }, [commitContent, refreshSelection]);

  const restoreEditorSelection = () => {
    const range = selectedRangeRef.current;
    if (!range) return false;
    const sel = window.getSelection();
    if (!sel) return false;
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
  };

  const commitEditorDom = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    const next = canvasHtmlToMarkdown(el);
    commitContent(next);
    refreshSelection();
  }, [commitContent, refreshSelection]);

  const insertHtmlAtSelection = useCallback((html: string) => {
    const el = editorRef.current;
    if (!el) return;
    const template = document.createElement('template');
    template.innerHTML = html;
    const fragment = template.content;
    const lastNode = fragment.lastChild;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) {
      el.appendChild(fragment);
      return;
    }

    const range = sel.getRangeAt(0);
    if (!el.contains(range.commonAncestorContainer)) {
      el.appendChild(fragment);
      return;
    }

    range.deleteContents();
    range.insertNode(fragment);
    if (lastNode) {
      range.setStartAfter(lastNode);
      range.collapse(true);
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }, []);

  const handlePaste = useCallback(
    (event: ClipboardEvent<HTMLDivElement>) => {
      if (aiDisabled) return;
      const text = event.clipboardData.getData('text/plain');
      if (!text) return;
      event.preventDefault();
      const html = shouldPasteAsRichBlocks(text)
        ? markdownToCanvasHtml(text)
        : escapeInlineHtml(text);
      insertHtmlAtSelection(html);
      commitEditorDom();
    },
    [aiDisabled, commitEditorDom, insertHtmlAtSelection],
  );

  const applyRichCommand = (command: 'bold' | 'italic') => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    restoreEditorSelection();
    document.execCommand(command);
    commitEditorDom();
  };

  const applyBlockCommand = (tag: 'h1' | 'h2' | 'blockquote') => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    restoreEditorSelection();
    const applied = document.execCommand('formatBlock', false, tag);
    if (!applied) {
      document.execCommand('formatBlock', false, `<${tag}>`);
    }
    commitEditorDom();
  };

  const handleContextMenu = (event: React.MouseEvent<HTMLDivElement>) => {
    const el = editorRef.current;
    if (!el) return;
    const next = getMarkdownSelectionInside(el, draft.content);
    if (!next) return;
    event.preventDefault();
    setSelection(next);
    const sel = window.getSelection();
    if (sel && sel.rangeCount > 0) {
      showToolbarForRange(sel.getRangeAt(0), 'input', { x: event.clientX, y: event.clientY });
    }
  };

  const handleKindChange = (kind: CanvasObjectKind) => {
    const nextObject = getCanvasObject(kind);
    const currentObject = getCanvasObject(draft.kind);
    updateDraft({
      kind,
      title: draft.title === currentObject.defaultTitle ? nextObject.defaultTitle : draft.title,
    });
  };

  const buildRequest = (scope: 'selection' | 'document', intent?: CanvasEditIntent): CanvasEditRequest | null => {
    const finalInstruction = (intent?.instruction || instruction).trim();
    if (!finalInstruction) return null;
    if (scope === 'selection' && !selection) return null;
    return {
      documentId: draft.id,
      kind: draft.kind,
      title: draft.title,
      scope,
      instruction: finalInstruction,
      content: draft.content,
      selection: scope === 'selection' ? selection ?? undefined : undefined,
    };
  };

  const requestEdit = async (scope: 'selection' | 'document', intent?: CanvasEditIntent) => {
    if (aiDisabled || busy || revising) return;
    const request = buildRequest(scope, intent);
    if (!request) return;
    setBusy(true);
    setToolbar(null);
    try {
      await onRequestEdit(request);
      setInstruction('');
      if (intent) setInstruction(intent.instruction);
    } finally {
      setBusy(false);
    }
  };

  const undo = () => {
    const previous = history[history.length - 1];
    if (previous === undefined) return;
    setHistory((prev) => prev.slice(0, -1));
    setFuture((prev) => [draft.content, ...prev]);
    updateDraft({ content: previous });
    syncEditorContent(previous);
  };

  const redo = () => {
    const next = future[0];
    if (next === undefined) return;
    setFuture((prev) => prev.slice(1));
    setHistory((prev) => trimHistory([...prev, draft.content]));
    updateDraft({ content: next });
    syncEditorContent(next);
  };

  const acceptSuggestion = () => {
    if (!suggestion || !onAcceptSuggestion) return;
    setHistory((prev) => trimHistory([...prev, draft.content]));
    setFuture([]);
    onAcceptSuggestion();
  };

  const selectedChars = selection ? selection.end - selection.start : 0;
  const isRevising = Boolean(busy || revising);
  const canRequestSelection = Boolean(selection && instruction.trim() && !aiDisabled && !isRevising);
  const canRequestDocument = Boolean(instruction.trim() && !aiDisabled && !isRevising);
  const suggestionSource =
    suggestion?.request.scope === 'selection' ? suggestion.request.selection?.text : suggestion?.request.content;
  const showDocumentSuggestion = Boolean(suggestion && !inlineSuggestionPreview);

  return (
    <div className="canvas-message-shell">
      <article className="canvas-message-card">
        <header className="canvas-message-card-head">
          <div className="flex items-center gap-2 min-w-0">
            <button
              type="button"
              className="canvas-card-edit-chip"
              onClick={() => editorRef.current?.focus()}
            >
              <span className="font-mono">✎</span>
              <span>编辑</span>
            </button>
            <select
              value={draft.kind}
              onChange={(event) => handleKindChange(event.target.value as CanvasObjectKind)}
              className="canvas-card-kind-select"
              title="对象类型"
            >
              {CANVAS_OBJECTS.map((item) => (
                <option key={item.kind} value={item.kind}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              className="cw-btn cw-btn-ghost cw-btn-sm"
              onClick={() => copy(draft.content)}
              title="复制"
              aria-label="复制"
            >
              {copied ? <span className="font-mono text-[10px]">已复制</span> : <CopyIcon />}
            </button>
            {onOpenPanel && (
              <button
                type="button"
                className="cw-btn cw-btn-ghost cw-btn-sm"
                onClick={onOpenPanel}
                title="在侧栏打开"
                aria-label="在侧栏打开"
              >
                ⛶
              </button>
            )}
          </div>
        </header>

        <div className="canvas-message-card-toolbar">
          <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={undo} disabled={history.length === 0} title="撤销">
            ↶
          </button>
          <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={redo} disabled={future.length === 0} title="恢复">
            ↷
          </button>
          <span className="h-5 w-px bg-line mx-1 shrink-0" />
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm font-semibold"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyRichCommand('bold')}
            title="加粗"
          >
            B
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm italic"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyRichCommand('italic')}
            title="斜体"
          >
            I
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('h1')}
            title="标题 1"
          >
            H1
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('h2')}
            title="标题 2"
          >
            H2
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('blockquote')}
            title="引用"
          >
            “
          </button>
          <div className="flex-1" />
          <span className="font-mono text-[10.5px] text-dim whitespace-nowrap">
            {draft.content.length} 字符 · {wordCount} 词
          </span>
        </div>

        <div className="canvas-message-card-body">
          <input
            value={draft.title}
            onChange={(event) => updateDraft({ title: event.target.value })}
            className="canvas-card-title-input"
            aria-label={`${object.label}标题`}
          />
          {showDocumentSuggestion && suggestion && (
            <section
              className="canvas-suggestion-review"
              aria-label={suggestion.request.scope === 'selection' ? '选区修改建议' : '整篇修改建议'}
            >
              <div className="canvas-suggestion-review-head">
                <div className="min-w-0">
                  <div className="canvas-suggestion-kicker">
                    {suggestion.request.scope === 'selection' ? '选区修改建议' : '整篇修改建议'}
                  </div>
                  <div className="canvas-suggestion-instruction">{suggestion.request.instruction}</div>
                </div>
                <div className="canvas-suggestion-actions">
                  <button
                    type="button"
                    className="canvas-suggestion-reject"
                    onClick={onRejectSuggestion}
                    disabled={!onRejectSuggestion}
                  >
                    撤销
                  </button>
                  <button
                    type="button"
                    className="canvas-suggestion-accept"
                    onClick={acceptSuggestion}
                    disabled={!onAcceptSuggestion}
                  >
                    接受
                  </button>
                </div>
              </div>
              {suggestionSource && suggestion.request.scope === 'selection' && (
                <div className="canvas-suggestion-before">{suggestionSource}</div>
              )}
              <div
                className="canvas-rich-editor canvas-suggestion-after"
                dangerouslySetInnerHTML={{ __html: suggestionPreviewHtml }}
              />
            </section>
          )}
          {inlineSuggestionPreview && suggestion ? (
            <div className="canvas-rich-editor canvas-inline-editor canvas-suggestion-preview-document text-ink break-words">
              <div
                className="canvas-suggestion-preview-segment"
                dangerouslySetInnerHTML={{ __html: inlineSuggestionPreview.beforeHtml }}
              />
              <section className="canvas-inline-suggestion-card" aria-label="选区修改建议">
                <div className="canvas-inline-suggestion-head">
                  <div className="min-w-0">
                    <div className="canvas-suggestion-kicker">选区修改建议</div>
                    <div className="canvas-suggestion-instruction">{suggestion.request.instruction}</div>
                  </div>
                  <div className="canvas-suggestion-actions">
                    <button
                      type="button"
                      className="canvas-suggestion-reject"
                      onClick={onRejectSuggestion}
                      disabled={!onRejectSuggestion}
                    >
                      撤销
                    </button>
                    <button
                      type="button"
                      className="canvas-suggestion-accept"
                      onClick={acceptSuggestion}
                      disabled={!onAcceptSuggestion}
                    >
                      接受
                    </button>
                  </div>
                </div>
                <div
                  className="canvas-inline-suggestion-old"
                  dangerouslySetInnerHTML={{ __html: inlineSuggestionPreview.oldTextHtml }}
                />
                <div
                  className="canvas-inline-suggestion-new"
                  dangerouslySetInnerHTML={{ __html: inlineSuggestionPreview.replacementHtml }}
                />
              </section>
              <div
                className="canvas-suggestion-preview-segment"
                dangerouslySetInnerHTML={{ __html: inlineSuggestionPreview.afterHtml }}
              />
            </div>
          ) : (
            <div
              ref={editorRef}
              contentEditable={!aiDisabled}
              suppressContentEditableWarning
              className="canvas-rich-editor canvas-inline-editor outline-none text-ink break-words"
              role="textbox"
              aria-label="页内画布正文"
              onInput={handleInput}
              onMouseUp={() => refreshSelection('menu')}
              onKeyUp={() => refreshSelection('menu')}
              onContextMenu={handleContextMenu}
              onPaste={handlePaste}
            />
          )}
          {pending && (
            <div className="canvas-card-pending">
              <span className="cw-blip" />
              <span>正在写入…</span>
            </div>
          )}
          {isRevising && !pending && (
            <div className="canvas-card-pending" role="status" aria-live="polite">
              <span className="cw-blip" />
              <span>修订中…</span>
            </div>
          )}
          {editError && (
            <div className="canvas-card-edit-error" role="alert">
              <span className="canvas-card-edit-error-text">{editError}</span>
              {onDismissEditError && (
                <button
                  type="button"
                  className="canvas-card-edit-error-dismiss"
                  onClick={onDismissEditError}
                  aria-label="关闭错误提示"
                >
                  ✕
                </button>
              )}
            </div>
          )}
        </div>

        <footer className="canvas-message-card-foot">
          <div className="flex flex-wrap gap-1.5 min-w-0">
            {object.editIntents.map((intent) => (
              <button
                key={intent.id}
                type="button"
                className="cw-btn cw-btn-ghost cw-btn-sm"
                disabled={aiDisabled || isRevising}
                onClick={() => requestEdit(selection ? 'selection' : 'document', intent)}
              >
                {intent.label}
              </button>
            ))}
          </div>
          <div className="canvas-card-request-row">
            <input
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              className="canvas-card-request-input"
              placeholder={
                isRevising
                  ? '修订中…'
                  : selection
                    ? `已选中 ${selectedChars} 字符，输入修改要求…`
                    : '输入整篇修改要求…'
              }
              disabled={aiDisabled || isRevising}
            />
            <button
              type="button"
              className="cw-btn cw-btn-accent cw-btn-sm"
              disabled={!canRequestDocument}
              onClick={() => requestEdit('document')}
            >
              整篇
            </button>
            <button
              type="button"
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={!canRequestSelection}
              onClick={() => requestEdit('selection')}
            >
              选区
            </button>
          </div>
        </footer>
      </article>

      {toolbar && (
        <div
          className="canvas-selection-popover"
          style={{
            left: toolbar.x,
            top: toolbar.y,
          }}
          onMouseDown={(event) => event.preventDefault()}
        >
          {toolbar.mode === 'menu' ? (
            <>
              <button
                type="button"
                className="canvas-selection-command"
                onClick={() => setToolbar((current) => current && { ...current, mode: 'input' })}
              >
                要求更改
              </button>
              <span className="canvas-selection-divider" />
              <button type="button" className="canvas-selection-icon" onClick={() => applyRichCommand('bold')} title="加粗">
                B
              </button>
              <button type="button" className="canvas-selection-icon italic" onClick={() => applyRichCommand('italic')} title="斜体">
                I
              </button>
              <button type="button" className="canvas-selection-command" onClick={() => applyBlockCommand('h2')}>
                文本
              </button>
            </>
          ) : (
            <form
              className="canvas-selection-input-form"
              onSubmit={(event) => {
                event.preventDefault();
                requestEdit(selection ? 'selection' : 'document');
              }}
            >
              <input
                ref={inputRef}
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder={isRevising ? '修订中…' : '修改一下'}
                className="canvas-selection-input"
                disabled={aiDisabled || isRevising}
              />
              <button
                type="submit"
                className="canvas-selection-send"
                disabled={!canRequestSelection && !canRequestDocument}
                title="发送修改要求"
              >
                ↑
              </button>
            </form>
          )}
        </div>
      )}
    </div>
  );
}
