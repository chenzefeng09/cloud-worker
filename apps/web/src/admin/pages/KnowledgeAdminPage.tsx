import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm } from '../components/dialogs';
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
  tenantId,
  onClose,
}: {
  doc: KnowledgeDocument;
  tenantId: string;
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
      .get(doc.id, tenantId)
      .then((data) => { if (!cancelled) setDetail(data); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [doc.id, tenantId]);

  const d = detail ?? doc;

  return (
    <div className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3 max-w-2xl">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">文档详情</div>
        <button onClick={onClose} className="text-[11px] font-mono text-dim hover:text-ink">关闭</button>
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
        <span className="font-mono text-ink-2 break-all">{d.source_uri || '—'}</span>
        <span className="font-mono text-dim">上传时间</span>
        <span className="font-mono text-ink-2">{new Date(d.uploaded_at).toLocaleString('zh-CN')}</span>
      </div>
    </div>
  );
}

// ─── Document row ─────────────────────────────────────────────────────────────

function DocRow({
  doc,
  tenantId,
  onSelect,
  selected,
}: {
  doc: KnowledgeDocument;
  tenantId: string;
  onSelect: (d: KnowledgeDocument) => void;
  selected: boolean;
}) {
  const [reingesting, setReingesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const confirm = useConfirm();

  const reingest = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!(await confirm({ title: '重新摄取', message: `确认重新摄取文档「${doc.title}」？`, confirmText: '确认' }))) return;
    setReingesting(true);
    setMsg(null);
    try {
      const res: IngestResult = await api.admin.knowledge.documents.reingest(doc.id, tenantId);
      setMsg(`已触发：${res.status}，生成 ${res.chunks_created} 个 chunk`);
    } catch (err) {
      setMsg(err instanceof ApiError ? `失败：${err.message}` : '重新摄取失败');
    } finally {
      setReingesting(false);
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
        {new Date(doc.uploaded_at).toLocaleString('zh-CN')}
      </td>
      <td className="px-3 py-2">
        <div className="flex items-center gap-2">
          {msg && <span className="text-[11px] font-mono text-dim">{msg}</span>}
          <button
            onClick={reingest}
            disabled={reingesting}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity disabled:opacity-50"
          >
            {reingesting ? '摄取中…' : '重新摄取'}
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
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [selected, setSelected] = useState<KnowledgeDocument | null>(null);

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

  useEffect(() => {
    if (!selectedTenantId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    setSelected(null);
    api.admin.knowledge.documents
      .list(selectedTenantId)
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
  }, [selectedTenantId]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">知识库</h1>
        <p className="text-[12px] text-dim mt-0.5">租户知识库文档治理（→ knowledge-ingest）</p>
      </div>

      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <select
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]"
          value={selectedTenantId ?? ''}
          onChange={(e) => { setSelectedTenantId(e.target.value || null); setDocs([]); setSelected(null); }}
          disabled={tenantsLoading}
        >
          <option value="">— 请选择 —</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
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

      {!selectedTenantId && !loading && (
        <div className="font-mono text-[12px] text-dim italic">请先选择一个租户</div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {selectedTenantId && !loading && !isTodoEndpoint && docs.length === 0 && !error && (
        <div className="font-mono text-[12px] text-dim italic">该租户暂无文档</div>
      )}

      {selectedTenantId && !isTodoEndpoint && docs.length > 0 && (
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
                  tenantId={selectedTenantId}
                  onSelect={setSelected}
                  selected={selected?.id === d.id}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && selectedTenantId && (
        <DocDetailPanel doc={selected} tenantId={selectedTenantId} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}
