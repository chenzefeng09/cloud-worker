import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type {
  QuotaInfo,
  BillingPlanDef,
  TenantSubscription,
  UsageRecord,
} from '../../api/types';

const PLAN_KIND_LABELS: Record<string, string> = {
  FREE: '免费版',
  PRO: '专业版',
  BUSINESS: '商业版',
  ENTERPRISE: '企业版',
  CUSTOM: '定制',
};

const CYCLE_LABELS: Record<string, string> = {
  MONTHLY: '按月',
  YEARLY: '按年',
  ONE_TIME: '一次性',
};

const STATUS_LABELS: Record<string, string> = {
  TRIAL: '试用',
  ACTIVE: '生效中',
  PAST_DUE: '逾期',
  CANCELED: '已取消',
  SUSPENDED: '已暂停',
};

const STATUS_STYLES: Record<string, string> = {
  ACTIVE: 'text-green-700 bg-green-50 border-green-200',
  TRIAL: 'text-blue-700 bg-blue-50 border-blue-200',
  PAST_DUE: 'text-amber-700 bg-amber-50 border-amber-200',
  CANCELED: 'text-gray-600 bg-gray-50 border-gray-200',
  SUSPENDED: 'text-red-700 bg-red-50 border-red-200',
};

function formatPrice(plan: BillingPlanDef): string {
  if (plan.price_amount === undefined || plan.price_amount === null) return '—';
  const amount = plan.price_amount / 100;
  const currency = plan.currency ?? 'CNY';
  return `${amount.toLocaleString()} ${currency}`;
}

function formatDate(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN');
}

export function QuotaPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();

  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaTodo, setQuotaTodo] = useState(false);

  const [plans, setPlans] = useState<BillingPlanDef[]>([]);
  const [plansLoading, setPlansLoading] = useState(false);
  const [plansTodo, setPlansTodo] = useState(false);

  const [subscription, setSubscription] = useState<TenantSubscription | null>(null);
  const [subLoading, setSubLoading] = useState(false);
  const [subTodo, setSubTodo] = useState(false);

  const [usage, setUsage] = useState<UsageRecord | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageTodo, setUsageTodo] = useState(false);

  const [error, setError] = useState<string | null>(null);

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

  // Plan catalogue — independent of tenant selection.
  useEffect(() => {
    let cancelled = false;
    setPlansLoading(true);
    setPlansTodo(false);
    api.admin.billing
      .plans()
      .then((items) => { if (!cancelled) setPlans(items); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setPlansTodo(true);
        }
      })
      .finally(() => { if (!cancelled) setPlansLoading(false); });
    return () => { cancelled = true; };
  }, []);

  // Tenant-scoped data: quota, subscription, usage.
  useEffect(() => {
    if (!selectedTenantId) {
      setQuota(null);
      setSubscription(null);
      setUsage(null);
      return;
    }
    let cancelled = false;
    setError(null);

    const isTodo = (err: unknown) =>
      err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

    setQuotaLoading(true);
    setQuotaTodo(false);
    api.admin.billing
      .quota(selectedTenantId)
      .then((data) => { if (!cancelled) setQuota(data); })
      .catch((err) => {
        if (cancelled) return;
        if (isTodo(err)) setQuotaTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => { if (!cancelled) setQuotaLoading(false); });

    setSubLoading(true);
    setSubTodo(false);
    api.admin.billing
      .subscription(selectedTenantId)
      .then((data) => { if (!cancelled) setSubscription(data); })
      .catch((err) => {
        if (cancelled) return;
        if (isTodo(err)) setSubTodo(true);
      })
      .finally(() => { if (!cancelled) setSubLoading(false); });

    setUsageLoading(true);
    setUsageTodo(false);
    api.admin.billing
      .usage({ tenant_id: selectedTenantId })
      .then((data) => { if (!cancelled) setUsage(data); })
      .catch((err) => {
        if (cancelled) return;
        if (isTodo(err)) setUsageTodo(true);
      })
      .finally(() => { if (!cancelled) setUsageLoading(false); });

    return () => { cancelled = true; };
  }, [selectedTenantId]);

  const planNameById = (id: string) => plans.find((p) => p.id === id)?.name ?? id;

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">计费与配额</h1>
        <p className="text-[12px] text-dim mt-0.5">套餐目录、租户订阅与用量明细</p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {/* ─── 套餐目录 ─────────────────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">套餐目录</div>
        {plansLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
        {plansTodo && (
          <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
            该能力服务未启用（套餐目录代理端点不可达）。
          </div>
        )}
        {!plansLoading && !plansTodo && plans.length === 0 && (
          <div className="font-mono text-[12px] text-dim italic">暂无可用套餐</div>
        )}
        {!plansLoading && !plansTodo && plans.length > 0 && (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {plans.map((plan) => (
              <div key={plan.id} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2">
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="text-[15px] font-display text-ink">{plan.name}</div>
                    <div className="flex items-center gap-1.5 mt-1">
                      {plan.kind && (
                        <span className="inline-flex items-center font-mono text-[10px] text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                          {PLAN_KIND_LABELS[plan.kind] ?? plan.kind}
                        </span>
                      )}
                      {plan.active === false && (
                        <span className="inline-flex items-center font-mono text-[10px] text-dim bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                          已下架
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-[13px] text-ink">{formatPrice(plan)}</div>
                    {plan.billing_cycle && (
                      <div className="font-mono text-[10px] text-dim mt-0.5">
                        {CYCLE_LABELS[plan.billing_cycle] ?? plan.billing_cycle}
                      </div>
                    )}
                  </div>
                </div>
                {plan.description && <p className="text-[11.5px] text-dim">{plan.description}</p>}
                {plan.included_quotas && Object.keys(plan.included_quotas).length > 0 && (
                  <div className="border-t border-line-2 pt-2 mt-1 flex flex-col gap-1">
                    <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-dim">配额维度</div>
                    {Object.entries(plan.included_quotas).map(([dim, amount]) => (
                      <div key={dim} className="flex items-center justify-between text-[11px]">
                        <span className="font-mono text-ink-2">{dim}</span>
                        <span className="font-mono text-dim">{amount.toLocaleString()}</span>
                      </div>
                    ))}
                  </div>
                )}
                {plan.features && plan.features.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {plan.features.map((f) => (
                      <span key={f} className="font-mono text-[10px] text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                        {f}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ─── 租户选择 ─────────────────────────────────────────── */}
      <div className="flex items-center gap-3 border-t border-line-2 pt-4">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <select
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]"
          value={selectedTenantId ?? ''}
          onChange={(e) => {
            setSelectedTenantId(e.target.value || null);
            setQuota(null);
            setSubscription(null);
            setUsage(null);
          }}
          disabled={tenantsLoading}
        >
          <option value="">— 请选择 —</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
      </div>

      {!selectedTenantId && (
        <div className="font-mono text-[12px] text-dim italic">选择一个租户以查看其订阅与用量</div>
      )}

      {selectedTenantId && (
        <>
          {/* ─── 当前套餐 + 用量 ──────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">当前套餐与用量</div>
            {quotaLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {quotaTodo && (
              <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
                该能力服务未启用（配额代理端点不可达）。
              </div>
            )}
            {!quotaLoading && !quotaTodo && quota && !quota.plan && (
              <div className="border border-line rounded-md p-4 text-[13px] text-dim">该租户暂无有效订阅</div>
            )}
            {!quotaLoading && !quotaTodo && quota && quota.plan && (
              <div className="flex flex-col gap-4">
                <div className="border border-line rounded-md p-4 bg-surface">
                  <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mb-2">当前套餐</div>
                  <div className="text-[18px] font-display text-ink">{quota.plan.name}</div>
                  <div className="font-mono text-[11px] text-dim mt-1">{quota.plan.tier}</div>
                </div>
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
          </section>

          {/* ─── 订阅详情 ─────────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">订阅详情</div>
            {subLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {subTodo && (
              <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
                该能力服务未启用（订阅代理端点不可达）。
              </div>
            )}
            {!subLoading && !subTodo && !subscription && (
              <div className="border border-line rounded-md p-4 text-[13px] text-dim">该租户暂无订阅记录</div>
            )}
            {!subLoading && !subTodo && subscription && (
              <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-3">
                <div className="flex items-center gap-3">
                  <div className="text-[15px] font-display text-ink">{planNameById(subscription.plan_id)}</div>
                  {subscription.status && (
                    <span
                      className={`inline-flex items-center font-mono text-[10px] border rounded-sm px-1.5 py-0.5 ${STATUS_STYLES[subscription.status] ?? 'text-dim bg-paper-2 border-line'}`}
                    >
                      {STATUS_LABELS[subscription.status] ?? subscription.status}
                    </span>
                  )}
                </div>
                <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-[11.5px]">
                  <span className="font-mono text-dim">订阅 ID</span>
                  <span className="font-mono text-ink-2 truncate">{subscription.id}</span>
                  <span className="font-mono text-dim">当前周期</span>
                  <span className="font-mono text-ink-2">
                    {formatDate(subscription.current_period_start)} ~ {formatDate(subscription.current_period_end)}
                  </span>
                  {subscription.trial_ends_at && (
                    <>
                      <span className="font-mono text-dim">试用结束</span>
                      <span className="font-mono text-ink-2">{formatDate(subscription.trial_ends_at)}</span>
                    </>
                  )}
                  {subscription.canceled_at && (
                    <>
                      <span className="font-mono text-dim">取消时间</span>
                      <span className="font-mono text-ink-2">{formatDate(subscription.canceled_at)}</span>
                    </>
                  )}
                  {subscription.provider && (
                    <>
                      <span className="font-mono text-dim">计费渠道</span>
                      <span className="font-mono text-ink-2">{subscription.provider}</span>
                    </>
                  )}
                </div>
                <div className="border-t border-line-2 pt-2 text-[11px] text-dim italic">
                  套餐变更暂由实施侧线下配置。
                </div>
              </div>
            )}
          </section>

          {/* ─── 用量明细 ─────────────────────────────────────── */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用量明细</div>
            {usageLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {usageTodo && (
              <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
                该能力服务未启用（用量查询代理端点不可达）。
              </div>
            )}
            {!usageLoading && !usageTodo && usage && (
              <div className="border border-line rounded-md overflow-hidden">
                <div className="bg-paper-2 px-3 py-2 border-b border-line flex items-center justify-between">
                  <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                    {usage.dimension ?? '用量序列'}
                  </span>
                  {usage.total !== undefined && (
                    <span className="font-mono text-[11px] text-ink-2">合计 {usage.total.toLocaleString()}</span>
                  )}
                </div>
                {usage.series && usage.series.length > 0 ? (
                  <table className="w-full text-left border-collapse">
                    <thead className="border-b border-line-2">
                      <tr>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim">时间</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">用量</th>
                      </tr>
                    </thead>
                    <tbody>
                      {usage.series.map((pt, i) => (
                        <tr key={pt.ts ?? i} className="border-b border-line-2 hover:bg-paper-2">
                          <td className="px-3 py-2 font-mono text-[11px] text-dim">{formatDate(pt.ts)}</td>
                          <td className="px-3 py-2 font-mono text-[12px] text-ink text-right">
                            {(pt.amount ?? 0).toLocaleString()}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="px-3 py-4 font-mono text-[12px] text-dim italic">该周期暂无用量数据</div>
                )}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
