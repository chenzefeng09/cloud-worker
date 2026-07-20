import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api/client';
import { LevelBadge } from '../lib/sensitivity';
import { FilePreviewPanel } from './FilePreviewPanel';
import type { FileContentSource } from './filePreview';
import type {
  DocumentStatus,
  KnowledgeDocument,
  KnowledgeFolder,
} from '../api/types';

// Read-only knowledge-source browser shown on the LibraryPage "知识源" tab.
//
// Regular (non-admin) users get folder navigation + a document list only - no
// upload, no mutations. Admins manage the KB over on /admin/knowledge; this
// view is intentionally a read-only mirror so the resource library stays a
// safe browse surface for everyone. The component reports its total document
// count up via `onCount` so the stat tile / tab badge stay in sync.

const DOC_STATUS_ZH: Record<DocumentStatus, string> = {
  PENDING: '待处理',
  INGESTING: '摄入中',
  READY: '就绪',
  FAILED: '失败',
};

/** Ancestor chain root -> currentFolderId (cycle-safe). */
function buildBreadcrumb(
  folders: KnowledgeFolder[],
  currentFolderId: string | null,
): KnowledgeFolder[] {
  const path: KnowledgeFolder[] = [];
  const guard = new Set<string>();
  let curId: string | null = currentFolderId;
  while (curId) {
    if (guard.has(curId)) break;
    guard.add(curId);
    const node = folders.find((f) => f.id === curId);
    if (!node) break;
    path.unshift(node);
    curId = node.parent_id;
  }
  return path;
}

export function KnowledgeSourceTab({
  onCount,
}: {
  onCount?: (n: number) => void;
}) {
  const [folders, setFolders] = useState<KnowledgeFolder[]>([]);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null);
  const [previewDoc, setPreviewDoc] = useState<{ id: string; filename: string; mime?: string } | null>(null);

  const getPreviewSource = (fileId: string): FileContentSource => ({
    url: api.library.documentContentUrl(fileId),
    getContent: () => api.library.getDocumentContent(fileId),
  });

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([api.library.listFolders(), api.library.listDocuments()])
      .then(([fRes, dRes]) => {
        if (cancelled) return;
        setFolders(fRes.items);
        setDocs(dRes.items);
        onCount?.(dRes.items.length);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : '加载知识源失败');
        onCount?.(0);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // onCount is a stable setState from the parent; omitting it from deps is
    // intentional so we don't refetch every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reset into the root when the tab is first mounted only; subsequent folder
  // clicks manage currentFolderId directly.
  const breadcrumb = useMemo(
    () => buildBreadcrumb(folders, currentFolderId),
    [folders, currentFolderId],
  );

  const subfolders = useMemo(() => {
    return folders
      .filter((f) =>
        currentFolderId ? f.parent_id === currentFolderId : f.parent_id === null,
      )
      .sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  }, [folders, currentFolderId]);

  const currentDocs = useMemo(() => {
    return docs
      .filter((d) =>
        currentFolderId ? d.folder_id === currentFolderId : !d.folder_id,
      )
      .sort((a, b) => a.title.localeCompare(b.title, 'zh'));
  }, [docs, currentFolderId]);

  if (loading) {
    return <div className="font-mono text-[11px] text-dim">加载中…</div>;
  }
  if (error) {
    return (
      <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
        {error}
      </div>
    );
  }

  const isEmpty = folders.length === 0 && docs.length === 0;

  return (
    <div className="flex flex-col gap-3">
      {/* Breadcrumb */}
      <div className="flex items-center gap-1 font-mono text-[11px] text-dim flex-wrap">
        <button
          type="button"
          className={`hover:text-ink transition-colors ${
            currentFolderId === null ? 'text-ink' : ''
          }`}
          onClick={() => setCurrentFolderId(null)}
        >
          根目录
        </button>
        {breadcrumb.map((f) => (
          <span key={f.id} className="flex items-center gap-1">
            <span className="text-dim">/</span>
            <button
              type="button"
              className={`hover:text-ink transition-colors ${
                currentFolderId === f.id ? 'text-ink' : ''
              }`}
              onClick={() => setCurrentFolderId(f.id)}
            >
              {f.name}
            </button>
          </span>
        ))}
      </div>

      {/* Subfolders */}
      {subfolders.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {subfolders.map((f) => (
            <button
              key={f.id}
              type="button"
              onClick={() => setCurrentFolderId(f.id)}
              className="flex items-center gap-2 px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all text-left"
            >
              <span className="text-[14px] leading-none">📁</span>
              <span className="text-[13.5px] font-medium flex-1 min-w-0 truncate">
                {f.name}
              </span>
              <LevelBadge level={f.default_sensitivity} small />
            </button>
          ))}
        </div>
      )}

      {/* Documents in current folder */}
      {currentDocs.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {currentDocs.map((d) => (
            <button
              key={d.id}
              type="button"
              onClick={d.source_uri ? () => setPreviewDoc({ id: d.id, filename: d.title, mime: d.mime ?? undefined }) : undefined}
              className={`grid grid-cols-[minmax(0,1fr)_auto_80px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md transition-colors text-left ${
                d.source_uri ? 'hover:border-ink-2 cursor-pointer' : ''
              }`}
            >
              <div className="min-w-0">
                <div className="text-[13.5px] font-medium truncate">{d.title}</div>
                <div className="font-mono text-[11px] text-dim truncate">
                  {d.mime || '-'}
                </div>
              </div>
              <LevelBadge level={d.sensitivity} small />
              <span className="font-mono text-[11px] text-ink-2 text-right">
                {DOC_STATUS_ZH[d.status] ?? d.status}
              </span>
            </button>
          ))}
        </div>
      )}

      {/* Empty states */}
      {subfolders.length === 0 && currentDocs.length === 0 && !isEmpty && (
        <div className="font-mono italic text-dim text-[12px]">
          该文件夹暂无文档
        </div>
      )}
      {isEmpty && (
        <div className="font-mono italic text-dim text-[12px]">
          暂无知识源。管理员可在「管理后台 → 知识库」中上传文档。
        </div>
      )}
      {previewDoc && (
        <div className="fixed top-0 right-0 bottom-0 z-50 w-[480px] border-l border-line bg-paper shadow-[-4px_0_16px_-4px_rgba(15,17,20,0.12)]">
          <FilePreviewPanel
            getSource={getPreviewSource}
            file={previewDoc}
            onClose={() => setPreviewDoc(null)}
          />
        </div>
      )}
    </div>
  );
}
