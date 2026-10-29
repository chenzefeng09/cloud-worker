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
  /** 关闭按钮样式与位置。
   *  - 'back'（默认）：左上角左指向返回箭头，aria-label="返回"。用于全覆盖层/分栏场景（产物预览、Workspace/Agent 分栏）。
   *  - 'x'：右上角 ✕，aria-label="关闭"。用于右侧 Drawer 场景（知识库预览）。
   */
  closeVariant?: 'back' | 'x';
}

export function FilePreviewPanel({ sessionId, file, onClose, getSource, closeVariant = 'back' }: FilePreviewPanelProps) {
  if (!file) return null;

  const source = getSource?.(file.id);
  const isCloseX = closeVariant === 'x';
  const closeButton = (
    <button
      onClick={onClose}
      aria-label={isCloseX ? 'Close' : 'Back'}
      title={isCloseX ? 'Close' : 'Back'}
      className="h-8 inline-flex items-center gap-1.5 px-2.5 rounded-md border border-line bg-paper text-ink-2 hover:text-ink hover:bg-paper-2 hover:border-ink-2 transition-colors shrink-0 font-mono text-[12px]"
    >
      {isCloseX ? (
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M3 3 L9 9 M9 3 L3 9" />
        </svg>
      ) : (
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
          <path d="M7 2 L3 6 L7 10 M3 6 L11 6" />
        </svg>
      )}
    </button>
  );

  return (
    <div
      className="flex flex-col h-full bg-paper border-l border-line"
      style={{ width: '100%', minWidth: 0, overflow: 'hidden' }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line shrink-0">
        {!isCloseX && closeButton}
        <span className="font-mono text-[11px] text-dim w-4 text-center">
          {kindGlyph(file.mime)}
        </span>
        <span
          className="font-mono text-[12px] font-semibold text-ink flex-1 truncate"
          title={file.filename}
        >
          {file.filename}
        </span>
        {isCloseX && closeButton}
      </div>

      {/* Body */}
      {/* min-h-0 + flex lets the body card stretch to the panel bottom (leaving
          the py-3 gutter) instead of stopping at a fixed height. */}
      <div className="cw-scroll flex-1 min-h-0 px-3 py-3 flex flex-col">
        <FilePreviewBody
          fill
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
