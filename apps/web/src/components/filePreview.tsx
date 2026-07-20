import { useEffect, useMemo, useState, lazy, Suspense } from 'react';
import hljs from 'highlight.js';
import { api, ApiError } from '../api/client';
import { useCopied, CopyIcon } from './CopyButton';

// 重依赖的预览器全部懒加载：react-pdf / xlsx / docx-preview 只有在用户真正
// 点开对应格式时才下载,绝不进入首屏主 bundle。FilePreviewBody 是基础文件页
// 与任务页面板共用的预览组件,在此接入即两处同时获得能力。
const PdfPreview = lazy(() => import('./previews/PdfPreview'));
const ExcelPreview = lazy(() => import('./previews/ExcelPreview'));
const DocxPreview = lazy(() => import('./previews/DocxPreview'));
const PptxPreview = lazy(() => import('./previews/PptxPreview'));

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
  html: '◳',
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
  if (mime.includes('html')) return 'html';
  if (mime.includes('text/')) return 'txt';
  return 'md';
}

export function kindGlyph(mime?: string): string {
  return KIND_GLYPH[mimeToKind(mime)] ?? '·';
}

// ── Text-like MIME detection ──────────────────────────────────────────────────

function isTextLikeMime(mime?: string): boolean {
  if (!mime) return false;
  // Office Open XML docs (docx/xlsx/pptx) are binary zip containers whose MIME
  // contains "...wordprocessingml..."/"...spreadsheetml..." — i.e. the 'xml'
  // substring. Exclude them up front so they aren't mistaken for text and dumped
  // as garbled ZIP bytes; they route to DocxPreview/ExcelPreview instead.
  if (mime.includes('officedocument') || mime.includes('msword') || mime.includes('ms-excel')) {
    return false;
  }
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

// Text-like extensions — the extension-level counterpart of isTextLikeMime.
// Needed because some entry points (e.g. a `#file:<id>` link in an assistant
// reply) carry only id+filename, no MIME; without an ext fallback those files
// wrongly land on the "unsupported type" card.
const TEXT_LIKE_EXTS = new Set(['txt', 'text', 'log', 'csv', 'tsv', ...Object.keys(EXT_TO_LANG)]);

const IMAGE_EXTS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'ico']);

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

/**
 * Headless highlighted-text body (no header bar). Shared by TextPreview and the
 * HtmlPreview "source" mode so the highlight/plain-text logic lives in one place.
 * `lang` overrides the filename-derived language (HTML source forces 'xml').
 */
function TextContent({
  contentText,
  contentLoading,
  contentError,
  filename,
  lang: langOverride,
}: TextPreviewProps & { lang?: string }) {
  const lang = langOverride ?? extToHljsLang(filename);

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

  if (contentLoading) {
    return <div className="px-4 py-3 font-mono text-[11px] text-dim">加载预览…</div>;
  }
  if (contentError) {
    return <div className="px-4 py-3 font-mono text-[11px] text-accent">{contentError}</div>;
  }
  if (contentText === null) return null;

  return highlightedHtml !== null ? (
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
  );
}

function TextPreview({ contentText, contentLoading, contentError, filename }: TextPreviewProps) {
  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="flex items-center justify-between px-4 py-2 border-b border-line-2">
        <span className="cw-label">文件内容</span>
        {contentText !== null && !contentLoading && (
          <PreviewCopyButton text={contentText} />
        )}
      </div>
      <TextContent
        contentText={contentText}
        contentLoading={contentLoading}
        contentError={contentError}
        filename={filename}
      />
    </div>
  );
}

// ── HTML preview: rendered webpage (default) + source toggle ───────────────────

function HtmlPreview({ contentText, contentLoading, contentError, filename }: TextPreviewProps) {
  const [mode, setMode] = useState<'render' | 'source'>('render');

  const segBtn = (active: boolean) =>
    `cw-btn cw-btn-ghost cw-btn-sm${active ? ' text-accent' : ''}`;

  return (
    <div className="border border-line rounded-md overflow-hidden bg-surface">
      <div className="flex items-center justify-between px-4 py-2 border-b border-line-2">
        <div className="flex items-center gap-1">
          <button
            type="button"
            className={segBtn(mode === 'render')}
            style={mode === 'render' ? { borderColor: 'var(--accent)' } : undefined}
            onClick={() => setMode('render')}
          >
            预览
          </button>
          <button
            type="button"
            className={segBtn(mode === 'source')}
            style={mode === 'source' ? { borderColor: 'var(--accent)' } : undefined}
            onClick={() => setMode('source')}
          >
            源码
          </button>
        </div>
        {mode === 'source' && contentText !== null && !contentLoading && (
          <PreviewCopyButton text={contentText} />
        )}
      </div>

      {contentLoading && (
        <div className="px-4 py-3 font-mono text-[11px] text-dim">加载预览…</div>
      )}
      {contentError && (
        <div className="px-4 py-3 font-mono text-[11px] text-accent">{contentError}</div>
      )}

      {!contentLoading && !contentError && contentText !== null && (
        mode === 'render' ? (
          <div className="p-3 flex flex-col gap-2">
            <div className="border border-line rounded-md overflow-hidden bg-surface">
              <iframe
                // sandbox WITH allow-scripts but WITHOUT allow-same-origin: with srcDoc
                // this puts the frame in a null (opaque) origin — agent-produced scripts
                // can run (e.g. Mermaid/chart rendering) but cannot read the parent app's
                // cookies/localStorage/same-origin data. Never add allow-same-origin here:
                // the combo would let untrusted scripts touch same-origin state → XSS.
                sandbox="allow-scripts"
                srcDoc={contentText ?? ''}
                title={filename}
                style={{ width: '100%', height: 480, border: 0, background: '#fff' }}
                className="min-h-[480px]"
              />
            </div>
            <div className="font-mono text-[10.5px] text-dim">
              脚本在隔离沙箱中运行（无法访问站点数据）。引用站点相对资源的内容可能无法加载，如需完整效果请下载查看。
            </div>
          </div>
        ) : (
          <TextContent
            contentText={contentText}
            contentLoading={contentLoading}
            contentError={contentError}
            filename={filename}
            lang="xml"
          />
        )
      )}
    </div>
  );
}

// ── Image preview + lightbox ──────────────────────────────────────────────────

// 全屏放大遮罩。点遮罩空白 / 点图 / ESC / 右上角 ✕ 均可关闭(点图也关闭符合
// zoom-out 语义,故整个遮罩 onClick 关闭、图片不 stopPropagation)。打开时锁滚动。
function ImageLightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKeyDown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      onClick={onClose}
      className="fixed inset-0 z-50 flex items-center justify-center"
      style={{ background: 'rgba(0,0,0,0.8)' }}
    >
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="fixed top-4 right-4 text-white text-2xl leading-none"
      >
        ✕
      </button>
      <img
        src={src}
        alt={alt}
        className="max-w-[92vw] max-h-[92vh] object-contain cursor-zoom-out"
        style={{ display: 'block' }}
      />
    </div>
  );
}

function ImagePreview({ src, alt }: { src: string; alt: string }) {
  const [zoomed, setZoomed] = useState(false);
  return (
    <>
      <div className="border border-line rounded-md overflow-hidden bg-surface p-2">
        <img
          src={src}
          alt={alt}
          title="点击放大"
          onClick={() => setZoomed(true)}
          className="max-w-full max-h-[480px] object-contain cursor-zoom-in"
          style={{ display: 'block' }}
        />
      </div>
      {zoomed && <ImageLightbox src={src} alt={alt} onClose={() => setZoomed(false)} />}
    </>
  );
}

// ── FilePreviewBody ───────────────────────────────────────────────────────────

/**
 * Optional content-source override. When provided (e.g. by the workspace file
 * list, which has no session), FilePreviewBody builds the content URL and
 * fetches bytes from this source instead of the session-scoped BFF paths. When
 * absent, the body falls back to the session-scoped path exactly as before, so
 * the chat / session page (AgentPage / AppResultPage / FilesPage) is unchanged.
 */
export type FileContentSource = {
  /** Cookie-auth content URL (for PDF / image). */
  url: string;
  /** Fetcher for blob-based types (docx / excel / text / html). */
  getContent: () => Promise<{ text: string | null; blob: Blob; mime: string }>;
};

export interface FilePreviewBodyProps {
  /** Session id for the session-scoped content path. Optional when `source` is
   *  provided (the workspace path has no session). */
  sessionId?: string;
  fileId: string;
  filename: string;
  mime?: string;
  /** Override the session-scoped content URL + blob fetcher. */
  source?: FileContentSource;
}

export function FilePreviewBody({ sessionId, fileId, filename, mime, source }: FilePreviewBodyProps) {
  const contentUrl = source?.url ?? (sessionId ? `/api/bff/v1/sessions/${sessionId}/files/${fileId}/content` : '');
  // Blob fetcher: prefer the override (workspace path); otherwise fall back to
  // the session-scoped attachments endpoint. The non-null assertion is safe - the
  // fallback is only reached on the chat path, where sessionId is always set.
  const getContent = source?.getContent ?? (() => api.attachments.getContent(sessionId!, fileId));
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  const isImage = (mime?.startsWith('image/') ?? false) || IMAGE_EXTS.has(ext);
  const isPdf = mime === 'application/pdf' || ext === 'pdf';
  // Office 文件的 mime 常不准(有时是 octet-stream),用扩展名兜底。
  const isExcel =
    ['xlsx', 'xls'].includes(ext) ||
    (mime?.includes('spreadsheet') ?? false) ||
    (mime?.includes('excel') ?? false);
  const isWord =
    ['docx', 'doc'].includes(ext) ||
    (mime?.includes('officedocument.wordprocessing') ?? false) ||
    (mime?.includes('msword') ?? false);
  const isPptx =
    ['pptx', 'ppt'].includes(ext) ||
    (mime?.includes('officedocument.presentationml') ?? false) ||
    (mime?.includes('ms-powerpoint') ?? false);
  const needsBlob = isExcel || isWord || isPptx;
  // HTML renders as a webpage (iframe) by default, with a source-code toggle —
  // so it must NOT fall into TextPreview. Detect it before the text gate.
  const isHtml = mime === 'text/html' || ext === 'html' || ext === 'htm';
  // Office docs take precedence: never treat them as text even if MIME/ext is
  // ambiguous, otherwise the binary zip renders as garbled bytes.
  const isText = !isWord && !isExcel && !isPptx && !isHtml && (isTextLikeMime(mime) || TEXT_LIKE_EXTS.has(ext));

  const [contentText, setContentText] = useState<string | null>(null);
  const [contentBlob, setContentBlob] = useState<Blob | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [contentError, setContentError] = useState<string | null>(null);

  useEffect(() => {
    // 文本/HTML 走 text,Excel/Word 走 blob;PDF 用 URL、图片用 <img>,都无需 fetch。
    if (!isText && !isHtml && !needsBlob) {
      setContentText(null);
      setContentBlob(null);
      setContentError(null);
      return;
    }

    let cancelled = false;
    setContentLoading(true);
    setContentError(null);
    setContentText(null);
    setContentBlob(null);

    getContent()
      .then((res) => {
        if (cancelled) return;
        if (isText || isHtml) setContentText(res.text);
        if (needsBlob) setContentBlob(res.blob);
      })
      .catch((e) => {
        if (cancelled) return;
        setContentError(e instanceof ApiError ? e.message : '加载文件内容失败');
      })
      .finally(() => {
        if (!cancelled) setContentLoading(false);
      });

    return () => {
      cancelled = true;
      // Drop large content refs on unmount / file switch so the blob and
      // decoded text become eligible for GC when the preview panel closes.
      setContentBlob(null);
      setContentText(null);
    };
    // `getContent` is intentionally excluded: it is recreated every render
    // (the workspace page rebuilds `source` each render, and drag-resize changes
    // `previewWidth` every mousemove). Adding it would cancel+refetch the file on
    // every mousemove. It only closes over `sessionId`/`fileId` (both in deps)
    // and `source` (a workspace switch unmounts this body before it can go stale).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, fileId, isText, isHtml, needsBlob]);

  // Image preview
  if (isImage) {
    return <ImagePreview src={contentUrl} alt={filename} />;
  }

  // HTML preview — rendered webpage (iframe, default) + source toggle
  if (isHtml) {
    return (
      <HtmlPreview
        contentText={contentText}
        contentLoading={contentLoading}
        contentError={contentError}
        filename={filename}
      />
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

  // PDF / Excel / Word / PPT — 懒加载预览器
  if (isPdf || isExcel || isWord || isPptx) {
    return (
      <Suspense fallback={<div className="font-mono text-[11px] text-dim">加载预览组件…</div>}>
        {contentError ? (
          <div className="font-mono text-[11px] text-accent">{contentError}</div>
        ) : isPdf ? (
          <PdfPreview url={contentUrl} />
        ) : contentBlob ? (
          isExcel ? <ExcelPreview blob={contentBlob} /> : isPptx ? <PptxPreview blob={contentBlob} /> : <DocxPreview blob={contentBlob} />
        ) : (
          <div className="font-mono text-[11px] text-dim">
            {isExcel ? '加载表格…' : isPptx ? '加载演示文稿…' : '加载文档…'}
          </div>
        )}
      </Suspense>
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
