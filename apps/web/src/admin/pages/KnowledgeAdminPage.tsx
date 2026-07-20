import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useConfirm, usePrompt, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import { FilePreviewPanel } from '../../components/FilePreviewPanel';
import type { FileContentSource } from '../../components/filePreview';
import {
  LevelBadge,
  SENSITIVITY_LABELS,
  SENSITIVITY_ORDER,
} from '../../lib/sensitivity';
import type {
  DlpSensitivity,
  DocumentStatus,
  IngestResult,
  KnowledgeDocument,
  KnowledgeFolder,
} from '../../api/types';

// ─── Constants ────────────────────────────────────────────────────────────────

const STATUS_COLOR: Record<DocumentStatus, string> = {
  PENDING: 'text-dim',
  INGESTING: 'text-warn',
  READY: 'text-ok',
  FAILED: 'text-accent',
};

const DOC_STATUS_ZH: Record<DocumentStatus, string> = {
  PENDING: '待处理',
  INGESTING: '摄入中',
  READY: '就绪',
  FAILED: '失败',
};

const SENSITIVITY_OPTIONS = SENSITIVITY_ORDER.map((l) => ({
  value: l,
  label: `${l} ${SENSITIVITY_LABELS[l]}`,
}));

// ─── Tree helpers ─────────────────────────────────────────────────────────────

interface FolderNode extends KnowledgeFolder {
  children: FolderNode[];
}

function buildTree(folders: KnowledgeFolder[]): FolderNode[] {
  const byId = new Map<string, FolderNode>();
  folders.forEach((f) => byId.set(f.id, { ...f, children: [] }));
  const roots: FolderNode[] = [];
  byId.forEach((node) => {
    if (node.parent_id && byId.has(node.parent_id)) {
      byId.get(node.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}

function flattenTree(
  nodes: FolderNode[],
  depth = 0,
): { node: FolderNode; depth: number }[] {
  const out: { node: FolderNode; depth: number }[] = [];
  const sorted = [...nodes].sort((a, b) => a.name.localeCompare(b.name, 'zh'));
  for (const n of sorted) {
    out.push({ node: n, depth });
    out.push(...flattenTree(n.children, depth + 1));
  }
  return out;
}

/** Ancestor chain root -> folderId (cycle-safe). */
function buildBreadcrumb(
  folders: KnowledgeFolder[],
  folderId: string | null,
): KnowledgeFolder[] {
  const path: KnowledgeFolder[] = [];
  const guard = new Set<string>();
  let curId: string | null = folderId;
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

/** "root / A / B" style path label for the folder picker. */
function folderPathLabel(folders: KnowledgeFolder[], id: string): string {
  return buildBreadcrumb(folders, id)
    .map((f) => f.name)
    .join(' / ');
}

/** All descendants of `excludeId` (inclusive) - targets a move-folder picker
 *  must not offer (can't move a folder into itself or its own subtree). */
function collectDescendants(folders: KnowledgeFolder[], excludeId: string): Set<string> {
  const out = new Set<string>([excludeId]);
  let added = true;
  while (added) {
    added = false;
    for (const f of folders) {
      if (f.parent_id && out.has(f.parent_id) && !out.has(f.id)) {
        out.add(f.id);
        added = true;
      }
    }
  }
  return out;
}

// ─── Audit mode (cross-tenant, read-only via admin.* with tenant_id) ──────────

function DocDetailPanel({
  doc,
  apiTenantId,
  onPreview,
  onClose,
}: {
  doc: KnowledgeDocument;
  apiTenantId: string | undefined;
  onPreview: (d: { id: string; filename: string; mime?: string }) => void;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<KnowledgeDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.admin.knowledge.documents
      .get(doc.id, apiTenantId)
      .then((data) => { if (!cancelled) setDetail(data); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [doc.id, apiTenantId]);

  const d = detail ?? doc;

  return (
    <Dialog title="文档详情" onClose={onClose} className="w-full max-w-lg">
      <div className="flex items-center gap-2 mb-3">
        {d.source_uri && (
          <button
            type="button"
            onClick={() => onPreview({ id: d.id, filename: d.title, mime: d.mime ?? undefined })}
            className="cw-btn cw-btn-ghost cw-btn-sm text-[11px] font-mono"
          >
            预览
          </button>
        )}
      </div>
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      <div className="grid grid-cols-[120px_1fr] gap-y-2 gap-x-3 text-[12px]">
        <span className="font-mono text-dim">ID</span>
        <span className="font-mono text-ink-2 break-all">{d.id}</span>
        <span className="font-mono text-dim">文件名</span>
        <span className="text-ink">{d.title}</span>
        <span className="font-mono text-dim">MIME</span>
        <span className="font-mono text-ink-2">{d.mime}</span>
        <span className="font-mono text-dim">状态</span>
        <span className={`font-mono ${STATUS_COLOR[d.status]}`}>{d.status}</span>
        <span className="font-mono text-dim">Chunk 数</span>
        <span className="font-mono text-ink-2">{d.chunk_count}</span>
        <span className="font-mono text-dim">来源</span>
        <span className="font-mono text-ink-2 break-all">{d.source_uri || '-'}</span>
        <span className="font-mono text-dim">上传时间</span>
        <span className="font-mono text-ink-2">{formatDateTimeAuto(new Date(d.uploaded_at))}</span>
      </div>
    </Dialog>
  );
}

function AuditDocRow({
  doc,
  apiTenantId,
  onSelect,
  onPreview,
  selected,
  onDeleted,
}: {
  doc: KnowledgeDocument;
  apiTenantId: string | undefined;
  onSelect: (d: KnowledgeDocument) => void;
  onPreview: (d: { id: string; filename: string; mime?: string }) => void;
  selected: boolean;
  onDeleted: () => void;
}) {
  const [reingesting, setReingesting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const confirm = useConfirm();
  const toast = useToast();

  const reingest = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!(await confirm({ title: '重新摄取', message: `确认重新摄取文档「${doc.title}」？`, confirmText: '确认' }))) return;
    setReingesting(true);
    setMsg(null);
    try {
      const res: IngestResult = await api.admin.knowledge.documents.reingest(doc.id, apiTenantId);
      setMsg(`已触发：${res.status}，生成 ${res.chunks_created} 个 chunk`);
    } catch (err) {
      setMsg(err instanceof ApiError ? `失败：${err.message}` : '重新摄取失败');
    } finally {
      setReingesting(false);
    }
  };

  const remove = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (
      !(await confirm({
        title: '删除文档',
        message: `确定删除文档「${doc.title}」吗？此操作不可撤销。`,
        tone: 'danger',
        confirmText: '删除',
      }))
    )
      return;
    setDeleting(true);
    try {
      await api.admin.knowledge.documents.delete(doc.id, apiTenantId);
      toast.success('已删除文档');
      onDeleted();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
      setDeleting(false);
    }
  };

  return (
    <tr
      onClick={doc.source_uri ? () => onPreview({ id: doc.id, filename: doc.title, mime: doc.mime ?? undefined }) : undefined}
      className={`border-b border-line-2 hover:bg-paper-2 transition-colors group ${
        doc.source_uri ? 'cursor-pointer' : ''
      } ${selected ? 'bg-paper-2' : ''}`}
    >
      <td className="px-3 py-2 text-[13px] text-ink truncate max-w-[260px]">{doc.title}</td>
      <td className="px-3 py-2"><LevelBadge level={doc.sensitivity} small /></td>
      <td className={`px-3 py-2 font-mono text-[11px] ${STATUS_COLOR[doc.status]}`}>{doc.status}</td>
      <td className="px-3 py-2 font-mono text-[12px] text-ink-2">{doc.chunk_count}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-dim">
        {formatDateTimeAuto(new Date(doc.uploaded_at))}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          {msg && <span className="text-[11px] font-mono text-dim">{msg}</span>}
          <button
            onClick={(e) => { e.stopPropagation(); onSelect(doc); }}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity"
          >
            详情
          </button>
          <button
            onClick={reingest}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity disabled:opacity-50"
          >
            {reingesting ? '摄取中…' : '重新摄取'}
          </button>
          <button
            onClick={remove}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-red-600 hover:text-red-700 transition-opacity disabled:opacity-50"
          >
            {deleting ? '删除中…' : '删除'}
          </button>
        </div>
      </td>
    </tr>
  );
}

// ─── Manage mode dialogs ──────────────────────────────────────────────────────

function CreateFolderDialog({
  parentId,
  onClose,
  onCreated,
}: {
  parentId: string | null;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [sens, setSens] = useState<DlpSensitivity>('L1');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setErr('请填写文件夹名称');
      return;
    }
    setSubmitting(true);
    setErr(null);
    try {
      await api.library.createFolder({
        name: name.trim(),
        parent_id: parentId,
        default_sensitivity: sens,
      });
      onCreated();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : '创建失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="新建文件夹" onClose={onClose} className="w-full max-w-md">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">名称</span>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如 产品文档"
            autoFocus
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">默认密级（新上传文档未指定时采用）</span>
          <CustomSelect
            options={SENSITIVITY_OPTIONS}
            value={sens}
            onChange={(v) => setSens(v as DlpSensitivity)}
          />
        </label>
        {err && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? '创建中…' : '创建'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function UploadDialog({
  folderId,
  defaultSensitivity,
  onClose,
  onUploaded,
}: {
  folderId: string | null;
  defaultSensitivity: DlpSensitivity;
  onClose: () => void;
  onUploaded: () => void;
}) {
  const [title, setTitle] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [sens, setSens] = useState<DlpSensitivity>(defaultSensitivity);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) {
      setErr('请选择文件');
      return;
    }
    setSubmitting(true);
    setErr(null);
    try {
      const fd = new FormData();
      fd.append('title', title.trim() || file.name);
      fd.append('mime', file.type || 'application/octet-stream');
      fd.append('file', file, file.name);
      fd.append('sensitivity', sens);
      if (folderId) fd.append('folder_id', folderId);
      const doc = await api.library.uploadDocument(fd);
      // Trigger ingestion so the document gets chunked and becomes READY.
      try {
        await api.library.ingestDocument(doc.id);
      } catch {
        // Upload succeeded; ingestion can be retried via the row action. Don't
        // surface a hard error - the doc exists, just not chunked yet.
      }
      onUploaded();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : '上传失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="上传文档" onClose={onClose} className="w-full max-w-md">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">标题（可选，默认取文件名）</span>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="文档标题"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">文件</span>
          <input
            type="file"
            className="text-[12px] text-ink-2 file:mr-2 file:px-2 file:py-1 file:text-[11px] file:font-mono file:border file:border-line file:rounded-sm file:bg-paper file:cursor-pointer"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            autoFocus
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">密级</span>
          <CustomSelect
            options={SENSITIVITY_OPTIONS}
            value={sens}
            onChange={(v) => setSens(v as DlpSensitivity)}
          />
        </label>
        {err && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm bg-accent text-surface hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? '上传中…' : '上传'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function EditSensitivityDialog({
  doc,
  onClose,
  onDone,
}: {
  doc: KnowledgeDocument;
  onClose: () => void;
  onDone: () => void;
}) {
  const [sens, setSens] = useState<DlpSensitivity>(doc.sensitivity ?? 'L1');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      await api.library.updateDocument(doc.id, { sensitivity: sens });
      onDone();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : '修改失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="修改文档密级" onClose={onClose} className="w-full max-w-md">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="text-[12px] text-dim">文档：{doc.title}</div>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">密级</span>
          <CustomSelect
            options={SENSITIVITY_OPTIONS}
            value={sens}
            onChange={(v) => setSens(v as DlpSensitivity)}
          />
        </label>
        {err && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? '保存中…' : '保存'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function MoveDocDialog({
  doc,
  folders,
  currentFolderId,
  onClose,
  onDone,
}: {
  doc: KnowledgeDocument;
  folders: KnowledgeFolder[];
  currentFolderId: string | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [target, setTarget] = useState<string>(currentFolderId ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // All folders as move targets, plus root (''). A doc may move anywhere.
  const options = useMemo(() => {
    const opts = folders
      .map((f) => ({ value: f.id, label: folderPathLabel(folders, f.id) }))
      .sort((a, b) => a.label.localeCompare(b.label, 'zh'));
    return [{ value: '', label: '根目录' }, ...opts];
  }, [folders]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      await api.library.updateDocument(doc.id, {
        folder_id: target || null,
      });
      onDone();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : '移动失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="移动文档" onClose={onClose} className="w-full max-w-md">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="text-[12px] text-dim">文档：{doc.title}</div>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">目标文件夹</span>
          <CustomSelect options={options} value={target} onChange={setTarget} />
        </label>
        {err && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? '移动中…' : '移动'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function MoveFolderDialog({
  folder,
  folders,
  onClose,
  onDone,
}: {
  folder: KnowledgeFolder;
  folders: KnowledgeFolder[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [target, setTarget] = useState<string>(folder.parent_id ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Exclude the folder itself + its descendants (can't move into own subtree).
  const options = useMemo(() => {
    const blocked = collectDescendants(folders, folder.id);
    const opts = folders
      .filter((f) => !blocked.has(f.id))
      .map((f) => ({ value: f.id, label: folderPathLabel(folders, f.id) }))
      .sort((a, b) => a.label.localeCompare(b.label, 'zh'));
    return [{ value: '', label: '根目录' }, ...opts];
  }, [folders, folder.id]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    try {
      await api.library.updateFolder(folder.id, {
        parent_id: target || null,
      });
      onDone();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : '移动失败');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog title="移动文件夹" onClose={onClose} className="w-full max-w-md">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <div className="text-[12px] text-dim">文件夹：{folder.name}</div>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[11px] text-dim">目标父文件夹</span>
          <CustomSelect options={options} value={target} onChange={setTarget} />
        </label>
        {err && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
        <div className="flex justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={submitting}
            className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
          >
            {submitting ? '移动中…' : '移动'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

// ─── Manage mode: document row ────────────────────────────────────────────────

function ManageDocRow({
  doc,
  onEdited,
  onMoved,
  onReingest,
  onDeleted,
  onPreview,
}: {
  doc: KnowledgeDocument;
  onEdited: (d: KnowledgeDocument) => void;
  onMoved: (d: KnowledgeDocument) => void;
  onReingest: (d: KnowledgeDocument) => void;
  onDeleted: (d: KnowledgeDocument) => void;
  onPreview: (d: { id: string; filename: string; mime?: string }) => void;
}) {
  const [reingesting, setReingesting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const confirm = useConfirm();
  const toast = useToast();

  const reingest = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!(await confirm({ title: '重新摄取', message: `确认重新摄取文档「${doc.title}」？`, confirmText: '确认' }))) return;
    setReingesting(true);
    setMsg(null);
    try {
      const res: IngestResult = await api.library.ingestDocument(doc.id);
      setMsg(`已触发：${res.status}，生成 ${res.chunks_created} 个 chunk`);
      onReingest(doc);
    } catch (err) {
      setMsg(err instanceof ApiError ? `失败：${err.message}` : '重新摄取失败');
    } finally {
      setReingesting(false);
    }
  };

  const remove = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (
      !(await confirm({
        title: '删除文档',
        message: `确定删除文档「${doc.title}」吗？此操作不可撤销。`,
        tone: 'danger',
        confirmText: '删除',
      }))
    )
      return;
    setDeleting(true);
    try {
      await api.library.deleteDocument(doc.id);
      toast.success('已删除文档');
      onDeleted(doc);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '删除失败');
      setDeleting(false);
    }
  };

  const openPreview = () => onPreview({ id: doc.id, filename: doc.title, mime: doc.mime ?? undefined });

  return (
    <tr
      onClick={doc.source_uri ? openPreview : undefined}
      className={`border-b border-line-2 hover:bg-paper-2 transition-colors group ${doc.source_uri ? 'cursor-pointer' : ''}`}
    >
      <td className="px-3 py-2 text-[13px] text-ink truncate max-w-[260px]">{doc.title}</td>
      <td className="px-3 py-2"><LevelBadge level={doc.sensitivity} small /></td>
      <td className={`px-3 py-2 font-mono text-[11px] ${STATUS_COLOR[doc.status]}`}>
        {DOC_STATUS_ZH[doc.status] ?? doc.status}
      </td>
      <td className="px-3 py-2 font-mono text-[12px] text-ink-2">{doc.chunk_count}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
        {formatDateTimeAuto(new Date(doc.uploaded_at))}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          {msg && <span className="text-[11px] font-mono text-dim">{msg}</span>}
          <button
            onClick={(e) => { e.stopPropagation(); onEdited(doc); }}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity disabled:opacity-50"
          >
            改密级
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); onMoved(doc); }}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity disabled:opacity-50"
          >
            移动
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); reingest(); }}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity disabled:opacity-50"
          >
            {reingesting ? '摄取中…' : '重新摄取'}
          </button>
          <button
            onClick={(e) => { e.stopPropagation(); remove(); }}
            disabled={reingesting || deleting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-red-600 hover:text-red-700 transition-opacity disabled:opacity-50"
          >
            {deleting ? '删除中…' : '删除'}
          </button>
        </div>
      </td>
    </tr>
  );
}

// ─── Manage mode: folder tree (left pane) ─────────────────────────────────────

function FolderTree({
  folders,
  currentFolderId,
  onNavigate,
  onRename,
  onMove,
  onDelete,
}: {
  folders: KnowledgeFolder[];
  currentFolderId: string | null;
  onNavigate: (id: string | null) => void;
  onRename: (f: KnowledgeFolder) => void;
  onMove: (f: KnowledgeFolder) => void;
  onDelete: (f: KnowledgeFolder) => void;
}) {
  const tree = useMemo(() => buildTree(folders), [folders]);
  const flat = useMemo(() => flattenTree(tree), [tree]);

  return (
    <div className="flex flex-col gap-0.5">
      {/* Root entry */}
      <button
        type="button"
        onClick={() => onNavigate(null)}
        className={`flex items-center gap-1.5 px-2 py-1.5 rounded-sm text-left text-[12.5px] transition-colors ${
          currentFolderId === null
            ? 'bg-surface text-ink border border-line'
            : 'text-ink-2 hover:bg-paper-2 border border-transparent'
        }`}
      >
        <span className="text-[13px] leading-none">📁</span>
        <span className="flex-1 truncate">根目录</span>
      </button>
      {flat.map(({ node, depth }) => (
        <div
          key={node.id}
          className="group flex items-center gap-1 rounded-sm hover:bg-paper-2 border border-transparent"
          style={{ paddingLeft: depth * 14 + 8 }}
        >
          <button
            type="button"
            onClick={() => onNavigate(node.id)}
            className={`flex items-center gap-1.5 flex-1 min-w-0 px-2 py-1.5 text-left text-[12.5px] transition-colors ${
              currentFolderId === node.id ? 'text-ink font-medium' : 'text-ink-2'
            }`}
          >
            <span className="text-[13px] leading-none shrink-0">📁</span>
            <span className="truncate">{node.name}</span>
          </button>
          <LevelBadge level={node.default_sensitivity} small />
          <div className="flex items-center gap-0.5 pr-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <button
              type="button"
              title="重命名"
              onClick={() => onRename(node)}
              className="text-[11px] font-mono text-dim hover:text-ink px-1"
            >
              ✎
            </button>
            <button
              type="button"
              title="移动"
              onClick={() => onMove(node)}
              className="text-[11px] font-mono text-dim hover:text-ink px-1"
            >
              ↔
            </button>
            <button
              type="button"
              title="删除"
              onClick={() => onDelete(node)}
              className="text-[11px] font-mono text-red-600 hover:text-red-700 px-1"
            >
              ✕
            </button>
          </div>
        </div>
      ))}
      {flat.length === 0 && (
        <div className="px-2 py-1.5 font-mono text-[11px] text-dim italic">暂无文件夹</div>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function KnowledgeAdminPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const userProfile = useChatStore((s) => s.userProfile);
  const confirm = useConfirm();
  const prompt = usePrompt();
  const toast = useToast();

  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);
  const ownTenantId = userProfile?.tenant_id ?? null;
  const ownTenantName = tenants.find((t) => t.id === ownTenantId)?.name ?? ownTenantId ?? '当前租户';

  // Platform admins may pick any tenant; tenant admins are locked to their own.
  // The shared adminStore may hold a stale cross-tenant id from a prior
  // platform-admin session, so tenant admins always resolve to their own tenant.
  const isManageMode =
    !canSwitchTenant || !selectedTenantId || selectedTenantId === ownTenantId;
  // The tenant_id sent to the BFF for the audit (cross-tenant) path. Manage mode
  // operates via /library/* which is caller-tenant-scoped (no override sent).
  const apiTenantId = canSwitchTenant ? (selectedTenantId ?? undefined) : undefined;

  // ── Manage-mode state (own tenant, library.*) ───────────────────────────────
  const [folders, setFolders] = useState<KnowledgeFolder[]>([]);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [currentFolderId, setCurrentFolderId] = useState<string | null>(null);
  const [manageLoading, setManageLoading] = useState(false);
  const [manageError, setManageError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // ── Audit-mode state (cross-tenant, admin.*) ────────────────────────────────
  const [auditDocs, setAuditDocs] = useState<KnowledgeDocument[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [selectedAudit, setSelectedAudit] = useState<KnowledgeDocument | null>(null);
  const [auditReloadKey, setAuditReloadKey] = useState(0);

  // ── Modal state ─────────────────────────────────────────────────────────────
  const [createFolderOpen, setCreateFolderOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [editSensDoc, setEditSensDoc] = useState<KnowledgeDocument | null>(null);
  const [moveDoc, setMoveDoc] = useState<KnowledgeDocument | null>(null);
  const [moveFolder, setMoveFolder] = useState<KnowledgeFolder | null>(null);
  const [previewDoc, setPreviewDoc] = useState<{ id: string; filename: string; mime?: string } | null>(null);

  const getPreviewSource = useCallback(
    (fileId: string): FileContentSource => {
      if (isManageMode) {
        return {
          url: api.library.documentContentUrl(fileId),
          getContent: () => api.library.getDocumentContent(fileId),
        };
      }
      return {
        url: api.admin.knowledge.documents.contentUrl(fileId, apiTenantId),
        getContent: () => api.admin.knowledge.documents.getContent(fileId, apiTenantId),
      };
    },
    [isManageMode, apiTenantId],
  );

  // Bootstrap tenant list
  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  // Tenant admins must never carry a cross-tenant selection in the shared store.
  useEffect(() => {
    if (!canSwitchTenant && selectedTenantId !== null) {
      setSelectedTenantId(null);
    }
  }, [canSwitchTenant, selectedTenantId, setSelectedTenantId]);

  // Load manage-mode data when entering manage mode.
  useEffect(() => {
    if (!isManageMode) return;
    let cancelled = false;
    setManageLoading(true);
    setManageError(null);
    Promise.all([api.library.listFolders(), api.library.listDocuments()])
      .then(([fRes, dRes]) => {
        if (cancelled) return;
        setFolders(fRes.items);
        setDocs(dRes.items);
      })
      .catch((err) => {
        if (cancelled) return;
        setManageError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => {
        if (!cancelled) setManageLoading(false);
      });
    return () => { cancelled = true; };
    // reloadKey lets mutation handlers force a fresh fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isManageMode, reloadKey]);

  // If the current folder disappears (deleted/moved), fall back to root.
  useEffect(() => {
    if (currentFolderId && !folders.some((f) => f.id === currentFolderId)) {
      setCurrentFolderId(null);
    }
  }, [folders, currentFolderId]);

  // Load audit-mode data when entering audit mode.
  useEffect(() => {
    if (isManageMode) return;
    if (!selectedTenantId) return;
    let cancelled = false;
    setAuditLoading(true);
    setAuditError(null);
    setIsTodoEndpoint(false);
    setSelectedAudit(null);
    api.admin.knowledge.documents
      .list(apiTenantId)
      .then((data) => { if (!cancelled) setAuditDocs(data); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setIsTodoEndpoint(true);
        } else {
          setAuditError(err instanceof ApiError ? err.message : '加载失败');
        }
      })
      .finally(() => { if (!cancelled) setAuditLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isManageMode, selectedTenantId, apiTenantId, auditReloadKey]);

  // ── Derived manage-mode data ────────────────────────────────────────────────
  const breadcrumb = useMemo(
    () => buildBreadcrumb(folders, currentFolderId),
    [folders, currentFolderId],
  );
  const currentDocs = useMemo(() => {
    return docs
      .filter((d) =>
        currentFolderId ? d.folder_id === currentFolderId : !d.folder_id,
      )
      .sort((a, b) => a.title.localeCompare(b.title, 'zh'));
  }, [docs, currentFolderId]);
  const currentFolderDefaultSensitivity: DlpSensitivity = useMemo(() => {
    const f = folders.find((x) => x.id === currentFolderId);
    return f?.default_sensitivity ?? 'L1';
  }, [folders, currentFolderId]);

  // ── Folder mutation handlers ────────────────────────────────────────────────
  const handleRenameFolder = useCallback(
    async (f: KnowledgeFolder) => {
      const next = await prompt({
        title: '重命名文件夹',
        label: '名称',
        defaultValue: f.name,
        required: true,
        confirmText: '保存',
      });
      if (next === null) return;
      const name = next.trim();
      if (!name || name === f.name) return;
      try {
        await api.library.updateFolder(f.id, { name });
        toast.success('已重命名');
        setReloadKey((k) => k + 1);
      } catch (err) {
        toast.error(err instanceof ApiError ? err.message : '重命名失败');
      }
    },
    [prompt, toast],
  );

  const handleDeleteFolder = useCallback(
    async (f: KnowledgeFolder) => {
      if (
        !(await confirm({
          title: '删除文件夹',
          message: `确定删除文件夹「${f.name}」吗？仅空文件夹可删除。`,
          tone: 'danger',
          confirmText: '删除',
        }))
      )
        return;
      try {
        await api.library.deleteFolder(f.id);
        toast.success('已删除文件夹');
        if (currentFolderId === f.id) setCurrentFolderId(null);
        setReloadKey((k) => k + 1);
      } catch (err) {
        const msg =
          err instanceof ApiError
            ? err.status === 409
              ? `文件夹非空：${err.message}`
              : err.message
            : '删除失败';
        toast.error(msg);
      }
    },
    [confirm, toast, currentFolderId],
  );

  // ── Render ──────────────────────────────────────────────────────────────────
  const tenantSelectorOptions = useMemo(() => {
    if (!canSwitchTenant) return [];
    return [
      { value: '', label: '我的租户（管理）' },
      ...tenants
        .filter((t) => t.id !== ownTenantId)
        .map((t) => ({ value: t.id, label: `${t.name}（审计）` })),
    ];
  }, [canSwitchTenant, tenants, ownTenantId]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">知识库</h1>
        <p className="text-[12px] text-dim mt-0.5">
          租户知识库文件夹与文档管理（由 knowledge-ingest 提供）
        </p>
      </div>

      {/* Tenant selector + mode badge */}
      <div className="flex items-center gap-3 flex-wrap">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? '选择租户' : '当前租户'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            options={tenantSelectorOptions}
            value={selectedTenantId ?? ''}
            onChange={(v) => {
              setSelectedTenantId(v || null);
              setAuditDocs([]);
              setSelectedAudit(null);
            }}
            disabled={tenantsLoading}
            className="min-w-[220px]"
          />
        ) : (
          <span className="min-w-[200px] text-[13px] text-ink font-mono px-3 py-1.5 rounded-sm border border-line bg-surface">
            {ownTenantName}
          </span>
        )}
        <span
          className={`px-2 py-0.5 rounded-sm font-mono text-[10.5px] border ${
            isManageMode
              ? 'border-line bg-paper-2 text-ink-2'
              : 'border-line bg-paper-2 text-dim'
          }`}
        >
          {isManageMode ? '管理模式：可增删改' : '审计模式：只读（跨租户）'}
        </span>
      </div>

      {/* ── Manage mode: folder tree + doc list ─────────────────────────────── */}
      {isManageMode && (
        <>
          {manageError && (
            <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
              {manageError}
            </div>
          )}

          {manageLoading && folders.length === 0 && docs.length === 0 && (
            <div className="font-mono text-[12px] text-dim">加载中…</div>
          )}

          <div className="flex gap-4 min-w-0">
            {/* Left: folder tree */}
            <div className="w-[260px] shrink-0 border border-line rounded-md p-2 bg-surface flex flex-col gap-2 max-h-[70vh] overflow-y-auto">
              <div className="flex items-center justify-between px-1">
                <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  文件夹
                </span>
                <button
                  type="button"
                  onClick={() => setCreateFolderOpen(true)}
                  className="px-2 py-0.5 text-[11px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2"
                >
                  + 新建
                </button>
              </div>
              <FolderTree
                folders={folders}
                currentFolderId={currentFolderId}
                onNavigate={setCurrentFolderId}
                onRename={handleRenameFolder}
                onMove={(f) => setMoveFolder(f)}
                onDelete={handleDeleteFolder}
              />
            </div>

            {/* Right: doc list */}
            <div className={`flex-1 min-w-0 flex flex-col gap-2 ${previewDoc ? 'max-w-[50%]' : ''}`}>
              {/* Breadcrumb + upload */}
              <div className="flex items-center gap-2 flex-wrap">
                <div className="flex items-center gap-1 font-mono text-[11px] text-dim flex-wrap">
                  <button
                    type="button"
                    className={`hover:text-ink ${currentFolderId === null ? 'text-ink' : ''}`}
                    onClick={() => setCurrentFolderId(null)}
                  >
                    根目录
                  </button>
                  {breadcrumb.map((f) => (
                    <span key={f.id} className="flex items-center gap-1">
                      <span className="text-dim">/</span>
                      <button
                        type="button"
                        className={`hover:text-ink ${currentFolderId === f.id ? 'text-ink' : ''}`}
                        onClick={() => setCurrentFolderId(f.id)}
                      >
                        {f.name}
                      </button>
                    </span>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={() => setUploadOpen(true)}
                  className="ml-auto px-3 py-1.5 text-[12px] font-mono bg-accent text-surface rounded-sm hover:opacity-90"
                >
                  上传文档
                </button>
              </div>

              {/* Doc table */}
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">文件名</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">密级</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Chunk 数</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">上传时间</th>
                      <th className="px-3 py-2 w-56"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {currentDocs.map((d) => (
                      <ManageDocRow
                        key={d.id}
                        doc={d}
                        onEdited={(doc) => setEditSensDoc(doc)}
                        onMoved={(doc) => setMoveDoc(doc)}
                        onReingest={() => setReloadKey((k) => k + 1)}
                        onDeleted={() => setReloadKey((k) => k + 1)}
                        onPreview={setPreviewDoc}
                      />
                    ))}
                    {currentDocs.length === 0 && !manageLoading && (
                      <tr>
                        <td colSpan={6} className="px-3 py-6 text-center font-mono text-[12px] text-dim italic">
                          该文件夹暂无文档
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
            {previewDoc && (
              <div className="w-[440px] shrink-0 border border-line rounded-md overflow-hidden max-h-[70vh]">
                <FilePreviewPanel
                  getSource={getPreviewSource}
                  file={previewDoc}
                  onClose={() => setPreviewDoc(null)}
                />
              </div>
            )}
          </div>
        </>
      )}

      {/* ── Audit mode: cross-tenant read-only list ────────────────────────── */}
      {!isManageMode && (
        <>
          {auditError && (
            <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
              {auditError}
            </div>
          )}
          {isTodoEndpoint && (
            <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
              该能力暂未启用：knowledge-ingest 代理端点尚未就绪，接线后将自动展示文档列表。
            </div>
          )}
          {auditLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
          {!auditLoading && !isTodoEndpoint && auditDocs.length === 0 && !auditError && (
            <div className="font-mono text-[12px] text-dim italic">该租户暂无文档</div>
          )}
          {auditDocs.length > 0 && (
            <div className="border border-line rounded-md overflow-hidden">
              <table className="w-full text-left border-collapse">
                <thead className="bg-paper-2 border-b border-line">
                  <tr>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">文件名</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">密级</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Chunk 数</th>
                    <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">上传时间</th>
                    <th className="px-3 py-2 w-40"></th>
                  </tr>
                </thead>
                <tbody>
                  {auditDocs.map((d) => (
                    <AuditDocRow
                      key={d.id}
                      doc={d}
                      apiTenantId={apiTenantId}
                      onSelect={setSelectedAudit}
                      onPreview={setPreviewDoc}
                      selected={selectedAudit?.id === d.id}
                      onDeleted={() => {
                        setSelectedAudit(null);
                        setAuditReloadKey((k) => k + 1);
                      }}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {selectedAudit && (
            <DocDetailPanel
              doc={selectedAudit}
              apiTenantId={apiTenantId}
              onPreview={setPreviewDoc}
              onClose={() => setSelectedAudit(null)}
            />
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
        </>
      )}

      {/* ── Manage-mode modals ──────────────────────────────────────────────── */}
      {createFolderOpen && (
        <CreateFolderDialog
          parentId={currentFolderId}
          onClose={() => setCreateFolderOpen(false)}
          onCreated={() => {
            setCreateFolderOpen(false);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
      {uploadOpen && (
        <UploadDialog
          folderId={currentFolderId}
          defaultSensitivity={currentFolderDefaultSensitivity}
          onClose={() => setUploadOpen(false)}
          onUploaded={() => {
            setUploadOpen(false);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
      {editSensDoc && (
        <EditSensitivityDialog
          doc={editSensDoc}
          onClose={() => setEditSensDoc(null)}
          onDone={() => {
            setEditSensDoc(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
      {moveDoc && (
        <MoveDocDialog
          doc={moveDoc}
          folders={folders}
          currentFolderId={currentFolderId}
          onClose={() => setMoveDoc(null)}
          onDone={() => {
            setMoveDoc(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
      {moveFolder && (
        <MoveFolderDialog
          folder={moveFolder}
          folders={folders}
          onClose={() => setMoveFolder(null)}
          onDone={() => {
            setMoveFolder(null);
            setReloadKey((k) => k + 1);
          }}
        />
      )}
    </div>
  );
}
