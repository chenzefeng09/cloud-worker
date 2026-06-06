import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { QuotaInfo } from '../../api/types';

export function QuotaPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);

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
    api.admin.billing
      .quota(selectedTenantId)
      .then((data) => { if (!cancelled) setQuota(data); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
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
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">配额套餐</h1>
        <p className="text-[12px] text-dim mt-0.5">查看租户当前用量与套餐限额</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <select
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]"
          value={selectedTenantId ?? ''}
          onChange={(e) => { setSelectedTenantId(e.target.value || null); setQuota(null); }}
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

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          BFF 配额代理端点尚未就绪，当前占位显示。接线后将自动展示用量数据。
        </div>
      )}

      {quota && !quota.plan && (
        <div className="border border-line rounded-md p-4 text-[13px] text-dim">
          该租户暂无有效订阅
        </div>
      )}

      {quota && quota.plan && (
        <div className="flex flex-col gap-4">
          {/* Plan card */}
          <div className="border border-line rounded-md p-4 bg-surface">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mb-2">当前套餐</div>
            <div className="text-[18px] font-display text-ink">{quota.plan.name}</div>
            <div className="font-mono text-[11px] text-dim mt-1">{quota.plan.tier}</div>
          </div>

          {/* Limits & usage */}
          <div className="border border-line rounded-md overflow-hidden">
            <div className="bg-paper-2 px-3 py-2 border-b border-line font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
              用量 / 限额
            </div>
            <table className="w-full text-left border-collapse">
              <thead className="border-b border-line-2">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim">指标</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim">已用</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim">上限</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim">占比</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(quota.plan.limits ?? {}).map(([key, limit]) => {
                  const used = quota.usage[key] ?? 0;
                  const pct = limit > 0 ? Math.round((used / limit) * 100) : 0;
                  return (
                    <tr key={key} className="border-b border-line-2 hover:bg-paper-2">
                      <td className="px-3 py-2 font-mono text-[12px] text-ink-2">{key}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-ink">{used.toLocaleString()}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-dim">{limit.toLocaleString()}</td>
                      <td className="px-3 py-2">
                        <div className="flex items-center gap-2">
                          <div className="flex-1 h-1.5 bg-line-2 rounded-full overflow-hidden max-w-[80px]">
                            <div
                              className={`h-full rounded-full ${pct >= 90 ? 'bg-accent' : pct >= 70 ? 'bg-warn' : 'bg-ok'}`}
                              style={{ width: `${Math.min(pct, 100)}%` }}
                            />
                          </div>
                          <span className="font-mono text-[11px] text-dim">{pct}%</span>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {!selectedTenantId && !loading && (
        <div className="font-mono text-[12px] text-dim italic">请先选择一个租户</div>
      )}
    </div>
  );
}
