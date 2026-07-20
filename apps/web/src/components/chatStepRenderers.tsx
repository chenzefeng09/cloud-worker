import { useState, useRef } from 'react';
import type { ToolCallEntry } from '../api/types';
import ReactMarkdown from 'react-markdown';
import { useCopied, CopyIcon } from './CopyButton';
import { useFilePreview } from '../lib/filePreviewContext';

// ── File-link interception (assistant replies) ───────────────────────────────
//
// The backend has the model emit `[filename](#file:<id>)` links, where `<id>`
// is an attachment (`att_…`) or workspace file (`wsf_…`). When a preview opener
// is available in context, we render these as in-app preview triggers instead
// of new-tab links; everything else stays a normal external link.
const FILE_LINK_RE = /^#file:((?:att_|wsf_)[A-Za-z0-9]+)$/;

/** Best-effort plain-text extraction from arbitrary markdown children. */
function nodeText(children: React.ReactNode): string {
  if (children == null || children === false) return '';
  if (typeof children === 'string' || typeof children === 'number') return String(children);
  if (Array.isArray(children)) return children.map(nodeText).join('');
  if (typeof children === 'object' && 'props' in (children as { props?: unknown })) {
    return nodeText((children as { props?: { children?: React.ReactNode } }).props?.children);
  }
  return '';
}

function MarkdownLink({ href, children }: { href?: string; children?: React.ReactNode }) {
  const openPreview = useFilePreview();
  const match = href ? FILE_LINK_RE.exec(href) : null;
  if (match && openPreview) {
    const id = match[1];
    const filename = nodeText(children) || id;
    return (
      <a
        href={href}
        onClick={(e) => {
          e.preventDefault();
          openPreview({ id, filename });
        }}
        className="text-accent underline underline-offset-2 hover:opacity-80 cursor-pointer"
      >
        <span aria-hidden className="mr-0.5">📄</span>
        {children}
      </a>
    );
  }
  return (
    <a
      href={href}
      className="text-accent underline underline-offset-2 hover:opacity-80"
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
    </a>
  );
}

// ── Code-block copy button component ─────────────────────────────────────────

function CodeBlockCopyButton({ preRef }: { preRef: React.RefObject<HTMLPreElement | null> }) {
  const { copied, copy } = useCopied();

  function handleCopy() {
    const text = preRef.current?.textContent ?? '';
    copy(text);
  }

  return (
    <button
      onClick={handleCopy}
      aria-label="复制代码"
      style={{
        position: 'absolute',
        top: 6,
        right: 6,
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: '2px 7px',
        fontFamily: 'var(--font-mono)',
        fontSize: 10.5,
        lineHeight: '1.4',
        color: copied ? 'var(--accent)' : 'var(--ink-2)',
        background: 'var(--surface)',
        border: '1px solid var(--line-2)',
        borderRadius: 'var(--r-sm)',
        cursor: 'pointer',
        transition: 'opacity 0.15s, color 0.15s',
        userSelect: 'none',
        whiteSpace: 'nowrap',
      }}
    >
      {copied ? <span>已复制</span> : <CopyIcon />}
    </button>
  );
}

// ── Code-block pre wrapper (supports hover-reveal copy button) ────────────────

function CodeBlockPre({ children }: { children?: React.ReactNode }) {
  const preRef = useRef<HTMLPreElement>(null);

  return (
    // Outer wrapper: position:relative so the button doesn't scroll with overflow-x.
    <div className="group relative my-3">
      {/* Hover-reveal copy button */}
      <div
        className="opacity-0 group-hover:opacity-100 transition-opacity duration-150"
        style={{ position: 'absolute', top: 0, right: 0, zIndex: 10, pointerEvents: 'auto' }}
      >
        <CodeBlockCopyButton preRef={preRef} />
      </div>
      {/* The actual <pre> — overflow-x-auto, no top margin (handled by outer div) */}
      <pre
        ref={preRef}
        className="rounded-md border border-line overflow-x-auto bg-surface"
        style={{ margin: 0 }}
      >
        {children}
      </pre>
    </div>
  );
}

// ── Table CSV export ─────────────────────────────────────────────────────────

/** Quote a CSV field per RFC 4180: wrap in quotes if it contains `,`, `"` or newline. */
function csvField(value: string): string {
  const v = value.replace(/\s+/g, ' ').trim();
  if (/[",\n\r]/.test(v)) {
    return `"${v.replace(/"/g, '""')}"`;
  }
  return v;
}

/** Read a rendered <table> DOM node and serialize its rows/cells to CSV text. */
function tableToCSV(table: HTMLTableElement): string {
  const rows: string[] = [];
  for (const tr of Array.from(table.rows)) {
    const cells = Array.from(tr.cells).map((c) => csvField(c.textContent ?? ''));
    rows.push(cells.join(','));
  }
  // Prefix a UTF-8 BOM so Excel opens Chinese text without mojibake.
  return '﻿' + rows.join('\r\n');
}

function downloadCSV(table: HTMLTableElement) {
  const blob = new Blob([tableToCSV(table)], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'table.csv';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// ── Table wrapper (supports hover-reveal CSV download button) ─────────────────

function TableBlock({ children }: { children?: React.ReactNode }) {
  const tableRef = useRef<HTMLTableElement>(null);

  function handleDownload() {
    if (tableRef.current) downloadCSV(tableRef.current);
  }

  return (
    // position:relative so the button stays put while the table scrolls horizontally.
    <div className="group relative my-3">
      <button
        onClick={handleDownload}
        aria-label="导出 CSV"
        className="opacity-0 group-hover:opacity-100 transition-opacity duration-150"
        style={{
          position: 'absolute',
          top: 6,
          right: 6,
          zIndex: 10,
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          padding: '2px 7px',
          fontFamily: 'var(--font-mono)',
          fontSize: 10.5,
          lineHeight: '1.4',
          color: 'var(--ink-2)',
          background: 'var(--surface)',
          border: '1px solid var(--line-2)',
          borderRadius: 'var(--r-sm)',
          cursor: 'pointer',
          userSelect: 'none',
          whiteSpace: 'nowrap',
        }}
      >
        <DownloadIcon />
        <span>CSV</span>
      </button>
      <div className="overflow-x-auto">
        <table
          ref={tableRef}
          style={{
            borderCollapse: 'collapse',
            width: '100%',
            fontSize: '13.5px',
            lineHeight: '1.55',
          }}
        >
          {children}
        </table>
      </div>
    </div>
  );
}

function DownloadIcon() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </svg>
  );
}

// ── Shared markdown components (used by ASSISTANT messages) ──────────────────
export const markdownComponents: React.ComponentProps<typeof ReactMarkdown>['components'] = {
  // Custom <pre> wrapper: applies the box styling and attaches a hover copy button.
  pre: ({ children }) => <CodeBlockPre>{children}</CodeBlockPre>,
  code: ({ className, children, ...props }) => {
    // rehype-highlight adds `language-*` and `hljs` classes to block <code>.
    // Detect block by presence of a language- class or hljs class.
    const isBlock =
      className?.includes('language-') || className?.includes('hljs');
    if (isBlock) {
      // Pass className through so hljs token spans render with correct colors.
      // Do NOT hardcode text-ink-2 here — let the hljs theme control colors.
      return (
        <code
          className={`block px-4 py-3 font-mono text-[12.5px] leading-[1.6] ${className ?? ''}`}
        >
          {children}
        </code>
      );
    }
    // Inline code: keep the bordered-pill styling.
    return (
      <code
        {...props}
        className="px-1.5 py-0.5 rounded font-mono text-[12.5px] bg-paper-2 border border-line-2 text-ink"
      >
        {children}
      </code>
    );
  },
  p: ({ children }) => <p className="mb-3 last:mb-0">{children}</p>,
  h1: ({ children }) => (
    <h1 className="mt-4 mb-2 text-[18px] font-semibold tracking-[-0.02em]">{children}</h1>
  ),
  h2: ({ children }) => (
    <h2 className="mt-4 mb-2 text-[15px] font-semibold tracking-[-0.01em]">{children}</h2>
  ),
  h3: ({ children }) => (
    <h3 className="mt-3 mb-1.5 text-[13.5px] font-semibold">{children}</h3>
  ),
  ul: ({ children }) => <ul className="mb-3 pl-4 list-disc space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="mb-3 pl-4 list-decimal space-y-1">{children}</ol>,
  li: ({ children }) => <li className="text-[14px]">{children}</li>,
  blockquote: ({ children }) => (
    <blockquote className="pl-3 border-l-2 border-accent text-ink-2 italic my-2">
      {children}
    </blockquote>
  ),
  a: MarkdownLink,
  table: ({ children }) => <TableBlock>{children}</TableBlock>,
  thead: ({ children }) => (
    <thead style={{ background: 'var(--paper-2)' }}>{children}</thead>
  ),
  tbody: ({ children }) => <tbody>{children}</tbody>,
  tr: ({ children }) => (
    <tr style={{ borderBottom: '1px solid var(--line-2)' }}>{children}</tr>
  ),
  th: ({ children }) => (
    <th
      style={{
        padding: '6px 12px',
        textAlign: 'left',
        fontWeight: 600,
        border: '1px solid var(--line)',
        color: 'var(--ink)',
      }}
    >
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td
      style={{
        padding: '6px 12px',
        border: '1px solid var(--line-2)',
        color: 'var(--ink)',
      }}
    >
      {children}
    </td>
  ),
  hr: () => <hr style={{ border: 'none', borderTop: '1px solid var(--line)', margin: '12px 0' }} />,
  del: ({ children }) => (
    <del style={{ textDecoration: 'line-through', opacity: 0.6 }}>{children}</del>
  ),
};

// ── Tool target extraction ────────────────────────────────────────────────────

export function extractTarget(tc: ToolCallEntry): string {
  const name = (tc.name ?? '').toLowerCase();
  const input = tc.input ?? {};

  if (name === 'read' || name === 'write' || name === 'edit') {
    const fp = input['file_path'] ?? input['path'] ?? input['filename'];
    return fp ? String(fp) : '';
  }
  if (name === 'grep' || name === 'search') {
    const pat = input['pattern'] ?? input['regex'] ?? input['query'];
    return pat ? `"${String(pat)}"` : '';
  }
  if (name === 'bash' || name === 'run' || name === 'shell') {
    const cmd = input['command'] ?? input['cmd'];
    return cmd ? String(cmd) : '';
  }
  // Generic: first string value
  const first = Object.values(input).find((v) => typeof v === 'string');
  return first ? String(first) : '';
}

// ── Metric computation ────────────────────────────────────────────────────────

export function computeMetric(tc: ToolCallEntry): string {
  const name = (tc.name ?? '').toLowerCase();
  const result = tc.result;

  if (result === undefined) return '';

  const lines = result.split('\n').length;

  if (name === 'read') return `${lines} ln`;
  if (name === 'grep' || name === 'search') {
    // Count non-empty lines as hits
    const hits = result.split('\n').filter((l) => l.trim().length > 0).length;
    return `${hits} hits`;
  }
  if (name === 'edit') {
    // Try to count diff lines
    const added = result.split('\n').filter((l) => l.startsWith('+')).length;
    const removed = result.split('\n').filter((l) => l.startsWith('-')).length;
    if (added > 0 || removed > 0) return `+${added} −${removed}`;
    return `${lines} ln`;
  }
  if (name === 'write') {
    return `+${lines}`;
  }
  if (name === 'bash' || name === 'run' || name === 'shell') {
    return tc.state === 'success' ? '✓ done' : `${lines} ln`;
  }
  return `${lines} ln`;
}

// ── Diff line coloring (optional enhancement) ─────────────────────────────────

export function renderResultWithDiff(result: string) {
  return result.split('\n').map((line, i) => {
    let color: string | undefined;
    if (line.startsWith('+')) color = 'var(--accent)';
    else if (line.startsWith('-')) color = 'rgba(0,0,0,0.5)';
    else if (line.startsWith('//') || line.startsWith('#')) color = 'var(--dim)';

    return (
      <span key={i} style={color ? { color, display: 'block' } : { display: 'block' }}>
        {line || '​'}
      </span>
    );
  });
}

// ── Single tool row ───────────────────────────────────────────────────────────

export interface ToolRowProps {
  tc: ToolCallEntry;
  rowKey: string;
  expanded: boolean;
  onToggle: () => void;
}

export function ToolRow({ tc, rowKey: _rowKey, expanded, onToggle }: ToolRowProps) {
  const toolName = tc.name ?? 'tool';
  const target = extractTarget(tc);
  const metric = computeMetric(tc);
  const hasResult = tc.result !== undefined;
  const isWriteTool =
    toolName.toLowerCase() === 'edit' || toolName.toLowerCase() === 'write';

  return (
    <div>
      <div
        onClick={onToggle}
        className="flex items-center gap-2 cursor-pointer font-mono text-[12px]"
        style={{
          padding: '6px 10px',
          border: '1px solid var(--line)',
          borderRadius: 'var(--r-sm)',
          background: expanded ? 'var(--surface)' : 'var(--paper)',
        }}
      >
        <span className="text-dim text-[10.5px]">{expanded ? '▾' : '▸'}</span>
        <span
          style={{
            fontWeight: 600,
            color: isWriteTool ? 'var(--accent)' : 'var(--ink)',
          }}
        >
          {toolName}
        </span>
        {target && (
          <span className="text-ink-2 truncate flex-1">{target}</span>
        )}
        <div className="flex items-center gap-2 ml-auto shrink-0">
          {metric && !expanded && (
            <span
              className="font-mono"
              style={{ fontSize: 10.5, color: 'var(--dim)' }}
            >
              {metric}
            </span>
          )}
          {/* State badge */}
          {tc.state && (
            <span
              className="font-mono px-1.5 py-0.5 rounded"
              style={{
                fontSize: 10,
                background: tc.state === 'success' ? 'var(--surface)' : 'var(--paper-2)',
                color: tc.state === 'success' ? 'var(--accent)' : 'var(--ink-2)',
                border: '1px solid var(--line-2)',
              }}
            >
              {tc.state}
            </span>
          )}
          {/* Spinner for in-flight calls */}
          {!hasResult && (
            <span
              className="font-mono animate-pulse"
              style={{ fontSize: 10.5, color: 'var(--dim)' }}
            >
              …
            </span>
          )}
        </div>
      </div>

      {expanded && (
        <div
          style={{
            margin: '4px 0 0 18px',
            padding: '10px 12px',
            background: 'var(--paper-2)',
            border: '1px solid var(--line-2)',
            borderRadius: 'var(--r-sm)',
            fontFamily: 'var(--font-mono)',
            fontSize: 11.5,
            color: 'var(--ink-2)',
            lineHeight: 1.55,
            whiteSpace: 'pre-wrap',
            maxHeight: 300,
            overflowY: 'auto',
          }}
        >
          {tc.result !== undefined ? (
            renderResultWithDiff(tc.result)
          ) : (
            <pre style={{ margin: 0, fontFamily: 'inherit', fontSize: 'inherit' }}>
              {JSON.stringify(tc.input ?? {}, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

// ── Thinking block ────────────────────────────────────────────────────────────

export interface ThinkingBlockProps {
  content: string;
  friendly?: boolean;
}

export function ThinkingBlock({ content, friendly }: ThinkingBlockProps) {
  // Default: always collapsed once thinking finishes (both modes); user expands manually.
  const [open, setOpen] = useState(false);

  // Split content into bullet points (by newline or paragraph)
  const lines = content
    .split(/\n+/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  return (
    <div>
      <div
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 cursor-pointer"
        style={{
          padding: '6px 10px',
          border: '1px dashed var(--line)',
          borderRadius: 'var(--r-sm)',
          background: open ? 'var(--surface)' : 'transparent',
          color: 'var(--ink-2)',
        }}
      >
        <span className="font-mono text-[10.5px] text-dim">{open ? '▾' : '▸'}</span>
        <span className="cw-label">{friendly ? 'thinking' : 'Thought'}</span>
      </div>
      {open && (
        <div
          style={{
            borderLeft: '2px solid var(--accent-line)',
            margin: '6px 0 0 6px',
            padding: '4px 0 4px 14px',
            display: 'flex',
            flexDirection: 'column',
            gap: 6,
          }}
        >
          {lines.map((line, i) => (
            <div key={i} className="flex gap-2 items-start">
              <span
                className="font-mono text-dim"
                style={{ fontSize: 11, marginTop: 2, minWidth: 16 }}
              >
                {i + 1}.
              </span>
              <span style={{ fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.5 }}>
                {line}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
