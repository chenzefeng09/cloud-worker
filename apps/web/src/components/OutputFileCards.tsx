import type { Attachment } from '../api/types';
import { kindGlyph, mimeToKind } from './filePreview';

// ── helpers ───────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Human-readable file kind label, e.g. "Markdown", "JSON", "Image". */
const KIND_LABEL: Record<string, string> = {
  md: 'Document',
  txt: 'Text',
  json: 'JSON',
  yaml: 'YAML',
  csv: 'Spreadsheet',
  pdf: 'PDF',
  xlsx: 'Spreadsheet',
  docx: 'Document',
  pptx: 'Slides',
  png: 'Image',
  jpg: 'Image',
  env: 'Config',
  ics: 'Calendar',
};

function fileKindLabel(mime?: string): string {
  const kind = mimeToKind(mime);
  return KIND_LABEL[kind] ?? kind.toUpperCase();
}

function fileExt(filename: string): string {
  const dot = filename.lastIndexOf('.');
  if (dot < 0) return '';
  return filename.slice(dot + 1).toUpperCase();
}

// ── Single output file card ───────────────────────────────────────────────────

interface OutputFileCardProps {
  file: Attachment;
  sessionId: string;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
}

function OutputFileCard({ file, sessionId, onPreviewFile }: OutputFileCardProps) {
  const contentUrl = `/api/bff/v1/sessions/${sessionId}/files/${file.id}/content`;
  const glyph = kindGlyph(file.mime);
  const kindLabel = fileKindLabel(file.mime);
  const ext = fileExt(file.filename);
  const subline = ext ? `${kindLabel} · ${ext}` : kindLabel;
  const sizeStr = formatBytes(file.size_bytes);

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onPreviewFile?.({ id: file.id, filename: file.filename, mime: file.mime })}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onPreviewFile?.({ id: file.id, filename: file.filename, mime: file.mime });
        }
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '9px 12px',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-md)',
        background: 'var(--paper)',
        cursor: 'pointer',
        transition: 'border-color 0.12s, background 0.12s',
        minWidth: 0,
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--line-2)';
        (e.currentTarget as HTMLDivElement).style.background = 'var(--paper-2)';
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--line)';
        (e.currentTarget as HTMLDivElement).style.background = 'var(--paper)';
      }}
    >
      {/* File type glyph */}
      <div
        style={{
          width: 32,
          height: 32,
          border: '1px solid var(--line-2)',
          borderRadius: 'var(--r-sm)',
          background: 'var(--surface)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          color: 'var(--ink-2)',
          flexShrink: 0,
        }}
      >
        {glyph}
      </div>

      {/* Filename + subline */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            letterSpacing: '-0.01em',
            color: 'var(--ink)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {file.filename}
        </div>
        <div
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10.5,
            color: 'var(--dim)',
            marginTop: 1,
          }}
        >
          {subline} · {sizeStr}
        </div>
      </div>

      {/* Download button */}
      <a
        href={contentUrl}
        download={file.filename}
        title={`Download ${file.filename}`}
        onClick={(e) => e.stopPropagation()}
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: 28,
          height: 28,
          borderRadius: 'var(--r-sm)',
          border: '1px solid var(--line-2)',
          background: 'var(--surface)',
          color: 'var(--ink-2)',
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          textDecoration: 'none',
          flexShrink: 0,
          cursor: 'pointer',
          transition: 'color 0.12s, border-color 0.12s',
        }}
        onMouseEnter={(e) => {
          const el = e.currentTarget as HTMLAnchorElement;
          el.style.color = 'var(--ink)';
          el.style.borderColor = 'var(--line)';
        }}
        onMouseLeave={(e) => {
          const el = e.currentTarget as HTMLAnchorElement;
          el.style.color = 'var(--ink-2)';
          el.style.borderColor = 'var(--line-2)';
        }}
      >
        ↓
      </a>
    </div>
  );
}

// ── OutputFileCards list ──────────────────────────────────────────────────────

interface OutputFileCardsProps {
  files: Attachment[];
  sessionId: string;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
}

export function OutputFileCards({ files, sessionId, onPreviewFile }: OutputFileCardsProps) {
  if (!files.length) return null;

  return (
    <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 10,
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
          color: 'var(--dim)',
          marginBottom: 2,
        }}
      >
        Output files
      </div>
      {files.map((f) => (
        <OutputFileCard
          key={f.id}
          file={f}
          sessionId={sessionId}
          onPreviewFile={onPreviewFile}
        />
      ))}
    </div>
  );
}
