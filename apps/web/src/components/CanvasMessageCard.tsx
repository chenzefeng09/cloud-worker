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
  canvasBlocksToHtml,
  canvasEditorToBlocks,
  canvasHtmlToMarkdown,
  getCanvasBlockSelectionsInside,
  getMarkdownSelectionInside,
  markdownToCanvasHtml,
} from '../lib/canvasRichText';
import { CopyIcon, useCopied } from './CopyButton';
import { useCanvasStore } from '../stores/canvasStore';
import { useChatStore } from '../stores/chatStore';
import { useConfirm } from '../admin/components/dialogs';
import { api } from '../api/client';
import type { CanvasSelectionSegment, CanvasVersionSummary } from '../api/types';
import { InlineCanvasSuggestion } from './InlineCanvasSuggestion';
import { CanvasHtmlPreview } from './CanvasHtmlPreview';
import { canvasHtmlSource } from '../lib/canvasHtml';

interface CanvasMessageCardProps {
  sessionId: string;
  /** Draft mode (canvas.v2 OFF / legacy path). Mutually exclusive with `canvasId`. */
  draft?: CanvasDocumentDraft;
  /**
   * When set, the card binds to a server canvas object from canvasStore
   * instead of `draft`. Mutually exclusive with `draft`.
   */
  canvasId?: string;
  /** True while the underlying creation run streams — freezes the editor. */
  aiDisabled?: boolean;
  pending?: boolean;
  /** True while a silent canvas revision request is in flight. */
  revising?: boolean;
  /** Localised error from the last failed revision (chat-invisible). */
  editError?: string | null;
  onDraftChange?: (draft: CanvasDocumentDraft) => void;
  onRequestEdit?: (request: CanvasEditRequest) => void | Promise<void>;
  onDismissEditError?: () => void;
  onOpenPanel?: () => void;
  suggestion?: CanvasEditSuggestion | null;
  onAcceptSuggestion?: () => void;
  onRejectSuggestion?: () => void;
}

/** Random 8-char id suffix for newly-split blocks created from manual edits. */
function randomBlockSuffix(): string {
  return Math.random().toString(36).slice(2, 10);
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
// CSS Custom Highlight API 的注册名:进入「要求更改」输入态后用它给选区打持久高亮。
const PENDING_EDIT_HIGHLIGHT = 'canvas-pending-edit';

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

// Placeholder empty draft used only to keep hook order stable when in
// canvasId mode before the store has loaded the canvas. Never rendered from —
// the component returns null in that case (checked after all hooks run).
const EMPTY_DRAFT: CanvasDocumentDraft = {
  id: '',
  kind: 'article',
  title: '',
  content: '',
  updatedAt: '',
};

export function CanvasMessageCard({
  sessionId,
  draft: draftProp,
  canvasId,
  aiDisabled,
  pending,
  revising,
  editError,
  onDraftChange,
  onRequestEdit,
  onDismissEditError,
  onOpenPanel,
  suggestion: suggestionProp,
  onAcceptSuggestion: onAcceptSuggestionProp,
  onRejectSuggestion: onRejectSuggestionProp,
}: CanvasMessageCardProps) {
  // ── canvasId mode (canvas.v2 ON): pull from the server-canvas store ────────
  // Selectors read primitives / stable references only (never a fresh object
  // literal) — see canvasStore.ts's selector-safety note. Hooks are always
  // called unconditionally regardless of mode, per Rules of Hooks.
  const storeCanvas = useCanvasStore((s) => (canvasId ? s.canvasesById[canvasId] : undefined));
  const storeSuggestion = useCanvasStore((s) =>
    canvasId ? s.suggestionsByCanvasId[canvasId] ?? null : null,
  );
  const storeRevising = useCanvasStore((s) => (canvasId ? s.revisingCanvasIds[canvasId] ?? false : false));
  const storeError = useCanvasStore((s) => (canvasId ? s.errorByCanvasId[canvasId] ?? null : null));
  const requestEditAction = useCanvasStore((s) => s.requestEdit);
  const applySuggestionAction = useCanvasStore((s) => s.applySuggestion);
  const saveManualAction = useCanvasStore((s) => s.saveManual);
  const revertAction = useCanvasStore((s) => s.revert);
  const clearErrorAction = useCanvasStore((s) => s.clearError);
  const confirm = useConfirm();

  // Derive a CanvasDocumentDraft-shaped view from the server canvas so the
  // existing rich-editor JSX (below) can render either mode unmodified.
  const derivedDraft: CanvasDocumentDraft | null = useMemo(() => {
    if (!canvasId || !storeCanvas) return null;
    return {
      id: storeCanvas.id,
      kind: (storeCanvas.kind as CanvasObjectKind) || 'article',
      title: storeCanvas.title,
      content: storeCanvas.blocks.map((b) => b.text).join('\n\n'),
      updatedAt: storeCanvas.updated_at,
    };
  }, [canvasId, storeCanvas]);

  const missingCanvas = Boolean(canvasId) && !derivedDraft;
  // `draft` is always non-null while hooks run below; the "really missing"
  // case is handled by `missingCanvas` short-circuiting the render at the end.
  const draft = canvasId ? derivedDraft ?? EMPTY_DRAFT : draftProp ?? EMPTY_DRAFT;

  // canvasId-mode suggestion, reshaped into the CanvasEditSuggestion shape the
  // existing JSX below already knows how to render (whole-document form only —
  // per-block diff review is PR-4 scope). The real per-block orig/sugg text
  // (`storeSuggestion.diffs`) is rendered directly in a lightweight extra
  // section below rather than forced through the character-diff path.
  const derivedSuggestion: CanvasEditSuggestion | null = useMemo(() => {
    if (!canvasId || !storeSuggestion) return null;
    return {
      request: {
        documentId: canvasId,
        kind: (draft.kind as CanvasObjectKind) || 'article',
        title: draft.title,
        scope: 'document',
        instruction: storeSuggestion.instruction,
        content: draft.content,
      },
      replacement: draft.content,
      summary: storeSuggestion.change_summary,
      receivedAt: storeSuggestion.created_at,
    };
  }, [canvasId, draft.content, draft.kind, draft.title, storeSuggestion]);

  const suggestion = canvasId ? derivedSuggestion : suggestionProp;
  const revisingEffective = canvasId ? storeRevising : revising;
  const editErrorEffective = canvasId ? storeError : editError;
  const [blockSelection, setBlockSelection] = useState<CanvasSelectionSegment[]>([]);

  const handleDraftChange = useCallback(
    (next: CanvasDocumentDraft) => {
      if (!canvasId) {
        onDraftChange?.(next);
      }
      // canvasId mode: keystroke-level changes stay local to the contentEditable
      // DOM; persistence to the server happens on blur (handleEditorBlur below)
      // rather than on every keystroke, to avoid spamming saveManual.
    },
    [canvasId, onDraftChange],
  );

  const handleRequestEdit = useCallback(
    async (request: CanvasEditRequest) => {
      if (!canvasId) {
        await onRequestEdit?.(request);
        return;
      }
      const model = useChatStore.getState().selectedModel[sessionId] || undefined;
      const scope = request.scope === 'selection' && blockSelection.length > 0
        ? { type: 'selection' as const, segments: blockSelection }
        : { type: 'document' as const };
      await requestEditAction(canvasId, sessionId, {
        instruction: request.instruction,
        scope,
        model,
      });
    },
    [blockSelection, canvasId, onRequestEdit, requestEditAction, sessionId],
  );

  const handleAcceptSuggestion = useCallback(() => {
    if (!canvasId) {
      onAcceptSuggestionProp?.();
      return;
    }
    if (!storeSuggestion) return;
    setHistory((prev) => trimHistory([...prev, draft.content]));
    setFuture([]);
    void applySuggestionAction(canvasId, storeSuggestion.id, true);
  }, [applySuggestionAction, canvasId, draft.content, onAcceptSuggestionProp, storeSuggestion]);

  const handleRejectSuggestion = useCallback(() => {
    if (!canvasId) {
      onRejectSuggestionProp?.();
      return;
    }
    if (!storeSuggestion) return;
    void applySuggestionAction(canvasId, storeSuggestion.id, false);
  }, [applySuggestionAction, canvasId, onRejectSuggestionProp, storeSuggestion]);

  const handleDismissEditError = useCallback(() => {
    if (canvasId) {
      clearErrorAction(canvasId);
      return;
    }
    onDismissEditError?.();
  }, [canvasId, clearErrorAction, onDismissEditError]);

  const onAcceptSuggestion = canvasId ? handleAcceptSuggestion : onAcceptSuggestionProp;
  const onRejectSuggestion = canvasId ? handleRejectSuggestion : onRejectSuggestionProp;

  const editorRef = useRef<HTMLDivElement | null>(null);
  const hydratedDraftIdRef = useRef<string | null>(null);
  const localEditContentRef = useRef<string | null>(null);
  const selectedRangeRef = useRef<Range | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  const object = getCanvasObject(draft.kind);
  const { copied, copy } = useCopied();
  const [selection, setSelection] = useState<CanvasTextSelection | null>(null);
  const [toolbar, setToolbar] = useState<ToolbarState | null>(null);
  const [instruction, setInstruction] = useState('');
  // 悬浮选区弹框的指令与底部整篇输入框各自独立,避免互相镜像
  const [popupInstruction, setPopupInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [future, setFuture] = useState<string[]>([]);
  // ── Version history (canvasId mode only) ──────────────────────────────────
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [versions, setVersions] = useState<CanvasVersionSummary[] | null>(null);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
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
        setBlockSelection([]);
        setToolbar(null);
      }
      return;
    }

    if (hydratedDraftIdRef.current !== draft.id) {
      hydratedDraftIdRef.current = draft.id;
      localEditContentRef.current = null;
      el.innerHTML = canvasId && storeCanvas
        ? canvasBlocksToHtml(storeCanvas.blocks)
        : markdownToCanvasHtml(draft.content);
      setHistory([]);
      setFuture([]);
      setSelection(null);
      setBlockSelection([]);
      setToolbar(null);
      return;
    }

    if (localEditContentRef.current === draft.content) {
      localEditContentRef.current = null;
      return;
    }

    if (!el.innerHTML.trim() && draft.content.trim()) {
      el.innerHTML = canvasId && storeCanvas
        ? canvasBlocksToHtml(storeCanvas.blocks)
        : markdownToCanvasHtml(draft.content);
      setSelection(null);
      setBlockSelection([]);
      setToolbar(null);
      return;
    }

    const current = canvasHtmlToMarkdown(el);
    if (current !== draft.content) {
      setHistory((prev) => trimHistory([...prev, current]));
      setFuture([]);
      el.innerHTML = canvasId && storeCanvas
        ? canvasBlocksToHtml(storeCanvas.blocks)
        : markdownToCanvasHtml(draft.content);
      setSelection(null);
      setBlockSelection([]);
      setToolbar(null);
    }
  }, [canvasId, draft.content, draft.id, hasInlineSuggestionPreview, storeCanvas]);

  useEffect(() => {
    // canvasId mode is server-persisted (saveManual on blur) — the legacy
    // localStorage draft mirror only applies to the in-memory draft mode.
    if (canvasId) return;
    try {
      localStorage.setItem(storageKey(sessionId), JSON.stringify(draft));
    } catch {
      // Canvas persistence remains best-effort until backend document APIs land.
    }
  }, [canvasId, draft, sessionId]);

  useEffect(() => {
    if (toolbar?.mode !== 'input') return;
    // 同步聚焦一次:后台/隐藏标签页里 rAF 会被冻结,不能只依赖它
    inputRef.current?.focus();
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [toolbar]);

  useEffect(() => {
    if (!toolbar) return;
    const handlePointerDown = (event: PointerEvent) => {
      const popover = popoverRef.current;
      if (popover && event.target instanceof Node && popover.contains(event.target)) return;
      setToolbar(null);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      // isComposing: Escape 取消输入法组合时不应关闭弹框
      if (event.key === 'Escape' && !event.isComposing) setToolbar(null);
    };
    document.addEventListener('pointerdown', handlePointerDown, true);
    document.addEventListener('keydown', handleKeyDown, true);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown, true);
      document.removeEventListener('keydown', handleKeyDown, true);
    };
  }, [toolbar]);

  useEffect(() => {
    // 进入「要求更改」输入态后,焦点移入指令输入框会清掉 contenteditable 的原生选区高亮。
    // 用 CSS Custom Highlight API 给选中的 Range 打一层持久高亮,让用户始终看得到要改的是哪一段;
    // 该高亮不改动 DOM,不会污染 markdown 序列化。旧浏览器无此 API 时静默降级。
    const registry = typeof CSS !== 'undefined' ? CSS.highlights : undefined;
    if (!registry || typeof Highlight === 'undefined') return;
    const range = selectedRangeRef.current;
    if (toolbar?.mode !== 'input' || !range) {
      registry.delete(PENDING_EDIT_HIGHLIGHT);
      return;
    }
    registry.set(PENDING_EDIT_HIGHLIGHT, new Highlight(range));
    return () => {
      registry.delete(PENDING_EDIT_HIGHLIGHT);
    };
  }, [toolbar?.mode]);

  const updateDraft = useCallback(
    (patch: Partial<CanvasDocumentDraft>) => {
      handleDraftChange({
        ...draft,
        ...patch,
        updatedAt: new Date().toISOString(),
      });
    },
    [draft, handleDraftChange],
  );

  // Manual-edit save (canvasId mode only): stable ids are read from the DOM,
  // so deleting or inserting an earlier paragraph cannot rebind later text to
  // the wrong block.
  const handleEditorBlur = useCallback(() => {
    if (!canvasId || !storeCanvas || !editorRef.current) return;
    const content = canvasHtmlToMarkdown(editorRef.current);
    const currentContent = storeCanvas.blocks.map((b) => b.text).join('\n\n');
    if (content === currentContent) return;
    const blocks = canvasEditorToBlocks(
      editorRef.current,
      storeCanvas.blocks,
      () => `blk_${randomBlockSuffix()}`,
    );
    void saveManualAction(canvasId, blocks, storeCanvas.current_version);
  }, [canvasId, saveManualAction, storeCanvas]);

  // ── Version history (canvasId mode only) ──────────────────────────────────
  // Lightweight: toggle a dropdown, lazily fetch the version list, revert to a
  // chosen version (append a new version server-side). No diff preview.
  const toggleVersions = useCallback(() => {
    if (!canvasId) return;
    setVersionsOpen((open) => {
      const next = !open;
      if (next) {
        setVersionsLoading(true);
        setVersionsError(null);
        api.canvas
          .versions(canvasId)
          .then((res) => setVersions(res.items))
          .catch((err) => {
            setVersionsError(
              err instanceof Error && err.message ? err.message : 'Couldn’t load version history.',
            );
          })
          .finally(() => setVersionsLoading(false));
      }
      return next;
    });
  }, [canvasId]);

  const handleRevert = useCallback(
    async (toVersion: number) => {
      if (!canvasId) return;
      const ok = await confirm({
        title: 'Restore version',
        message: `Restore version ${toVersion}? A new version will be created from it.`,
        confirmText: 'Restore',
      });
      if (!ok) return;
      const reverted = await revertAction(canvasId, toVersion);
      if (reverted) {
        setVersionsOpen(false);
        setVersions(null);
      }
    },
    [canvasId, revertAction, confirm],
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
        setBlockSelection([]);
        if (toolbar?.mode !== 'input') setToolbar(null);
        return;
      }
      setSelection(next);
      setBlockSelection(canvasId && storeCanvas ? getCanvasBlockSelectionsInside(el, storeCanvas.blocks) : []);
      showToolbarForRange(range, mode, fallback);
    },
    [canvasId, draft.content, showToolbarForRange, storeCanvas, toolbar?.mode],
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

  const buildRequest = (
    scope: 'selection' | 'document',
    intent?: CanvasEditIntent,
    instructionText?: string,
  ): CanvasEditRequest | null => {
    const finalInstruction = (intent?.instruction || (instructionText ?? instruction)).trim();
    if (!finalInstruction) return null;
    if (scope === 'selection' && (!selection || (canvasId && blockSelection.length === 0))) return null;
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

  const requestEdit = async (
    scope: 'selection' | 'document',
    intent?: CanvasEditIntent,
    instructionText?: string,
  ) => {
    if (aiDisabled || busy || revisingEffective) return;
    const request = buildRequest(scope, intent, instructionText);
    if (!request) return;
    setBusy(true);
    setToolbar(null);
    try {
      await handleRequestEdit(request);
      if (instructionText !== undefined) {
        setPopupInstruction('');
      } else {
        setInstruction('');
        if (intent) setInstruction(intent.instruction);
      }
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

  const isRevising = Boolean(busy || revisingEffective);
  const canRequestDocument = Boolean(instruction.trim() && !aiDisabled && !isRevising);
  const canRequestPopupEdit = Boolean(selection && popupInstruction.trim() && !aiDisabled && !isRevising);
  const suggestionSource =
    suggestion?.request.scope === 'selection' ? suggestion.request.selection?.text : suggestion?.request.content;
  // canvasId mode renders every active suggestion (selection- or block-level)
  // in place via InlineCanvasSuggestion — the full document with the changed
  // blocks highlighted — so the legacy whole-document review card below is only
  // used by the draft (non-canvasId) path.
  const showDocumentSuggestion = Boolean(suggestion && !inlineSuggestionPreview && !canvasId);
  const showCanvasIdSuggestion = Boolean(canvasId && storeCanvas && storeSuggestion);

  // canvasId mode before the canvas has loaded into the store (or, in
  // practice never, neither draft nor canvasId supplied): render nothing.
  // This check runs after all hooks above so hook order stays stable
  // regardless of `missingCanvas`. The caller (ChatWindow) already gates
  // rendering on the canvas being present in the store, so this is a
  // defensive no-op in normal operation.
  if (missingCanvas) return null;

  // Executable canvas (content_type="html"): render a sandboxed live preview
  // instead of the prose editor. Detected from the server canvas' single html
  // block; prose canvases return null here and fall through to the editor below.
  // (All hooks above have already run, so this early return keeps hook order
  // stable.) Editing is intentionally disabled for html canvases in MVP — they
  // are runnable artifacts, viewed/copied, not inline-edited.
  const htmlSource = canvasId ? canvasHtmlSource(storeCanvas) : null;
  if (htmlSource !== null) {
    return (
      <div className="canvas-message-shell">
        <article className="canvas-message-card">
          <header className="canvas-message-card-head">
            <div className="flex items-center gap-2 min-w-0">
              <span className="canvas-card-edit-chip" title="Runnable web page">
                <span className="font-mono">▶</span>
                <span>Web page</span>
              </span>
              <span
                className="truncate text-[13px] font-medium text-ink"
                title={draft.title}
              >
                {draft.title}
              </span>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                type="button"
                className="cw-btn cw-btn-ghost cw-btn-sm"
                onClick={() => copy(htmlSource)}
                title="Copy source"
                aria-label="Copy source"
              >
                {copied ? <span className="font-mono text-[10px]">Copied</span> : <CopyIcon />}
              </button>
              {onOpenPanel && (
                <button
                  type="button"
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  onClick={onOpenPanel}
                  title="Open in side panel"
                  aria-label="Open in side panel"
                >
                  ⛶
                </button>
              )}
            </div>
          </header>
          <CanvasHtmlPreview
            html={htmlSource}
            title={draft.title}
            reloadKey={storeCanvas?.current_version}
          />
        </article>
      </div>
    );
  }

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
              <span>Edit</span>
            </button>
            <select
              value={draft.kind}
              onChange={(event) => handleKindChange(event.target.value as CanvasObjectKind)}
              className="canvas-card-kind-select"
              title="Object type"
            >
              {CANVAS_OBJECTS.map((item) => (
                <option key={item.kind} value={item.kind}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {canvasId && (
              <div className="canvas-card-history">
                <button
                  type="button"
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  onClick={toggleVersions}
                  title="Version history"
                  aria-label="Version history"
                  aria-expanded={versionsOpen}
                >
                  History
                </button>
                {versionsOpen && (
                  <div className="canvas-card-history-pop" role="menu" aria-label="Version history">
                    <div className="canvas-card-history-head">Version history</div>
                    {versionsLoading && (
                      <div className="canvas-card-history-empty">Loading…</div>
                    )}
                    {versionsError && (
                      <div className="canvas-card-history-error" role="alert">
                        {versionsError}
                      </div>
                    )}
                    {!versionsLoading && !versionsError && versions && versions.length === 0 && (
                      <div className="canvas-card-history-empty">No previous versions.</div>
                    )}
                    {!versionsLoading &&
                      !versionsError &&
                      versions &&
                      versions.map((v) => (
                        <button
                          key={v.id}
                          type="button"
                          role="menuitem"
                          className="canvas-card-history-item"
                          onClick={() => void handleRevert(v.version)}
                          disabled={revisingEffective}
                          title={`Restore version ${v.version}`}
                        >
                          <span className="canvas-card-history-ver">v{v.version}</span>
                          <span className="canvas-card-history-meta">
                            <span className="canvas-card-history-summary">
                              {v.change_summary?.trim() ||
                                (v.created_by === 'user' ? 'Manual edit' : 'AI edit')}
                            </span>
                            <span className="canvas-card-history-sub">
                              {new Date(v.created_at).toLocaleString()} · {v.created_by}
                            </span>
                          </span>
                        </button>
                      ))}
                  </div>
                )}
              </div>
            )}
            <button
              type="button"
              className="cw-btn cw-btn-ghost cw-btn-sm"
              onClick={() => copy(draft.content)}
              title="Copy"
              aria-label="Copy"
            >
              {copied ? <span className="font-mono text-[10px]">Copied</span> : <CopyIcon />}
            </button>
            {onOpenPanel && (
              <button
                type="button"
                className="cw-btn cw-btn-ghost cw-btn-sm"
                onClick={onOpenPanel}
                title="Open in side panel"
                aria-label="Open in side panel"
              >
                ⛶
              </button>
            )}
          </div>
        </header>

        <div className="canvas-message-card-toolbar">
          <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={undo} disabled={history.length === 0} title="Undo">
            ↶
          </button>
          <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={redo} disabled={future.length === 0} title="Redo">
            ↷
          </button>
          <span className="h-5 w-px bg-line mx-1 shrink-0" />
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm font-semibold"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyRichCommand('bold')}
            title="Bold"
          >
            B
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm italic"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyRichCommand('italic')}
            title="Italic"
          >
            I
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('h1')}
            title="Heading 1"
          >
            H1
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('h2')}
            title="Heading 2"
          >
            H2
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => applyBlockCommand('blockquote')}
            title="Quote"
          >
            “
          </button>
          <div className="flex-1" />
          <span className="font-mono text-[10.5px] text-dim whitespace-nowrap">
            {draft.content.length} chars · {wordCount} words
          </span>
        </div>

        <div className="canvas-message-card-body">
          <input
            value={draft.title}
            onChange={(event) => updateDraft({ title: event.target.value })}
            className="canvas-card-title-input"
            aria-label={`${object.label} title`}
          />
          {showDocumentSuggestion && suggestion && (
            <section
              className="canvas-suggestion-review"
              aria-label={suggestion.request.scope === 'selection' ? 'Suggested edit (selection)' : 'Suggested edit (whole document)'}
            >
              <div className="canvas-suggestion-review-head">
                <div className="min-w-0">
                  <div className="canvas-suggestion-kicker">
                    {suggestion.request.scope === 'selection' ? 'Suggested edit (selection)' : 'Suggested edit (whole document)'}
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
                    Discard
                  </button>
                  <button
                    type="button"
                    className="canvas-suggestion-accept"
                    onClick={acceptSuggestion}
                    disabled={!onAcceptSuggestion}
                  >
                    Accept
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
          {showCanvasIdSuggestion && storeCanvas && storeSuggestion ? (
            <InlineCanvasSuggestion
              canvas={storeCanvas}
              suggestion={storeSuggestion}
              onAccept={handleAcceptSuggestion}
              onReject={handleRejectSuggestion}
              busy={revisingEffective}
            />
          ) : inlineSuggestionPreview && suggestion ? (
            <div className="canvas-rich-editor canvas-inline-editor canvas-suggestion-preview-document text-ink break-words">
              <div
                className="canvas-suggestion-preview-segment"
                dangerouslySetInnerHTML={{ __html: inlineSuggestionPreview.beforeHtml }}
              />
              <section className="canvas-inline-suggestion-card" aria-label="Suggested edit (selection)">
                <div className="canvas-inline-suggestion-head">
                  <div className="min-w-0">
                    <div className="canvas-suggestion-kicker">Suggested edit (selection)</div>
                    <div className="canvas-suggestion-instruction">{suggestion.request.instruction}</div>
                  </div>
                  <div className="canvas-suggestion-actions">
                    <button
                      type="button"
                      className="canvas-suggestion-reject"
                      onClick={onRejectSuggestion}
                      disabled={!onRejectSuggestion}
                    >
                      Discard
                    </button>
                    <button
                      type="button"
                      className="canvas-suggestion-accept"
                      onClick={acceptSuggestion}
                      disabled={!onAcceptSuggestion}
                    >
                      Accept
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
              aria-label="Inline canvas body"
              data-writing-block-fullscreen-editor-region="true"
              data-writing-block-fullscreen-editor-layout="inline"
              onInput={handleInput}
              onBlur={handleEditorBlur}
              onMouseUp={() => refreshSelection('menu')}
              onKeyUp={() => refreshSelection('menu')}
              onContextMenu={handleContextMenu}
              onPaste={handlePaste}
            />
          )}
          {pending && (
            <div className="canvas-card-pending">
              <span className="cw-blip" />
              <span>Writing…</span>
            </div>
          )}
          {isRevising && !pending && (
            <div className="canvas-card-pending" role="status" aria-live="polite">
              <span className="cw-blip" />
              <span>Revising…</span>
            </div>
          )}
          {editErrorEffective && (
            <div className="canvas-card-edit-error" role="alert">
              <span className="canvas-card-edit-error-text">{editErrorEffective}</span>
              {(canvasId || onDismissEditError) && (
                <button
                  type="button"
                  className="canvas-card-edit-error-dismiss"
                  onClick={handleDismissEditError}
                  aria-label="Dismiss error"
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
                onClick={() => requestEdit('document', intent)}
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
              placeholder={isRevising ? 'Revising…' : 'Describe the changes you want…'}
              disabled={aiDisabled || isRevising}
            />
            <button
              type="button"
              className="cw-btn cw-btn-accent cw-btn-sm"
              disabled={!canRequestDocument}
              onClick={() => requestEdit('document')}
            >
              Revise
            </button>
          </div>
        </footer>
      </article>

      {toolbar && (
        <div
          ref={popoverRef}
          className="canvas-selection-popover"
          style={{
            left: toolbar.x,
            top: toolbar.y,
          }}
          onMouseDown={(event) => {
            // 保住编辑器选区（加粗/斜体按钮依赖它），但指令输入框要能点击聚焦
            if (event.target instanceof HTMLElement && event.target.closest('input')) return;
            event.preventDefault();
          }}
        >
          {toolbar.mode === 'menu' ? (
            <>
              <button
                type="button"
                className="canvas-selection-command"
                onClick={() => setToolbar((current) => current && { ...current, mode: 'input' })}
              >
                Request changes
              </button>
              <span className="canvas-selection-divider" />
              <button type="button" className="canvas-selection-icon" onClick={() => applyRichCommand('bold')} title="Bold">
                B
              </button>
              <button type="button" className="canvas-selection-icon italic" onClick={() => applyRichCommand('italic')} title="Italic">
                I
              </button>
              <button type="button" className="canvas-selection-command" onClick={() => applyBlockCommand('h2')}>
                Text
              </button>
            </>
          ) : (
            <form
              className="canvas-selection-input-form"
              onSubmit={(event) => {
                event.preventDefault();
                requestEdit('selection', undefined, popupInstruction);
              }}
            >
              <input
                ref={inputRef}
                value={popupInstruction}
                onChange={(event) => setPopupInstruction(event.target.value)}
                placeholder={isRevising ? 'Revising…' : 'Edit this'}
                className="canvas-selection-input"
                disabled={aiDisabled || isRevising}
              />
              <button
                type="submit"
                className="canvas-selection-send"
                disabled={!canRequestPopupEdit}
                title="Send edit request"
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
