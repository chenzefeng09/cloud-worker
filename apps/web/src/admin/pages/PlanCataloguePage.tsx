import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';

const TIER_LABELS: Record<string, string> = {
  FREE: '免费版',
  PRO: '专业版',
  BUSINESS: '商业版',
  ENTERPRISE: '企业版',
  CUSTOM: '定制',
};

const TIERS = ['FREE', 'PRO', 'BUSINESS', 'ENTERPRISE', 'CUSTOM'] as const;

const DEFAULT_QUOTA_DIMS = [
  'user_seats',
  'sessions',
  'agent_sessions',
  'api_calls',
  'llm_tokens',
  'storage_gb',
  'skills',
];

const TIER_PRESETS: Record<string, { price: string; quotas: Record<string, string> }> = {
  FREE: {
    price: '0',
    quotas: { user_seats: '5', sessions: '50', agent_sessions: '100', api_calls: '5000', llm_tokens: '500000', storage_gb: '5', skills: '10' },
  },
  PRO: {
    price: '99',
    quotas: { user_seats: '50', sessions: '500', agent_sessions: '1000', api_calls: '100000', llm_tokens: '10000000', storage_gb: '100', skills: '200' },
  },
  BUSINESS: {
    price: '299',
    quotas: { user_seats: '200', sessions: '2000', agent_sessions: '5000', api_calls: '500000', llm_tokens: '50000000', storage_gb: '500', skills: '1000' },
  },
  ENTERPRISE: {
    price: '999',
    quotas: { user_seats: '1000', sessions: '10000', agent_sessions: '20000', api_calls: '2000000', llm_tokens: '200000000', storage_gb: '2000', skills: '5000' },
  },
  CUSTOM: {
    price: '',
    quotas: { user_seats: '', sessions: '', agent_sessions: '', api_calls: '', llm_tokens: '', storage_gb: '', skills: '' },
  },
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
  description?: string;
  features?: string[];
};

function planQuotas(p: UpstreamPlan): Record<string, number> {
  return p.quotas ?? p.included_quotas ?? {};
}

function planTier(p: UpstreamPlan): string {
  return p.tier ?? p.kind ?? '';
}

function formatPrice(p: UpstreamPlan): string {
  if (p.price_monthly_usd !== undefined && p.price_monthly_usd !== null) {
    return `$${p.price_monthly_usd.toLocaleString()} / 月`;
  }
  if (p.price_amount !== undefined && p.price_amount !== null) {
    const amount = p.price_amount / 100;
    const currency = p.currency ?? 'CNY';
    return `${amount.toLocaleString()} ${currency}`;
  }
  return '—';
}

// ─── Create Plan Form ───────────────────────────────────────────────────────

function CreatePlanForm({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const toast = useToast();
  const [name, setName] = useState('');
  const [tier, setTier] = useState('PRO');
  const [price, setPrice] = useState(TIER_PRESETS.PRO.price);
  const [quotas, setQuotas] = useState<Record<string, string>>(() => ({ ...TIER_PRESETS.PRO.quotas }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const updateQuota = (key: string, val: string) => setQuotas((prev) => ({ ...prev, [key]: val }));

  const applyTierPreset = (newTier: string) => {
    setTier(newTier);
    const preset = TIER_PRESETS[newTier];
    if (preset) {
      setPrice(preset.price);
      setQuotas({ ...preset.quotas });
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    const quotaMap: Record<string, number> = {};
    for (const [k, v] of Object.entries(quotas)) {
      const n = parseInt(v, 10);
      if (n > 0) quotaMap[k] = n;
    }
    try {
      await api.admin.billing.createPlan({
        name: name.trim(),
        tier,
        quotas: quotaMap,
        price_monthly_usd: parseFloat(price) || 0,
      });
      toast.success('套餐创建成功');
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title="新建套餐" onClose={onCancel} dismissable={!busy} className="w-full max-w-2xl">
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">套餐名称</span>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如 Enterprise Plan"
            required
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] font-mono bg-paper focus:outline-none focus:border-ink-2"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">等级</span>
          <CustomSelect
            options={TIERS.map((t) => ({ value: t, label: `${TIER_LABELS[t]} (${t})` }))}
            value={tier}
            onChange={applyTierPreset}
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">月价 (USD)</span>
          <input
            type="number"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
            placeholder="99"
            min={0}
            step="0.01"
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] font-mono bg-paper focus:outline-none focus:border-ink-2"
          />
        </label>
      </div>
      <div>
        <div className="font-mono text-[10px] text-dim uppercase tracking-[0.08em] mb-1.5">配额维度</div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          {DEFAULT_QUOTA_DIMS.map((dim) => (
            <label key={dim} className="flex flex-col gap-0.5">
              <span className="font-mono text-[10px] text-ink-2">{dim}</span>
              <input
                type="number"
                value={quotas[dim]}
                onChange={(e) => updateQuota(dim, e.target.value)}
                placeholder="0"
                min={0}
                className="border border-line rounded-sm px-2 py-1 text-[11px] font-mono bg-paper focus:outline-none focus:border-ink-2 w-full"
              />
            </label>
          ))}
        </div>
      </div>
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="px-3 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
        >
          取消
        </button>
        <button
          type="submit"
          disabled={busy || !name.trim()}
          className="px-3 py-1.5 text-[11px] font-mono bg-ink text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          {busy ? '创建中…' : '创建套餐'}
        </button>
      </div>
      </form>
    </Dialog>
  );
}

// ─── Main Page ──────────────────────────────────────────────────────────────

export function PlanCataloguePage() {
  const [plans, setPlans] = useState<UpstreamPlan[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [showCreate, setShowCreate] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setTodo(false);
    api.admin.billing
      .plans()
      .then((items) => { if (!cancelled) setPlans(items as unknown as UpstreamPlan[]); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setTodo(true);
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [reloadKey]);

  const refresh = () => { setReloadKey((k) => k + 1); setShowCreate(false); };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">套餐目录</h1>
        <p className="text-[12px] text-dim mt-0.5">管理平台可用的计费套餐</p>
      </div>

      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
          共 {plans.length} 个套餐
        </div>
        {!todo && (
          <button
            onClick={() => setShowCreate(true)}
            className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
          >
            + 新建套餐
          </button>
        )}
      </div>

      {showCreate && (
        <CreatePlanForm onCreated={refresh} onCancel={() => setShowCreate(false)} />
      )}

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力服务未启用（套餐目录代理端点不可达）。
        </div>
      )}
      {!loading && !todo && plans.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无可用套餐</div>
      )}
      {!loading && !todo && plans.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {plans.map((plan) => (
            <div key={plan.id} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2">
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="text-[15px] font-display text-ink">{plan.name}</div>
                  <div className="flex items-center gap-1.5 mt-1">
                    {planTier(plan) && (
                      <span className="inline-flex items-center font-mono text-[10px] text-ink-2 bg-paper-2 border border-line rounded-sm px-1.5 py-0.5">
                        {TIER_LABELS[planTier(plan)] ?? planTier(plan)}
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
                </div>
              </div>
              {plan.description && <p className="text-[11.5px] text-dim">{plan.description}</p>}
              {Object.keys(planQuotas(plan)).length > 0 && (
                <div className="border-t border-line-2 pt-2 mt-1 flex flex-col gap-1">
                  <div className="font-mono text-[10px] uppercase tracking-[0.08em] text-dim">配额维度</div>
                  {Object.entries(planQuotas(plan)).map(([dim, amount]) => (
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
    </div>
  );
}
