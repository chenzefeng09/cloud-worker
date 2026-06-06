import { FilePreviewBody, kindGlyph } from './filePreview';

export interface FilePreviewPanelFile {
  id: string;
  filename: string;
  mime?: string;
}

export interface FilePreviewPanelProps {
  sessionId: string;
  file: FilePreviewPanelFile | null;
  onClose: () => void;
}

export function FilePreviewPanel({ sessionId, file, onClose }: FilePreviewPanelProps) {
  if (!file) return null;

  return (
    <div
      className="flex flex-col h-full bg-paper border-l border-line"
      style={{ width: '100%', minWidth: 0, overflow: 'hidden' }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line shrink-0">
        <span className="font-mono text-[11px] text-dim w-4 text-center">
          {kindGlyph(file.mime)}
        </span>
        <span
          className="font-mono text-[12px] font-semibold text-ink flex-1 truncate"
          title={file.filename}
        >
          {file.filename}
        </span>
        <button
          onClick={onClose}
          aria-label="关闭预览"
          className="cw-btn cw-btn-ghost cw-btn-sm font-mono text-[12px] text-dim leading-none px-1.5 py-0.5"
        >
          ✕
        </button>
      </div>

      {/* Body */}
      <div className="cw-scroll flex-1 px-3 py-3">
        <FilePreviewBody
          sessionId={sessionId}
          fileId={file.id}
          filename={file.filename}
          mime={file.mime}
        />
      </div>
    </div>
  );
}
