import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type {
  QuotaInfo,
  TenantSubscription,
  UsageRecord,
} from '../../api/types';

const TIER_LABELS: Record<string, string> = {
  FREE: '免费版',
  PRO: '专业版',
  BUSINESS: '商业版',
  ENTERPRISE: '企业版',
  CUSTOM: '定制',
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

type UpstreamPlan = {
  id: string;
  name: string;
  tier?: string;
  quotas?: Record<string, number>;
  price_monthly_usd?: number;
  active?: boolean;
  kind?: string;
  included_quotas?: Record<string, number>;
  price_amount?: number;
  currency?: string;
};

function planTier(p: UpstreamPlan): string {
  return p.tier ?? p.kind ?? '';
}

function formatDate(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString('zh-CN');
}

// ─── Bind Plan (create subscription for tenant without one) ─────────────────

function BindPlanForm({
  tenantId,
  plans,
  onBound,
}: {
  tenantId: string;
  plans: UpstreamPlan[];
  onBound: () => void;
}) {
  const toast = useToast();
  const [selectedPlan, setSelectedPlan] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleBind = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedPlan) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.billing.createSubscription({ plan_id: selectedPlan }, tenantId);
      toast.success('已为租户绑定套餐');
      onBound();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '绑定失败');
    } finally {
      setBusy(false);
    }
  };

  const activePlans = plans.filter((p) => p.active !== false);

  return (
    <form onSubmit={handleBind} className="flex items-end gap-2 flex-wrap mt-2">
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm w-full">{error}</div>}
      <label className="flex flex-col gap-1">
        <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">选择套餐</span>
        <CustomSelect
          options={[
            { value: '', label: '— 请选择 —' },
            ...activePlans.map((p) => ({
              value: p.id,
              label: `${p.name} (${TIER_LABELS[planTier(p)] ?? planTier(p)})`,
            })),
          ]}
          value={selectedPlan}
          onChange={setSelectedPlan}
          className="min-w-[220px]"
        />
      </label>
      <button
        type="submit"
        disabled={busy || !selectedPlan}
        className="px-3 py-1.5 text-[11px] font-mono bg-ink text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
      >
        {busy ? '绑定中…' : '绑定套餐'}
      </button>
    </form>
  );
}

// ─── Subscription actions (change plan / cancel) ────────────────────────────

function SubscriptionActions({
  tenantId,
  subscription,
  plans,
  onChanged,
}: {
  tenantId: string;
  subscription: TenantSubscription;
  plans: UpstreamPlan[];
  onChanged: () => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [changing, setChanging] = useState(false);
  const [targetPlan, setTargetPlan] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleChangePlan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!targetPlan) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.billing.changePlan({ new_plan_id: targetPlan }, tenantId);
      toast.success('已提交套餐变更');
      setChanging(false);
      setTargetPlan('');
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '变更失败');
    } finally {
      setBusy(false);
    }
  };

  const handleCancel = async () => {
    if (
      !(await confirm({
        title: '取消订阅',
        message: '确定取消该租户的当前订阅吗？此操作可能影响其服务可用性。',
        tone: 'danger',
        confirmText: '取消订阅',
      }))
    )
      return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.billing.cancelSubscription({}, tenantId);
      toast.success('已提交取消订阅');
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '取消失败');
    } finally {
      setBusy(false);
    }
  };

  const candidates = plans.filter((p) => p.active !== false && p.id !== subscription.plan_id);

  return (
    <div className="border-t border-line-2 pt-3 flex flex-col gap-2">
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {changing ? (
        <form onSubmit={handleChangePlan} className="flex items-center gap-2 flex-wrap">
          <CustomSelect
            options={[{ value: '', label: '— 选择目标套餐 —' }, ...candidates.map((p) => ({ value: p.id, label: p.name }))]}
            value={targetPlan}
            onChange={(v) => setTargetPlan(v)}
            className="min-w-[180px]"
          />
          <button
            type="submit"
            disabled={busy || !targetPlan}
            className="px-2.5 py-1.5 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {busy ? '提交中…' : '确认变更'}
          </button>
          <button
            type="button"
            onClick={() => { setChanging(false); setTargetPlan(''); setError(null); }}
            className="px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
        </form>
      ) : (
        <div className="flex items-center gap-2">
          <button
            onClick={() => setChanging(true)}
            disabled={busy}
            className="px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
          >
            变更套餐
          </button>
          <button
            onClick={handleCancel}
            disabled={busy}
            className="px-2.5 py-1.5 text-[11px] font-mono border border-red-200 text-red-600 rounded-sm hover:bg-red-50 disabled:opacity-50"
          >
            取消订阅
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────────

export function QuotaPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();

  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [quotaLoading, setQuotaLoading] = useState(false);
  const [quotaTodo, setQuotaTodo] = useState(false);

  const [plans, setPlans] = useState<UpstreamPlan[]>([]);

  const [subscription, setSubscription] = useState<TenantSubscription | null>(null);
  const [subLoading, setSubLoading] = useState(false);
  const [subTodo, setSubTodo] = useState(false);
  const [subReloadKey, setSubReloadKey] = useState(0);

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

  // Plan catalogue — needed for BindPlanForm and SubscriptionActions.
  useEffect(() => {
    let cancelled = false;
    api.admin.billing
      .plans()
      .then((items) => { if (!cancelled) setPlans(items as unknown as UpstreamPlan[]); })
      .catch(() => {});
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
  }, [selectedTenantId, subReloadKey]);

  const planNameById = (id: string) => plans.find((p) => p.id === id)?.name ?? id;
  const refreshSubscription = () => setSubReloadKey((k) => k + 1);

  const noSubscription = !subLoading && !subTodo && (subscription?.status as string) === 'NO_SUBSCRIPTION';

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">订阅与配额</h1>
        <p className="text-[12px] text-dim mt-0.5">租户订阅管理与用量明细</p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {/* ─── 租户选择 ─────────────────────────────────────────── */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '— 请选择 —' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => {
            setSelectedTenantId(v || null);
            setQuota(null);
            setSubscription(null);
            setUsage(null);
          }}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
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
              <div className="border border-line rounded-md p-4 flex flex-col gap-2">
                <div className="text-[13px] text-dim">该租户暂无有效订阅</div>
                <BindPlanForm
                  tenantId={selectedTenantId}
                  plans={plans}
                  onBound={refreshSubscription}
                />
              </div>
            )}
            {!quotaLoading && !quotaTodo && quota && quota.plan && (
              <div className="flex flex-col gap-4">
                <div className="border border-line rounded-md p-4 bg-surface">
                  <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mb-2">当前套餐</div>
                  <div className="text-[18px] font-display text-ink">{quota.plan.name}</div>
                  <div className="font-mono text-[11px] text-dim mt-1">
                    {TIER_LABELS[quota.plan.tier] ?? quota.plan.tier}
                  </div>
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
            {!subLoading && !subTodo && (noSubscription || !subscription) && (
              <div className="border border-line rounded-md p-4 text-[13px] text-dim">该租户暂无订阅记录</div>
            )}
            {!subLoading && !subTodo && subscription && !noSubscription && (
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
                {selectedTenantId && (
                  <SubscriptionActions
                    tenantId={selectedTenantId}
                    subscription={subscription}
                    plans={plans}
                    onChanged={refreshSubscription}
                  />
                )}
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
