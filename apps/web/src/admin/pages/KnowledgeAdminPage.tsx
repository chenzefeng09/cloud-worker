import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type { DocumentStatus, IngestResult, KnowledgeDocument } from '../../api/types';

const STATUS_COLOR: Record<DocumentStatus, string> = {
  PENDING: 'text-dim',
  INGESTING: 'text-warn',
  READY: 'text-ok',
  FAILED: 'text-accent',
};

// ─── Document detail panel ────────────────────────────────────────────────────

function DocDetailPanel({
  doc,
  apiTenantId,
  onClose,
}: {
  doc: KnowledgeDocument;
  /** tenant_id to send to the BFF: undefined → caller's own tenant (no
   *  ?tenant_id= override); a concrete id → cross-tenant (platform-admin only). */
  apiTenantId: string | undefined;
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
        <span className="font-mono text-ink-2 break-all">{d.source_uri || '—'}</span>
        <span className="font-mono text-dim">上传时间</span>
        <span className="font-mono text-ink-2">{formatDateTimeAuto(new Date(d.uploaded_at))}</span>
      </div>
    </Dialog>
  );
}

// ─── Document row ─────────────────────────────────────────────────────────────

function DocRow({
  doc,
  apiTenantId,
  onSelect,
  selected,
  onDeleted,
}: {
  doc: KnowledgeDocument;
  /** tenant_id to send to the BFF (see DocDetailPanel). */
  apiTenantId: string | undefined;
  onSelect: (d: KnowledgeDocument) => void;
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
      onClick={() => onSelect(doc)}
      className={`border-b border-line-2 hover:bg-paper-2 transition-colors cursor-pointer group ${
        selected ? 'bg-paper-2' : ''
      }`}
    >
      <td className="px-3 py-2 text-[13px] text-ink truncate max-w-[260px]">{doc.title}</td>
      <td className={`px-3 py-2 font-mono text-[11px] ${STATUS_COLOR[doc.status]}`}>{doc.status}</td>
      <td className="px-3 py-2 font-mono text-[12px] text-ink-2">{doc.chunk_count}</td>
      <td className="px-3 py-2 font-mono text-[11px] text-dim">
        {formatDateTimeAuto(new Date(doc.uploaded_at))}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          {msg && <span className="text-[11px] font-mono text-dim">{msg}</span>}
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

// ─── Page ─────────────────────────────────────────────────────────────────────

export function KnowledgeAdminPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const userProfile = useChatStore((s) => s.userProfile);
  // Platform admins may pick any tenant; tenant admins are locked to their own
  // tenant. The adminStore is shared and may hold a stale cross-tenant id from a
  // prior platform-admin session, so we never trust selectedTenantId for a
  // tenant_admin — they always resolve to their own tenant_id.
  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);
  const ownTenantId = userProfile?.tenant_id ?? null;
  const ownTenantName = tenants.find((t) => t.id === ownTenantId)?.name ?? ownTenantId ?? '当前租户';

  // The tenant whose documents we view: platform-admins follow the selector;
  // tenant-admins are pinned to their own tenant.
  const effectiveTenantId = canSwitchTenant ? selectedTenantId : ownTenantId;
  // What we actually send to the BFF as ?tenant_id=: only platform-admins acting
  // cross-tenant send an override. A tenant_admin (or a platform-admin who hasn't
  // picked a tenant) sends undefined → BFF uses the caller's own tenant and the
  // cross-tenant gate stays in the same-tenant lane.
  const apiTenantId = canSwitchTenant ? (selectedTenantId ?? undefined) : undefined;

  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [selected, setSelected] = useState<KnowledgeDocument | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

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

  useEffect(() => {
    if (!effectiveTenantId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    setSelected(null);
    api.admin.knowledge.documents
      .list(apiTenantId)
      .then((data) => { if (!cancelled) setDocs(data); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setIsTodoEndpoint(true);
        } else {
          setError(err instanceof ApiError ? err.message : '加载失败');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [effectiveTenantId, apiTenantId, reloadKey]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">知识库</h1>
        <p className="text-[12px] text-dim mt-0.5">租户知识库文档治理（→ knowledge-ingest）</p>
      </div>

      {/* Tenant selector — only platform admins may switch; tenant admins are
          locked to their own tenant (shown read-only). */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? '选择租户' : '当前租户'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            options={[{ value: '', label: '— 请选择 —' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
            value={selectedTenantId ?? ''}
            onChange={(v) => { setSelectedTenantId(v || null); setDocs([]); setSelected(null); }}
            disabled={tenantsLoading}
            className="min-w-[200px]"
          />
        ) : (
          <span className="min-w-[200px] text-[13px] text-ink font-mono px-3 py-1.5 rounded-sm border border-line bg-surface">
            {ownTenantName}
          </span>
        )}
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：knowledge-ingest 代理端点尚未就绪，接线后将自动展示文档列表。
        </div>
      )}

      {canSwitchTenant && !effectiveTenantId && !loading && (
        <div className="font-mono text-[12px] text-dim italic">请先选择一个租户</div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {effectiveTenantId && !loading && !isTodoEndpoint && docs.length === 0 && !error && (
        <div className="font-mono text-[12px] text-dim italic">该租户暂无文档</div>
      )}

      {effectiveTenantId && !isTodoEndpoint && docs.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">文件名</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Chunk 数</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">上传时间</th>
                <th className="px-3 py-2 w-40"></th>
              </tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <DocRow
                  key={d.id}
                  doc={d}
                  apiTenantId={apiTenantId}
                  onSelect={setSelected}
                  selected={selected?.id === d.id}
                  onDeleted={() => { setSelected(null); setReloadKey((k) => k + 1); }}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && effectiveTenantId && (
        <DocDetailPanel doc={selected} apiTenantId={apiTenantId} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
