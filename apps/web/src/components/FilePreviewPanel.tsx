import { FilePreviewBody, kindGlyph } from './filePreview';
import type { FileContentSource } from './filePreview';

export interface FilePreviewPanelFile {
  id: string;
  filename: string;
  mime?: string;
}

export interface FilePreviewPanelProps {
  /** Session id for the session-scoped content path. Optional when `getSource`
   *  is provided (the workspace path has no session). */
  sessionId?: string;
  file: FilePreviewPanelFile | null;
  onClose: () => void;
  /** Per-file content-source override. When provided, the body fetches from this
   *  source instead of the session-scoped path. Absent on the chat page, so the
   *  session-scoped behaviour is preserved exactly. */
  getSource?: (fileId: string) => FileContentSource;
}

export function FilePreviewPanel({ sessionId, file, onClose, getSource }: FilePreviewPanelProps) {
  if (!file) return null;

  const source = getSource?.(file.id);

  return (
    <div
      className="flex flex-col h-full bg-paper border-l border-line"
      style={{ width: '100%', minWidth: 0, overflow: 'hidden' }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line shrink-0">
        <button
          onClick={onClose}
          aria-label="返回"
          title="返回"
          className="h-8 inline-flex items-center gap-1.5 px-2.5 rounded-md border border-line bg-paper text-ink-2 hover:text-ink hover:bg-paper-2 hover:border-ink-2 transition-colors shrink-0 font-mono text-[12px]"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 2 L3 6 L7 10 M3 6 L11 6" />
          </svg>
        </button>
        <span className="font-mono text-[11px] text-dim w-4 text-center">
          {kindGlyph(file.mime)}
        </span>
        <span
          className="font-mono text-[12px] font-semibold text-ink flex-1 truncate"
          title={file.filename}
        >
          {file.filename}
        </span>
      </div>

      {/* Body */}
      <div className="cw-scroll flex-1 px-3 py-3">
        <FilePreviewBody
          sessionId={sessionId}
          source={source}
          fileId={file.id}
          filename={file.filename}
          mime={file.mime}
        />
      </div>
    </div>
  );
}
