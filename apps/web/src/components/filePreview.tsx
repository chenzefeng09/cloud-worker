import { useEffect, useMemo, useState } from 'react';
import hljs from 'highlight.js';
import { api, ApiError } from '../api/client';
import { useCopied, CopyIcon } from './CopyButton';

// ── Kind / glyph helpers ─────────────────────────────────────────────────────

const KIND_GLYPH: Record<string, string> = {
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

export function mimeToKind(mime?: string): string {
  if (!mime) return 'md';
  if (mime.includes('pdf')) return 'pdf';
  if (mime.includes('markdown') || mime.includes('text/plain')) return 'md';
  if (mime.includes('json')) return 'json';
  if (mime.includes('yaml')) return 'yaml';
  if (mime.includes('csv')) return 'csv';
  if (mime.includes('word') || mime.includes('docx')) return 'docx';
  if (mime.includes('spreadsheet') || mime.includes('xlsx')) return 'xlsx';
  if (mime.includes('presentation') || mime.includes('pptx')) return 'pptx';
  if (mime.includes('image/png')) return 'png';
  if (mime.includes('image/jpeg')) return 'jpg';
  if (mime.includes('text/')) return 'txt';
  return 'md';
}

export function kindGlyph(mime?: string): string {
  return KIND_GLYPH[mimeToKind(mime)] ?? '·';
}

// ── Text-like MIME detection ──────────────────────────────────────────────────

function isTextLikeMime(mime?: string): boolean {
  if (!mime) return false;
  return (
    mime.startsWith('text/') ||
    mime.includes('json') ||
    mime.includes('yaml') ||
    mime.includes('xml') ||
    mime.includes('markdown') ||
    mime.includes('csv')
  );
}

// ── Syntax-highlight language detection (by filename extension) ────────────────

const EXT_TO_LANG: Record<string, string> = {
  py: 'python',
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  sql: 'sql',
  html: 'xml',
  xml: 'xml',
  css: 'css',
  scss: 'scss',
  md: 'markdown',
  markdown: 'markdown',
  java: 'java',
  go: 'go',
  rs: 'rust',
  c: 'c',
  h: 'cpp',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  rb: 'ruby',
  php: 'php',
  toml: 'ini',
  ini: 'ini',
  dockerfile: 'dockerfile',
};

/** Returns an hljs language for code files, or undefined for plain text (.txt, .csv…). */
function extToHljsLang(filename: string): string | undefined {
  const dot = filename.lastIndexOf('.');
  if (dot < 0) return undefined;
  const ext = filename.slice(dot + 1).toLowerCase();
  return EXT_TO_LANG[ext];
}

const MAX_HIGHLIGHT_BYTES = 200_000;

// ── Copy button (matches chat code-block style) ───────────────────────────────

function PreviewCopyButton({ text }: { text: string }) {
  const { copied, copy } = useCopied();
  return (
    <button
      onClick={() => copy(text)}
      aria-label="复制文件内容"
      title="复制文件内容"
      style={{
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
        whiteSpace: 'nowrap',
        userSelect: 'none',
      }}
    >
      {copied ? <span>已复制</span> : <CopyIcon />}
    </button>
  );
}

// ── Text/code preview with syntax highlighting + copy ─────────────────────────

interface TextPreviewProps {
  contentText: string | null;
  contentLoading: boolean;
  contentError: string | null;
  filename: string;
}

function TextPreview({ contentText, contentLoading, contentError, filename }: TextPreviewProps) {
  const lang = extToHljsLang(filename);

  // Highlighted HTML (memoised). Returns null when we should render plain text
  // (no language detected, file too large, or hljs errored).
  const highlightedHtml = useMemo(() => {
    if (contentText === null) return null;
    if (!lang) return null;
    if (contentText.length > MAX_HIGHLIGHT_BYTES) return null;
    try {
      return hljs.highlight(contentText, { language: lang }).value;
    } catch {
      return null; // unregistered language or hljs error → plain fallback
    }
  }, [contentText, lang]);

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="flex items-center justify-between px-4 py-2 border-b border-line-2">
        <span className="cw-label">文件内容</span>
        {contentText !== null && !contentLoading && (
          <PreviewCopyButton text={contentText} />
        )}
      </div>
      {contentLoading && (
        <div className="px-4 py-3 font-mono text-[11px] text-dim">加载预览…</div>
      )}
      {contentError && (
        <div className="px-4 py-3 font-mono text-[11px] text-accent">{contentError}</div>
      )}
      {contentText !== null && !contentLoading && (
        highlightedHtml !== null ? (
          <pre className="cw-scroll px-4 py-3 font-mono text-[11.5px] leading-[1.6] overflow-auto max-h-[480px]" style={{ whiteSpace: 'pre' }}>
            <code
              className={`hljs language-${lang}`}
              dangerouslySetInnerHTML={{ __html: highlightedHtml }}
            />
          </pre>
        ) : (
          <pre className="cw-scroll px-4 py-3 font-mono text-[11.5px] text-ink-2 whitespace-pre-wrap break-words max-h-[480px]">
            {contentText}
          </pre>
        )
      )}
    </div>
  );
}

// ── FilePreviewBody ───────────────────────────────────────────────────────────

export interface FilePreviewBodyProps {
  sessionId: string;
  fileId: string;
  filename: string;
  mime?: string;
}

export function FilePreviewBody({ sessionId, fileId, filename, mime }: FilePreviewBodyProps) {
  const contentUrl = `/api/bff/v1/sessions/${sessionId}/files/${fileId}/content`;
  const isImage = mime?.startsWith('image/') ?? false;
  const isText = isTextLikeMime(mime);

  const [contentText, setContentText] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);

  useEffect(() => {
    if (!isText) {
      setContentText(null);
      setContentError(null);
      return;
    }

    let cancelled = false;
    setContentLoading(true);
    setContentError(null);
    setContentText(null);

    api.attachments.getContent(sessionId, fileId)
      .then((res) => {
        if (cancelled) return;
        setContentText(res.text);
      })
      .catch((e) => {
        if (cancelled) return;
        setContentError(e instanceof ApiError ? e.message : '加载文件内容失败');
      })
      .finally(() => {
        if (!cancelled) setContentLoading(false);
      });

    return () => { cancelled = true; };
  }, [sessionId, fileId, isText]);

  // Image preview
  if (isImage) {
    return (
      <div className="border border-line rounded-md overflow-hidden bg-surface p-2">
        <img
          src={contentUrl}
          alt={filename}
          className="max-w-full max-h-[480px] object-contain"
          style={{ display: 'block' }}
        />
      </div>
    );
  }

  // Text preview
  if (isText) {
    return (
      <TextPreview
        contentText={contentText}
        contentLoading={contentLoading}
        contentError={contentError}
        filename={filename}
      />
    );
  }

  // Fallback: unsupported type
  return (
    <div className="border border-line rounded-md bg-surface px-4 py-4 flex flex-col gap-2">
      <div className="font-mono text-[12px] text-ink">{filename}</div>
      <div className="font-mono text-[11px] text-dim italic">— 该文件类型暂不支持预览，请下载查看 —</div>
      <a
        href={contentUrl}
        download={filename}
        className="cw-btn cw-btn-sm cw-btn-ghost self-start"
      >
        下载
      </a>
    </div>
  );
}
