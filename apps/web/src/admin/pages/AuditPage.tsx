import { useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { AuditEvent } from '../../api/types';

export function AuditPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading } = useAdminStore();
  const [tenantId, setTenantId] = useState('');
  const [action, setAction] = useState('');
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [queried, setQueried] = useState(false);

  // Bootstrap tenant list for filter
  if (tenants.length === 0 && !tenantsLoading) {
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }

  const handleQuery = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    setQueried(true);
    try {
      const body: Record<string, unknown> = { limit: 50 };
      if (tenantId) body.tenant_id = tenantId;
      if (action) body.action = action;
      if (from) body.from = from;
      if (to) body.to = to;
      const data = await api.admin.audit.query(body);
      setEvents(data.items);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
        setIsTodoEndpoint(true);
        setEvents([]);
      } else {
        setError(err instanceof ApiError ? err.message : '查询失败');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">审计日志</h1>
        <p className="text-[12px] text-dim mt-0.5">查询平台操作事件</p>
      </div>

      {/* Query form */}
      <form onSubmit={handleQuery} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-3">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">查询条件</div>
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">租户</label>
            <select
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              disabled={tenantsLoading}
            >
              <option value="">全部</option>
              {tenants.map((t) => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">操作 (action)</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
              placeholder="如 tenant.create"
              value={action}
              onChange={(e) => setAction(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">开始时间</label>
            <input
              type="datetime-local"
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">结束时间</label>
            <input
              type="datetime-local"
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </div>
        </div>
        <div>
          <button
            type="submit"
            disabled={loading}
            className="px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? '查询中…' : '查询'}
          </button>
        </div>
      </form>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          审计代理端点尚未就绪。接线后将自动展示查询结果。
        </div>
      )}

      {!isTodoEndpoint && queried && !loading && (
        events.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic">无匹配事件</div>
        ) : (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">时间</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">操作</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Actor</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">租户</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
                      {new Date(ev.created_at).toLocaleString('zh-CN')}
                    </td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink">{ev.action}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                      {ev.resource_type ? `${ev.resource_type}/${ev.resource_id ?? ''}` : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]">
                      {ev.actor_id ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]">
                      {ev.tenant_id}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </div>
  );
}
