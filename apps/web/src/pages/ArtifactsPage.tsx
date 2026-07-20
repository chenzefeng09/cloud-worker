import { useCallback, useEffect, useMemo, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { api, ApiError } from '../api/client';
import type { Artifact, ArtifactKind } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';
import { FilePreviewPanel } from '../components/FilePreviewPanel';
import type { FilePreviewPanelFile } from '../components/FilePreviewPanel';
import { CanvasReadonlyView } from '../components/CanvasReadonlyView';
import { Dialog } from '../admin/components/Dialog';

type KindFilter = 'all' | 'canvas' | 'file';

const KIND_TABS: { key: KindFilter; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'canvas', label: '画布' },
  { key: 'file', label: '文件' },
];

const KIND_LABEL: Record<ArtifactKind, string> = {
  canvas: '画布',
  file: '文件',
};

/** Shorten a UUID / ID to first 8 chars for display. */
function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) + '…' : id;
}

export function ArtifactsPage() {
  const [items, setItems] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<KindFilter>('all');
  const [searchQuery, setSearchQuery] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<Artifact | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Preview state
  const [previewArtifact, setPreviewArtifact] = useState<Artifact | null>(null);
  const [previewCanvasId, setPreviewCanvasId] = useState<string | null>(null);

  const fetchItems = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.artifacts.list();
      setItems(res.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '加载产物列表失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchItems();
  }, [fetchItems]);

  const filtered = useMemo(() => {
    let list = items;
    if (kindFilter !== 'all') {
      list = list.filter((a) => a.kind === kindFilter);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      list = list.filter(
        (a) =>
          a.title.toLowerCase().includes(q) ||
          (a.filename && a.filename.toLowerCase().includes(q)),
      );
    }
    return list;
  }, [items, kindFilter, searchQuery]);

  const handleDelete = useCallback(
    async (a: Artifact) => {
      setDeleting(true);
      setDeleteError(null);
      try {
        if (a.kind === 'canvas') {
          await api.artifacts.deleteCanvas(a.id);
        } else {
          await api.artifacts.deleteFile(a.session_id, a.id);
        }
        await fetchItems();
        setDeleteTarget(null);
      } catch (e) {
        setDeleteError(e instanceof ApiError ? e.message : '删除失败，请稍后重试。');
      } finally {
        setDeleting(false);
      }
    },
    [fetchItems],
  );

  const openPreview = useCallback((a: Artifact) => {
    if (a.kind === 'canvas') {
      setPreviewCanvasId(a.id);
      setPreviewArtifact(null);
    } else {
      setPreviewArtifact(a);
      setPreviewCanvasId(null);
    }
  }, []);

  const closePreview = useCallback(() => {
    setPreviewArtifact(null);
    setPreviewCanvasId(null);
  }, []);

  // Build FilePreviewPanel file shape from an Artifact
  const previewFile: FilePreviewPanelFile | null = previewArtifact
    ? { id: previewArtifact.id, filename: previewArtifact.filename ?? previewArtifact.title, mime: previewArtifact.mime }
    : null;

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页', to: '/home' }, { label: '产物' }]}
      />

      <div className="relative flex-1 min-h-0">
        {/* 列表层 */}
        <div className="cw-scroll absolute inset-0 overflow-auto px-8 py-5">
        <div className="w-full max-w-[960px] min-w-0 flex flex-col gap-4">

          {/* kind toggle */}
          <div className="flex items-center gap-0.5 border-b border-line">
            {KIND_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setKindFilter(t.key)}
                className={`px-3 py-1.5 font-mono text-[11px] rounded-t-md border-b-2 -mb-px transition-colors ${
                  kindFilter === t.key
                    ? 'border-accent text-accent bg-paper'
                    : 'border-transparent text-ink-2 hover:text-ink hover:bg-paper-2'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>

          {/* search */}
          <input
            className="border border-line bg-paper rounded-sm px-2.5 py-1.5 text-[12.5px] font-mono focus:outline-none focus:border-accent transition-colors"
            placeholder="搜索产物…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
          />

          {/* error */}
          {error && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {error}
            </div>
          )}

          {/* loading */}
          {loading && (
            <div className="font-mono text-[11px] text-dim">加载中…</div>
          )}

          {/* list */}
          {!loading && (
            filtered.length === 0 ? (
              <div className="font-mono italic text-dim text-[12px]">暂无产物</div>
            ) : (
              <div className="flex flex-col gap-1.5">
                {filtered.map((a) => (
                  <div
                    key={`${a.kind}-${a.id}`}
                    onClick={() => openPreview(a)}
                    className="grid grid-cols-[minmax(0,1fr)_80px_60px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all cursor-pointer"
                  >
                    {/* title + meta */}
                    <div className="min-w-0">
                      <div className="text-[13.5px] font-medium truncate">{a.title}</div>
                      <div className="font-mono text-[11px] text-dim truncate">
                        {a.kind === 'file' ? (a.filename ?? a.mime ?? '') : a.canvas_kind ?? ''}
                      </div>
                    </div>

                    {/* kind chip */}
                    <Chip
                      accent={a.kind === 'canvas'}
                      dashed={a.kind === 'file'}
                    >
                      {KIND_LABEL[a.kind]}
                    </Chip>

                    {/* session id (short) */}
                    <span className="font-mono text-[11px] text-dim truncate text-center">
                      {shortId(a.session_id)}
                    </span>

                    {/* time + delete */}
                    <div className="flex items-center justify-end gap-1.5">
                      <span className="font-mono text-[11px] text-dim">
                        {formatRelativeTimeZh(a.updated_at ?? a.created_at)}
                      </span>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeleteError(null);
                          setDeleteTarget(a);
                        }}
                        className="font-mono text-[11px] text-dim hover:text-accent transition-colors px-0.5"
                        title="删除"
                      >
                        ✕
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )
          )}

        </div>
        </div>

        {/* File preview panel -- 覆盖列表 */}
        {previewArtifact && previewFile && (
          <div className="absolute inset-0">
            <FilePreviewPanel
              sessionId={previewArtifact.session_id}
              file={previewFile}
              onClose={closePreview}
            />
          </div>
        )}

        {/* Canvas preview panel -- 覆盖列表 */}
        {previewCanvasId && (
          <div className="absolute inset-0">
            <CanvasReadonlyView
              canvasId={previewCanvasId}
              onClose={closePreview}
            />
          </div>
        )}
      </div>

      {deleteTarget && (
        <Dialog
          title="删除产物"
          onClose={() => {
            if (deleting) return;
            setDeleteTarget(null);
            setDeleteError(null);
          }}
          dismissable={!deleting}
          className="w-full max-w-[420px]"
        >
          <div className="text-[13px] text-ink leading-relaxed">
            确定删除「{deleteTarget.title}」？此操作不可撤销。
          </div>
          {deleteError && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12px] text-accent">
              {deleteError}
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <button
              type="button"
              disabled={deleting}
              onClick={() => {
                setDeleteTarget(null);
                setDeleteError(null);
              }}
              className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-50"
            >
              取消
            </button>
            <button
              type="button"
              disabled={deleting}
              onClick={() => void handleDelete(deleteTarget)}
              className="cw-btn cw-btn-accent cw-btn-sm disabled:opacity-50"
            >
              {deleting ? '删除中…' : '删除'}
            </button>
          </div>
        </Dialog>
      )}
    </div>
  );
}
