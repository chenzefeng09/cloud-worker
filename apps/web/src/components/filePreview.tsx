import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';

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
      <div className="border border-line rounded-md overflow-hidden bg-surface">
        <div className="cw-label px-4 py-2 border-b border-line-2">文件内容</div>
        {contentLoading && (
          <div className="px-4 py-3 font-mono text-[11px] text-dim">加载预览…</div>
        )}
        {contentError && (
          <div className="px-4 py-3 font-mono text-[11px] text-accent">{contentError}</div>
        )}
        {contentText !== null && !contentLoading && (
          <pre className="cw-scroll px-4 py-3 font-mono text-[11.5px] text-ink-2 whitespace-pre-wrap break-words max-h-[480px]">
            {contentText}
          </pre>
        )}
      </div>
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
