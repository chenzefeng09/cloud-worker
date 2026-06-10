import { useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { AuditEvent, AuditChainVerifyResult } from '../../api/types';

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

  // Chain integrity verification state.
  const [verifying, setVerifying] = useState(false);
  const [verifyResult, setVerifyResult] = useState<AuditChainVerifyResult | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);
  const [verifyTodo, setVerifyTodo] = useState(false);

  const handleVerifyChain = async () => {
    setVerifying(true);
    setVerifyResult(null);
    setVerifyError(null);
    setVerifyTodo(false);
    try {
      // tenant_id drives the upstream X-Tenant-Id; the verifier blocks
      // cross-tenant checks, so this must be the selected tenant.
      const result = await api.admin.audit.verifyChain({
        tenant_id: tenantId || undefined,
      });
      setVerifyResult(result);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
        setVerifyTodo(true);
      } else {
        setVerifyError(err instanceof ApiError ? err.message : '校验失败');
      }
    } finally {
      setVerifying(false);
    }
  };

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
    // Upstream audit query is tenant-scoped and rejects a missing tenant_id (422).
    // Require an explicit tenant rather than surfacing the raw validation error.
    if (!tenantId) {
      setEvents([]);
      setQueried(true);
      setIsTodoEndpoint(false);
      setError('请先选择一个租户后再查询审计事件。');
      return;
    }
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    setQueried(true);
    try {
      const body: Record<string, unknown> = { limit: 50, tenant_id: tenantId };
      if (action) body.event_type = action;
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
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={loading}
            className="px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? '查询中…' : '查询'}
          </button>
          <button
            type="button"
            onClick={handleVerifyChain}
            disabled={verifying}
            className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
          >
            {verifying ? '校验中…' : '链完整性校验'}
          </button>
          <span className="text-[11px] text-dim">
            校验所选租户的审计哈希链与 Merkle 签名（未选租户时校验当前管理员租户）
          </span>
        </div>
      </form>

      {/* Chain integrity verification result */}
      {verifyTodo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力服务未启用（链校验代理端点不可达）。
        </div>
      )}
      {verifyError && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {verifyError}
        </div>
      )}
      {verifyResult && (
        <div
          className={[
            'border rounded-md p-4 flex flex-col gap-3',
            verifyResult.valid
              ? 'border-green-200 bg-green-50'
              : 'border-red-200 bg-red-50',
          ].join(' ')}
        >
          <div className="flex items-center gap-2">
            <span
              className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-[12px] font-bold ${verifyResult.valid ? 'bg-green-600 text-white' : 'bg-red-600 text-white'}`}
            >
              {verifyResult.valid ? '✓' : '✗'}
            </span>
            <span className={`text-[14px] font-display ${verifyResult.valid ? 'text-green-800' : 'text-red-800'}`}>
              {verifyResult.valid ? '哈希链完整' : '检测到链断裂'}
            </span>
          </div>
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11.5px]">
            {verifyResult.events_checked !== undefined && (
              <>
                <span className="font-mono text-dim">已校验事件</span>
                <span className="font-mono text-ink-2">{verifyResult.events_checked.toLocaleString()}</span>
              </>
            )}
            {verifyResult.signatures_verified !== undefined && (
              <>
                <span className="font-mono text-dim">签名验证通过</span>
                <span className="font-mono text-ink-2">{verifyResult.signatures_verified.toLocaleString()}</span>
              </>
            )}
            {verifyResult.signatures_failed !== undefined && (
              <>
                <span className="font-mono text-dim">签名验证失败</span>
                <span className={`font-mono ${verifyResult.signatures_failed > 0 ? 'text-red-700' : 'text-ink-2'}`}>
                  {verifyResult.signatures_failed.toLocaleString()}
                </span>
              </>
            )}
            {verifyResult.tsa_present !== undefined && (
              <>
                <span className="font-mono text-dim">含可信时间戳</span>
                <span className="font-mono text-ink-2">{verifyResult.tsa_present.toLocaleString()}</span>
              </>
            )}
            {verifyResult.effective_tenant_id && (
              <>
                <span className="font-mono text-dim">校验租户</span>
                <span className="font-mono text-ink-2 truncate">{verifyResult.effective_tenant_id}</span>
              </>
            )}
            {!verifyResult.valid && verifyResult.first_break_event_id && (
              <>
                <span className="font-mono text-red-700">首个断链事件</span>
                <span className="font-mono text-red-800 truncate">{verifyResult.first_break_event_id}</span>
              </>
            )}
          </div>
          {verifyResult.message && (
            <div className="text-[11.5px] text-dim font-mono">{verifyResult.message}</div>
          )}
        </div>
      )}

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
                  <tr key={ev.event_id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
                      {ev.occurred_at ? new Date(ev.occurred_at).toLocaleString('zh-CN') : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink">{ev.event_type}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                      {ev.resource_type ? `${ev.resource_type}/${ev.resource_id ?? ''}` : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]">
                      {ev.actor_user_id ?? ev.actor_kind ?? '—'}
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
