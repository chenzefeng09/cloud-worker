import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  CANVAS_OBJECTS,
  buildCanvasEditPrompt,
  createCanvasDraft,
  getCanvasObject,
  sanitizeCanvasDraft,
  type CanvasDocumentDraft,
  type CanvasEditIntent,
  type CanvasEditRequest,
  type CanvasObjectKind,
  type CanvasTextSelection,
} from '../lib/canvasObjects';
import { useCopied, CopyIcon } from './CopyButton';

interface ArticleCanvasPanelProps {
  sessionId: string;
  draft: CanvasDocumentDraft;
  aiDisabled?: boolean;
  onDraftChange: (draft: CanvasDocumentDraft) => void;
  onClose: () => void;
  onRequestEdit: (request: CanvasEditRequest, prompt: string) => void | Promise<void>;
}

const HISTORY_LIMIT = 80;
const STORAGE_PREFIX = 'article-canvas-draft-v1';

function storageKey(sessionId: string): string {
  return `${STORAGE_PREFIX}:${sessionId}`;
}

export function loadStoredCanvasDraft(sessionId: string): CanvasDocumentDraft | null {
  try {
    const raw = localStorage.getItem(storageKey(sessionId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CanvasDocumentDraft>;
    if (!parsed.id || !parsed.kind || typeof parsed.content !== 'string') return null;
    return sanitizeCanvasDraft({
      id: parsed.id,
      kind: parsed.kind,
      title: parsed.title || getCanvasObject(parsed.kind).defaultTitle,
      content: parsed.content,
      sourceMessageId: parsed.sourceMessageId,
      updatedAt: parsed.updatedAt || new Date().toISOString(),
    });
  } catch {
    return null;
  }
}

function getSelectionInside(root: HTMLElement, markdown: string): CanvasTextSelection | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0);
  if (range.collapsed) return null;
  if (!root.contains(range.commonAncestorContainer)) return null;

  const before = range.cloneRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);

  const text = range.toString();
  if (!text.trim()) return null;

  const start = before.toString().length;
  return findMarkdownSelection(markdown, text, start);
}

function trimHistory(items: string[]): string[] {
  return items.length > HISTORY_LIMIT ? items.slice(items.length - HISTORY_LIMIT) : items;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inlineMarkdownToHtml(value: string): string {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function splitTableRow(line: string): string[] {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((cell) => cell.trim());
}

function isTableSeparator(line: string | undefined): boolean {
  if (!line) return false;
  return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
}

function isBlockBoundary(line: string, nextLine?: string): boolean {
  return (
    line.trim() === '' ||
    /^(#{1,3})\s+/.test(line) ||
    /^-{3,}$/.test(line.trim()) ||
    /^>\s?/.test(line) ||
    /^[-*]\s+/.test(line) ||
    /^\d+\.\s+/.test(line) ||
    isTableSeparator(nextLine)
  );
}

function markdownToCanvasHtml(markdown: string): string {
  const lines = markdown.replace(/\r\n/g, '\n').split('\n');
  const html: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    if (line.includes('|') && isTableSeparator(lines[i + 1])) {
      const head = splitTableRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitTableRow(lines[i]));
        i += 1;
      }
      html.push(
        `<table><thead><tr>${head.map((cell) => `<th>${inlineMarkdownToHtml(cell)}</th>`).join('')}</tr></thead><tbody>${rows
          .map((row) => `<tr>${row.map((cell) => `<td>${inlineMarkdownToHtml(cell)}</td>`).join('')}</tr>`)
          .join('')}</tbody></table>`,
      );
      continue;
    }

    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      html.push(`<h${heading[1].length}>${inlineMarkdownToHtml(heading[2])}</h${heading[1].length}>`);
      i += 1;
      continue;
    }

    if (/^-{3,}$/.test(trimmed)) {
      html.push('<hr>');
      i += 1;
      continue;
    }

    if (/^>\s?/.test(line)) {
      const parts: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        parts.push(lines[i].replace(/^>\s?/, ''));
        i += 1;
      }
      html.push(`<blockquote>${inlineMarkdownToHtml(parts.join('\n')).replace(/\n/g, '<br>')}</blockquote>`);
      continue;
    }

    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^[-*]\s+/, ''));
        i += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${inlineMarkdownToHtml(item)}</li>`).join('')}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\d+\.\s+/, ''));
        i += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${inlineMarkdownToHtml(item)}</li>`).join('')}</ol>`);
      continue;
    }

    const paragraph: string[] = [];
    while (i < lines.length && !isBlockBoundary(lines[i], lines[i + 1])) {
      paragraph.push(lines[i].trim());
      i += 1;
    }
    html.push(`<p>${inlineMarkdownToHtml(paragraph.join(' '))}</p>`);
  }

  return html.length > 0 ? html.join('') : '<p><br></p>';
}

function nodeChildrenToMarkdown(node: Node): string {
  return Array.from(node.childNodes).map((child) => nodeToMarkdown(child)).join('');
}

function nodeToMarkdown(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent ?? '';
  if (!(node instanceof HTMLElement)) return '';

  const content = nodeChildrenToMarkdown(node);
  switch (node.tagName.toLowerCase()) {
    case 'strong':
    case 'b':
      return `**${content}**`;
    case 'em':
    case 'i':
      return `*${content}*`;
    case 'code':
      return `\`${content}\``;
    case 'br':
      return '\n';
    case 'a': {
      const href = node.getAttribute('href');
      return href ? `[${content}](${href})` : content;
    }
    default:
      return content;
  }
}

function blockToMarkdown(node: ChildNode): string {
  if (node.nodeType === Node.TEXT_NODE) return (node.textContent ?? '').trim();
  if (!(node instanceof HTMLElement)) return '';

  const tag = node.tagName.toLowerCase();
  const content = nodeChildrenToMarkdown(node).trim();
  if (!content && tag !== 'hr') return '';

  if (tag === 'h1') return `# ${content}`;
  if (tag === 'h2') return `## ${content}`;
  if (tag === 'h3') return `### ${content}`;
  if (tag === 'blockquote') {
    return content
      .split('\n')
      .map((line) => `> ${line}`)
      .join('\n');
  }
  if (tag === 'ul') {
    return Array.from(node.children)
      .filter((child) => child.tagName.toLowerCase() === 'li')
      .map((child) => `- ${nodeChildrenToMarkdown(child).trim()}`)
      .join('\n');
  }
  if (tag === 'ol') {
    return Array.from(node.children)
      .filter((child) => child.tagName.toLowerCase() === 'li')
      .map((child, index) => `${index + 1}. ${nodeChildrenToMarkdown(child).trim()}`)
      .join('\n');
  }
  if (tag === 'hr') return '---';
  if (tag === 'table') {
    const rows = Array.from(node.querySelectorAll('tr')).map((row) =>
      Array.from(row.children).map((cell) => nodeChildrenToMarkdown(cell).replace(/\s+/g, ' ').trim()),
    );
    if (rows.length === 0) return '';
    const [head, ...body] = rows;
    return [
      `| ${head.join(' | ')} |`,
      `| ${head.map(() => '---').join(' | ')} |`,
      ...body.map((row) => `| ${row.join(' | ')} |`),
    ].join('\n');
  }
  if (tag === 'pre') return `\`\`\`\n${node.textContent?.trim() ?? ''}\n\`\`\``;
  return content;
}

function canvasHtmlToMarkdown(root: HTMLElement): string {
  return Array.from(root.childNodes)
    .map((node) => blockToMarkdown(node))
    .filter(Boolean)
    .join('\n\n')
    .trim();
}

function markdownToPlainText(markdown: string): string {
  const host = document.createElement('div');
  host.innerHTML = markdownToCanvasHtml(markdown);
  return Array.from(host.childNodes)
    .map((node) => node.textContent?.trim() ?? '')
    .filter(Boolean)
    .join('\n');
}

function findMarkdownSelection(markdown: string, selectedText: string, renderedStart: number): CanvasTextSelection {
  const text = selectedText.replace(/\u00a0/g, ' ');
  const matches: number[] = [];
  let index = markdown.indexOf(text);
  while (index !== -1) {
    matches.push(index);
    index = markdown.indexOf(text, index + Math.max(1, text.length));
  }

  if (matches.length === 1) {
    return { text, start: matches[0], end: matches[0] + text.length };
  }

  if (matches.length > 1) {
    let best = matches[0];
    let bestDistance = Number.POSITIVE_INFINITY;
    for (const match of matches) {
      const plainBefore = markdownToPlainText(markdown.slice(0, match)).length;
      const distance = Math.abs(plainBefore - renderedStart);
      if (distance < bestDistance) {
        best = match;
        bestDistance = distance;
      }
    }
    return { text, start: best, end: best + text.length };
  }

  return { text, start: renderedStart, end: renderedStart + text.length };
}

export function ArticleCanvasPanel({
  sessionId,
  draft,
  aiDisabled,
  onDraftChange,
  onClose,
  onRequestEdit,
}: ArticleCanvasPanelProps) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const hydratedDraftIdRef = useRef<string | null>(null);
  const localEditContentRef = useRef<string | null>(null);
  const { copied, copy } = useCopied();
  const object = getCanvasObject(draft.kind);
  const [selection, setSelection] = useState<CanvasTextSelection | null>(null);
  const [instruction, setInstruction] = useState('');
  const [busy, setBusy] = useState(false);
  const [fullScreen, setFullScreen] = useState(false);
  const [history, setHistory] = useState<string[]>([]);
  const [future, setFuture] = useState<string[]>([]);

  const selectedChars = selection ? selection.end - selection.start : 0;
  const activeIntents = object.editIntents;

  useEffect(() => {
    const el = editorRef.current;
    if (!el) return;

    if (hydratedDraftIdRef.current !== draft.id) {
      hydratedDraftIdRef.current = draft.id;
      localEditContentRef.current = null;
      el.innerHTML = markdownToCanvasHtml(draft.content);
      setHistory([]);
      setFuture([]);
      setSelection(null);
      return;
    }

    if (localEditContentRef.current === draft.content) {
      localEditContentRef.current = null;
      return;
    }

    const current = canvasHtmlToMarkdown(el);
    if (current !== draft.content) {
      setHistory((prev) => trimHistory([...prev, current]));
      setFuture([]);
      el.innerHTML = markdownToCanvasHtml(draft.content);
      setSelection(null);
    }
  }, [draft.content, draft.id]);

  useEffect(() => {
    try {
      localStorage.setItem(storageKey(sessionId), JSON.stringify(draft));
    } catch {
      // Local persistence is best-effort; backend document APIs are reserved.
    }
  }, [draft, sessionId]);

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

  const syncEditorContent = useCallback((text: string) => {
    if (editorRef.current && canvasHtmlToMarkdown(editorRef.current) !== text) {
      editorRef.current.innerHTML = markdownToCanvasHtml(text);
    }
  }, []);

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

  const refreshSelection = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    setSelection(getSelectionInside(el, draft.content));
  }, [draft.content]);

  const handleInput = useCallback(() => {
    const next = editorRef.current ? canvasHtmlToMarkdown(editorRef.current) : '';
    commitContent(next);
    refreshSelection();
  }, [commitContent, refreshSelection]);

  const handleKindChange = (kind: CanvasObjectKind) => {
    const nextObject = getCanvasObject(kind);
    const currentObject = getCanvasObject(draft.kind);
    updateDraft({
      kind,
      title: draft.title === currentObject.defaultTitle ? nextObject.defaultTitle : draft.title,
    });
  };

  const newDraft = (kind: CanvasObjectKind = draft.kind) => {
    const next = createCanvasDraft(kind);
    onDraftChange(next);
  };

  const commitEditorDom = () => {
    const el = editorRef.current;
    if (!el) return;
    const next = canvasHtmlToMarkdown(el);
    commitContent(next);
    refreshSelection();
  };

  const applyRichCommand = (command: 'bold' | 'italic') => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    document.execCommand(command);
    commitEditorDom();
  };

  const applyBlockCommand = (tag: 'h1' | 'h2' | 'blockquote') => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    const applied = document.execCommand('formatBlock', false, tag);
    if (!applied) {
      document.execCommand('formatBlock', false, `<${tag}>`);
    }
    commitEditorDom();
  };

  const keepEditorSelection = (event: React.MouseEvent<HTMLButtonElement>) => {
    event.preventDefault();
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
    if (aiDisabled) return;
    const request = buildRequest(scope, intent);
    if (!request) return;
    setBusy(true);
    try {
      await onRequestEdit(request, buildCanvasEditPrompt(request));
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

  const shellClass = fullScreen
    ? 'fixed inset-0 z-50 flex flex-col bg-paper'
    : 'flex flex-col h-full bg-paper border-l border-line';

  const wordCount = useMemo(
    () => draft.content.trim().split(/\s+/).filter(Boolean).length,
    [draft.content],
  );

  return (
    <div className={shellClass} style={{ width: '100%', minWidth: 0, overflow: 'hidden' }}>
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line shrink-0">
        <select
          value={draft.kind}
          onChange={(e) => handleKindChange(e.target.value as CanvasObjectKind)}
          className="border border-line bg-surface rounded-sm px-2 py-1 text-[12px] text-ink focus:outline-none focus:border-accent"
          title="对象类型"
        >
          {CANVAS_OBJECTS.map((item) => (
            <option key={item.kind} value={item.kind}>
              {item.label}
            </option>
          ))}
        </select>
        <input
          value={draft.title}
          onChange={(e) => updateDraft({ title: e.target.value })}
          className="flex-1 min-w-0 bg-transparent border-none outline-none text-[13px] font-semibold text-ink"
          aria-label="画布标题"
        />
        <button
          type="button"
          onClick={() => copy(draft.content)}
          className="cw-btn cw-btn-ghost cw-btn-sm"
          title="复制"
          aria-label="复制"
        >
          {copied ? <span className="font-mono text-[10px]">已复制</span> : <CopyIcon />}
        </button>
        <button
          type="button"
          onClick={() => setFullScreen((v) => !v)}
          className="cw-btn cw-btn-ghost cw-btn-sm"
          title={fullScreen ? '退出全屏' : '全屏'}
          aria-label={fullScreen ? '退出全屏' : '全屏'}
        >
          {fullScreen ? '□' : '⛶'}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="cw-btn cw-btn-ghost cw-btn-sm font-mono text-[12px] text-dim leading-none px-1.5 py-0.5"
          aria-label="关闭画布"
        >
          ✕
        </button>
      </div>

      <div className="flex items-center gap-1 px-3 py-2 border-b border-line-2 shrink-0 overflow-x-auto">
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
          onMouseDown={keepEditorSelection}
          onClick={() => applyRichCommand('bold')}
          title="加粗"
        >
          B
        </button>
        <button
          type="button"
          className="cw-btn cw-btn-ghost cw-btn-sm italic"
          onMouseDown={keepEditorSelection}
          onClick={() => applyRichCommand('italic')}
          title="斜体"
        >
          I
        </button>
        <button
          type="button"
          className="cw-btn cw-btn-ghost cw-btn-sm"
          onMouseDown={keepEditorSelection}
          onClick={() => applyBlockCommand('h1')}
          title="标题 1"
        >
          H1
        </button>
        <button
          type="button"
          className="cw-btn cw-btn-ghost cw-btn-sm"
          onMouseDown={keepEditorSelection}
          onClick={() => applyBlockCommand('h2')}
          title="标题 2"
        >
          H2
        </button>
        <button
          type="button"
          className="cw-btn cw-btn-ghost cw-btn-sm"
          onMouseDown={keepEditorSelection}
          onClick={() => applyBlockCommand('blockquote')}
          title="引用"
        >
          “
        </button>
        <span className="h-5 w-px bg-line mx-1 shrink-0" />
        <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={() => newDraft()} title="新建">
          +
        </button>
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-dim whitespace-nowrap">
          {draft.content.length} 字符 · {wordCount} 词
        </span>
      </div>

      <div className="cw-scroll flex-1 min-h-0 px-4 py-4">
        <div
          ref={editorRef}
          contentEditable
          suppressContentEditableWarning
          className="canvas-rich-editor outline-none text-[14px] leading-[1.72] text-ink break-words min-h-[520px]"
          style={{
            maxWidth: 760,
            margin: '0 auto',
            caretColor: 'var(--accent)',
          }}
          role="textbox"
          aria-label="文章画布"
          onInput={handleInput}
          onMouseUp={refreshSelection}
          onKeyUp={refreshSelection}
        />
      </div>

      <div className="border-t border-line bg-paper px-3 py-3 shrink-0">
        <div className="flex flex-wrap gap-1.5 mb-2">
          {activeIntents.map((intent) => (
            <button
              key={intent.id}
              type="button"
              onClick={() => requestEdit(selection ? 'selection' : 'document', intent)}
              disabled={busy || aiDisabled}
              className="cw-btn cw-btn-ghost cw-btn-sm"
            >
              {intent.label}
            </button>
          ))}
        </div>
        <div className="flex items-end gap-2">
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={2}
            className="flex-1 resize-none border border-line rounded-md bg-surface px-2.5 py-2 text-[12.5px] leading-[1.45] outline-none focus:border-accent"
            placeholder={selection ? `已选中 ${selectedChars} 字符，输入修改要求…` : '输入整篇修改要求…'}
          />
          <div className="flex flex-col gap-1.5 shrink-0">
            <button
              type="button"
              className="cw-btn cw-btn-accent cw-btn-sm disabled:opacity-40"
              disabled={!instruction.trim() || busy || aiDisabled}
              onClick={() => requestEdit('document')}
            >
              整篇
            </button>
            <button
              type="button"
              className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-40"
              disabled={!selection || !instruction.trim() || busy || aiDisabled}
              onClick={() => requestEdit('selection')}
            >
              选区
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
