import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type {
  ConsentRecord,
  CrossBorderResult,
  DataResidency,
  DestructionRequest,
  MaskingRule,
} from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN');
}

function csv(v: string): string[] {
  return v.split(',').map((x) => x.trim()).filter(Boolean);
}

function decisionClass(decision?: string): string {
  switch ((decision ?? '').toUpperCase()) {
    case 'ALLOW': return 'text-green-700';
    case 'REVIEW': return 'text-amber-600';
    case 'BLOCKED': return 'text-red-700';
    default: return 'text-dim';
  }
}

export function DataProtectionPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();
  const confirm = useConfirm();
  const tid = selectedTenantId ?? undefined;

  const [rules, setRules] = useState<MaskingRule[]>([]);
  const [residency, setResidency] = useState<DataResidency | null>(null);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Cross-border check.
  const [cbOpen, setCbOpen] = useState(false);
  const [cbCountry, setCbCountry] = useState('');
  const [cbCategories, setCbCategories] = useState('');
  const [cbResult, setCbResult] = useState<CrossBorderResult | null>(null);
  const [cbBusy, setCbBusy] = useState(false);
  const [cbError, setCbError] = useState<string | null>(null);

  // Destruction.
  const [destOpen, setDestOpen] = useState(false);
  const [destResIds, setDestResIds] = useState('');
  const [destMethod, setDestMethod] = useState('crypto_shred');
  const [destBusy, setDestBusy] = useState(false);
  const [destError, setDestError] = useState<string | null>(null);
  const [destCreated, setDestCreated] = useState<DestructionRequest | null>(null);
  const [destQueryId, setDestQueryId] = useState('');
  const [destQueried, setDestQueried] = useState<DestructionRequest | null>(null);
  const [destQueryError, setDestQueryError] = useState<string | null>(null);

  // Consent.
  const [consentItems, setConsentItems] = useState<ConsentRecord[]>([]);
  const [consentUserFilter, setConsentUserFilter] = useState('');
  const [consentLoading, setConsentLoading] = useState(false);
  const [consentTodo, setConsentTodo] = useState(false);
  const [cOpen, setCOpen] = useState(false);
  const [cUser, setCUser] = useState('');
  const [cType, setCType] = useState('');
  const [cPurpose, setCPurpose] = useState('');
  const [cGranted, setCGranted] = useState(true);
  const [cIp, setCIp] = useState('');
  const [cBusy, setCBusy] = useState(false);
  const [cError, setCError] = useState<string | null>(null);

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  // Static info: masking rules + residency.
  useEffect(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    Promise.all([
      api.admin.dataProtection.maskingRules(tid),
      api.admin.dataProtection.dataResidency(tid),
    ])
      .then(([mr, dr]) => { setRules(mr.rules ?? []); setResidency(dr); })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tid]);

  const loadConsent = useCallback(() => {
    setConsentLoading(true);
    setConsentTodo(false);
    api.admin.dataProtection.consent
      .list(consentUserFilter.trim() || undefined, tid)
      .then(setConsentItems)
      .catch((err) => { if (isTodo(err)) setConsentTodo(true); })
      .finally(() => setConsentLoading(false));
  }, [consentUserFilter, tid]);

  useEffect(() => { loadConsent(); }, [tid]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleCrossBorder = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cbCountry.trim()) { setCbError('请填写目的国家/地区'); return; }
    setCbBusy(true);
    setCbError(null);
    setCbResult(null);
    try {
      const res = await api.admin.dataProtection.crossBorderCheck(
        { destination_country: cbCountry.trim(), data_categories: csv(cbCategories) },
        tid,
      );
      setCbResult(res);
      setCbOpen(false);
    } catch (err) {
      setCbError(err instanceof ApiError ? err.message : '检查失败');
    } finally {
      setCbBusy(false);
    }
  };

  const handleCreateDestruction = async (e: React.FormEvent) => {
    e.preventDefault();
    const ids = csv(destResIds);
    if (ids.length === 0) { setDestError('请填写至少一个资源 ID'); return; }
    setDestBusy(true);
    setDestError(null);
    setDestCreated(null);
    try {
      const res = await api.admin.dataProtection.destruction.create(
        { resource_ids: ids, method: destMethod.trim() || 'crypto_shred' },
        tid,
      );
      setDestCreated(res);
      toast.success('已创建销毁请求');
      setDestResIds(''); setDestOpen(false);
    } catch (err) {
      setDestError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setDestBusy(false);
    }
  };

  const handleQueryDestruction = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!destQueryId.trim()) return;
    setDestQueryError(null);
    setDestQueried(null);
    try {
      const res = await api.admin.dataProtection.destruction.get(destQueryId.trim(), tid);
      setDestQueried(res);
    } catch (err) {
      setDestQueryError(err instanceof ApiError ? err.message : '查询失败');
    }
  };

  const handleApproveDestruction = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({
      title: '审批销毁请求',
      message: '四眼审批：审批人需与申请人不同。确定批准本次销毁？',
      tone: 'danger',
      confirmText: '批准销毁',
    }))) return;
    try {
      const res = await api.admin.dataProtection.destruction.approve(id, tid);
      toast.success('已审批');
      // Refresh whichever view referenced this id.
      if (destCreated?.id === id) setDestCreated(res);
      if (destQueried?.id === id) setDestQueried(res);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '审批失败');
    }
  };

  const handleCreateConsent = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!cUser.trim() || !cType.trim() || !cPurpose.trim()) { setCError('请填写用户 ID、类型与目的'); return; }
    setCBusy(true);
    setCError(null);
    try {
      await api.admin.dataProtection.consent.create(
        {
          user_id: cUser.trim(),
          consent_type: cType.trim(),
          purpose: cPurpose.trim(),
          granted: cGranted,
          ip_address: cIp.trim() || null,
        },
        tid,
      );
      toast.success('已记录同意');
      setCUser(''); setCType(''); setCPurpose(''); setCIp(''); setCGranted(true); setCOpen(false);
      loadConsent();
    } catch (err) {
      setCError(err instanceof ApiError ? err.message : '记录失败');
    } finally {
      setCBusy(false);
    }
  };

  const isPending = (d?: DestructionRequest | null) =>
    !!d && (d.status ?? '').toUpperCase() === 'PENDING_APPROVAL';

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">数据保护</h1>
        <p className="text-[12px] text-dim mt-0.5">脱敏、驻留、跨境、销毁与同意（→ data-protection）</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '（默认 / 当前租户）' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {error && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：数据保护端点不可达，接线后将自动展示。</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {!todo && (
        <>
          {/* Masking rules + residency */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <section className="flex flex-col gap-2">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">脱敏规则</div>
              {rules.length === 0 ? (
                <div className="font-mono text-[12px] text-dim italic">暂无脱敏规则</div>
              ) : (
                <div className="border border-line rounded-md overflow-hidden">
                  <table className="w-full text-left border-collapse">
                    <thead className="bg-paper-2 border-b border-line">
                      <tr>
                        <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">字段</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">方法</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rules.map((r, i) => (
                        <tr key={r.name ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                          <td className="px-3 py-2 font-mono text-[12px] text-ink">{r.name ?? '—'}</td>
                          <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{r.method ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="flex flex-col gap-2">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">数据驻留</div>
              <div className="border border-line rounded-md p-4 bg-surface grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-[12px]">
                <span className="font-mono text-dim">主区域</span>
                <span className="font-mono text-ink-2">{residency?.primary_region ?? '—'}</span>
                <span className="font-mono text-dim">受限目的地</span>
                <span className="font-mono text-ink-2">{residency?.restricted_destinations?.join(', ') || '—'}</span>
                <span className="font-mono text-dim">策略</span>
                <span className="font-mono text-ink-2">{residency?.policy ?? '—'}</span>
              </div>
            </section>
          </div>

          {/* Cross-border check */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">跨境传输检查</div>
              {!cbOpen && <button onClick={() => setCbOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 跨境传输登记</button>}
            </div>
            {cbOpen && (
              <Dialog title="跨境传输登记" onClose={() => setCbOpen(false)} dismissable={!cbBusy} className="w-full max-w-2xl">
              <form onSubmit={handleCrossBorder} className="flex flex-col gap-2">
              {cbError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{cbError}</div>}
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">目的国家/地区</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={cbCountry} onChange={(e) => setCbCountry(e.target.value)} placeholder="US" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">数据类别 (逗号分隔)</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={cbCategories} onChange={(e) => setCbCategories(e.target.value)} placeholder="pii, financial" />
                </div>
              </div>
              <div className="flex gap-2">
                <button type="submit" disabled={cbBusy} className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50">{cbBusy ? '检查中…' : '检查'}</button>
                <button type="button" onClick={() => setCbOpen(false)} className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
              </div>
              </form>
              </Dialog>
            )}
            {cbResult && (
              <div className="border border-line rounded-md p-4 bg-surface grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px] max-w-2xl">
                <span className="font-mono text-dim">是否允许</span>
                <span className="font-mono text-ink-2">{cbResult.allowed === undefined ? '—' : cbResult.allowed ? '是' : '否'}</span>
                <span className="font-mono text-dim">决策</span>
                <span className={`font-mono ${decisionClass(cbResult.decision)}`}>{cbResult.decision ?? '—'}</span>
                <span className="font-mono text-dim">风险</span>
                <span className="font-mono text-ink-2">{cbResult.risks?.join('; ') || '—'}</span>
                <span className="font-mono text-dim">所需措施</span>
                <span className="font-mono text-ink-2">{cbResult.required_actions?.join('; ') || '—'}</span>
              </div>
            )}
          </section>

          {/* Destruction */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">数据销毁</div>
              {!destOpen && <button onClick={() => setDestOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建销毁请求</button>}
            </div>
            {destOpen && (
              <Dialog title="新建销毁请求" onClose={() => setDestOpen(false)} dismissable={!destBusy} className="w-full max-w-md">
              <form onSubmit={handleCreateDestruction} className="flex flex-col gap-2">
                {destError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{destError}</div>}
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">资源 ID (逗号分隔)</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={destResIds} onChange={(e) => setDestResIds(e.target.value)} placeholder="doc-1, doc-2" />
                </div>
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">销毁方法</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={destMethod} onChange={(e) => setDestMethod(e.target.value)} />
                </div>
                <div className="flex gap-2">
                  <button type="submit" disabled={destBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{destBusy ? '提交中…' : '创建'}</button>
                  <button type="button" onClick={() => setDestOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
                </div>
              </form>
              </Dialog>
            )}
            {destCreated && (
              <div className="border border-line rounded-md p-3 bg-surface flex flex-col gap-1 text-[12px] max-w-2xl">
                <div className="font-mono text-ink-2">已创建：{destCreated.id} · 状态 {destCreated.status ?? '—'}</div>
                {isPending(destCreated) && (
                  <button onClick={() => handleApproveDestruction(destCreated.id)} className="self-start text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">审批</button>
                )}
              </div>
            )}
            <div className="flex flex-col md:flex-row gap-3">
              <form onSubmit={handleQueryDestruction} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 flex-1">
                <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">按 ID 查询销毁状态</div>
                {destQueryError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{destQueryError}</div>}
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">销毁请求 ID</label>
                  <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={destQueryId} onChange={(e) => setDestQueryId(e.target.value)} />
                </div>
                <div>
                  <button type="submit" className="px-2.5 py-1 text-[11px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2">查询</button>
                </div>
                {destQueried && (
                  <div className="border-t border-line-2 pt-2 flex flex-col gap-1 text-[12px]">
                    <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
                      <span className="font-mono text-dim">ID</span>
                      <span className="font-mono text-ink-2 break-all">{destQueried.id ?? '—'}</span>
                      <span className="font-mono text-dim">状态</span>
                      <span className="font-mono text-ink-2">{destQueried.status ?? '—'}</span>
                      <span className="font-mono text-dim">方法</span>
                      <span className="font-mono text-ink-2">{destQueried.method ?? '—'}</span>
                    </div>
                    {isPending(destQueried) && (
                      <button onClick={() => handleApproveDestruction(destQueried.id)} className="self-start text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">审批</button>
                    )}
                  </div>
                )}
              </form>
            </div>
          </section>

          {/* Consent */}
          <section className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">同意记录</div>
              {!cOpen && <button onClick={() => setCOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 记录同意</button>}
            </div>

            {cOpen && (
              <Dialog title="记录同意" onClose={() => setCOpen(false)} dismissable={!cBusy} className="w-full max-w-2xl">
              <form onSubmit={handleCreateConsent} className="flex flex-col gap-2">
                {cError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{cError}</div>}
                <div className="grid grid-cols-3 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">用户 ID</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={cUser} onChange={(e) => setCUser(e.target.value)} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">同意类型</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={cType} onChange={(e) => setCType(e.target.value)} placeholder="marketing" />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">处理目的</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={cPurpose} onChange={(e) => setCPurpose(e.target.value)} placeholder="推送营销邮件" />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2 items-end">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">IP 地址 (可选)</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={cIp} onChange={(e) => setCIp(e.target.value)} />
                  </div>
                  <label className="flex items-center gap-2 text-[12px] text-ink-2 pb-1.5">
                    <input type="checkbox" checked={cGranted} onChange={(e) => setCGranted(e.target.checked)} /> 已授予
                  </label>
                </div>
                <div className="flex gap-2">
                  <button type="submit" disabled={cBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{cBusy ? '提交中…' : '记录'}</button>
                  <button type="button" onClick={() => setCOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
                </div>
              </form>
              </Dialog>
            )}

            <div className="flex items-end gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">按用户 ID 过滤 (可选)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono w-[220px]" value={consentUserFilter} onChange={(e) => setConsentUserFilter(e.target.value)} />
              </div>
              <button onClick={loadConsent} className="px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">查询</button>
            </div>

            {consentLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {consentTodo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：同意记录端点不可达。</div>}
            {!consentLoading && !consentTodo && consentItems.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无同意记录</div>}
            {!consentLoading && !consentTodo && consentItems.length > 0 && (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用户</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">目的</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">是否授予</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">记录时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {consentItems.map((c, i) => (
                      <tr key={c.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{c.user_id ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{c.consent_type ?? '—'}</td>
                        <td className="px-3 py-2 text-[12px] text-ink-2 truncate max-w-[200px]">{c.purpose ?? '—'}</td>
                        <td className={`px-3 py-2 font-mono text-[11px] ${c.granted ? 'text-green-700' : 'text-red-600'}`}>{c.granted === undefined ? '—' : c.granted ? '是' : '否'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(c.recorded_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
