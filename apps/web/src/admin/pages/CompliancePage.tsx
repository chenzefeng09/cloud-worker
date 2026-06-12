import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { usePrompt, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect, GroupedSelect, ComboBox, MultiChipSelect } from '../components/CustomSelect';
import type { ComplianceReport, DsarRecord, RopaRecord } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

// 处理个人数据的法律依据：GDPR 第 6 条 6 项；PIPL（《个人信息保护法》）第 13 条各项。
// 注意 PIPL 无 GDPR 的 legitimate_interests（正当利益）。
const LEGAL_BASIS_GROUPS = [
  {
    label: 'PIPL 第 13 条',
    options: [
      { value: 'PIPL_CONSENT', label: 'PIPL_CONSENT — 知情同意' },
      { value: 'PIPL_SEPARATE_CONSENT', label: 'PIPL_SEPARATE_CONSENT — 单独同意' },
      { value: 'PIPL_CONTRACT', label: 'PIPL_CONTRACT — 订立/履行合同所必需' },
      { value: 'PIPL_HR_MANAGEMENT', label: 'PIPL_HR_MANAGEMENT — 人力资源管理所必需' },
      { value: 'PIPL_LEGAL_DUTY', label: 'PIPL_LEGAL_DUTY — 法定职责/义务所必需' },
      { value: 'PIPL_PUBLIC_HEALTH', label: 'PIPL_PUBLIC_HEALTH — 应对突发公共卫生事件' },
      { value: 'PIPL_PUBLIC_INTEREST', label: 'PIPL_PUBLIC_INTEREST — 公共利益新闻报道/舆论监督' },
      { value: 'PIPL_PUBLIC_DATA', label: 'PIPL_PUBLIC_DATA — 合理处理已公开信息' },
    ],
  },
  {
    label: 'GDPR 第 6 条',
    options: [
      { value: 'CONSENT', label: 'CONSENT — 同意' },
      { value: 'CONTRACT', label: 'CONTRACT — 合同所必需' },
      { value: 'LEGAL_OBLIGATION', label: 'LEGAL_OBLIGATION — 法定义务' },
      { value: 'VITAL_INTEREST', label: 'VITAL_INTEREST — 重大利益' },
      { value: 'PUBLIC_TASK', label: 'PUBLIC_TASK — 公共利益/公务' },
      { value: 'LEGITIMATE_INTEREST', label: 'LEGITIMATE_INTEREST — 正当利益' },
    ],
  },
];

function csv(v: string): string[] {
  return v.split(',').map((x) => x.trim()).filter(Boolean);
}

// 报告类型：后端不校验枚举（透传给上游 foundation-compliance），这里给常见预设但仍允许自定义。
const REPORT_TYPE_OPTIONS = [
  { value: 'annual', label: 'annual — 年度报告' },
  { value: 'quarterly', label: 'quarterly — 季度报告' },
  { value: 'pipl_annual', label: 'pipl_annual — PIPL 年度报告' },
  { value: 'dpia', label: 'dpia — 数据保护影响评估' },
];

// 合规模式：固定取值，与列表中存储的小写值一致。
const COMPLIANCE_MODE_OPTIONS = ['gdpr', 'pipl'];

// 数据类别：后端不校验枚举（透传），给常见预设但仍允许自定义。ROPA（单数）与 DPIA（多值）共用。
const DATA_CATEGORY_OPTIONS = [
  { value: 'pii', label: 'pii — 个人身份信息' },
  { value: 'contact', label: 'contact — 联系方式' },
  { value: 'location', label: 'location — 位置信息' },
  { value: 'financial', label: 'financial — 财务信息' },
  { value: 'health', label: 'health — 健康信息' },
  { value: 'biometric', label: 'biometric — 生物识别信息' },
  { value: 'behavioral', label: 'behavioral — 行为/使用数据' },
];
const DATA_CATEGORY_VALUES = DATA_CATEGORY_OPTIONS.map((o) => o.value);

export function CompliancePage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();
  const prompt = usePrompt();
  const tid = selectedTenantId ?? undefined;

  const [ropa, setRopa] = useState<RopaRecord[]>([]);
  const [reports, setReports] = useState<ComplianceReport[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // ROPA form.
  const [ropaOpen, setRopaOpen] = useState(false);
  const [rService, setRService] = useState('');
  const [rDataCategory, setRDataCategory] = useState('');
  const [rPurpose, setRPurpose] = useState('');
  const [rLegalBasis, setRLegalBasis] = useState('PIPL_CONSENT');
  const [rSubjects, setRSubjects] = useState('');
  const [rRecipients, setRRecipients] = useState('');
  const [rBusy, setRBusy] = useState(false);

  // Report form.
  const [repOpen, setRepOpen] = useState(false);
  const [repType, setRepType] = useState('');
  const [repModes, setRepModes] = useState<string[]>([]);
  const [repBusy, setRepBusy] = useState(false);

  // DSAR.
  const [dsarOpen, setDsarOpen] = useState(false);
  const [dsarUser, setDsarUser] = useState('');
  const [dsarType, setDsarType] = useState('access');
  const [dsarDetails, setDsarDetails] = useState('');
  const [dsarBusy, setDsarBusy] = useState(false);
  const [dsarResult, setDsarResult] = useState<DsarRecord | null>(null);
  const [dsarError, setDsarError] = useState<string | null>(null);
  const [dsarQueryId, setDsarQueryId] = useState('');
  const [dsarQueryResult, setDsarQueryResult] = useState<DsarRecord | null>(null);

  // DPIA.
  const [dpiaOpen, setDpiaOpen] = useState(false);
  const [dpiaProject, setDpiaProject] = useState('');
  const [dpiaDesc, setDpiaDesc] = useState('');
  const [dpiaCategories, setDpiaCategories] = useState<string[]>([]);
  const [dpiaBusy, setDpiaBusy] = useState(false);
  const [dpiaError, setDpiaError] = useState<string | null>(null);

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    Promise.all([api.admin.compliance.ropa.list(tid), api.admin.compliance.reports.list(tid)])
      .then(([r, rep]) => { setRopa(r); setReports(rep); })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tid]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const handleCreateRopa = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!rService.trim() || !rPurpose.trim()) return;
    setRBusy(true);
    setError(null);
    try {
      await api.admin.compliance.ropa.create(
        {
          service: rService.trim(),
          data_category: rDataCategory.trim(),
          purpose: rPurpose.trim(),
          legal_basis: rLegalBasis.trim(),
          data_subjects: csv(rSubjects),
          recipients: csv(rRecipients),
        },
        tid,
      );
      toast.success('已新建 ROPA 记录');
      setRService(''); setRDataCategory(''); setRPurpose(''); setRSubjects(''); setRRecipients(''); setRopaOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setRBusy(false);
    }
  };

  const handleGenerateReport = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!repType.trim()) return;
    setRepBusy(true);
    setError(null);
    try {
      await api.admin.compliance.reports.generate(
        { report_type: repType.trim(), compliance_modes: repModes },
        tid,
      );
      toast.success('已生成合规报告');
      setRepType(''); setRepModes([]); setRepOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '生成失败');
    } finally {
      setRepBusy(false);
    }
  };

  const handleCreateDsar = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dsarUser.trim()) { setDsarError('请填写用户 ID'); return; }
    setDsarBusy(true);
    setDsarError(null);
    setDsarResult(null);
    try {
      const res = await api.admin.compliance.dsar.create(
        { user_id: dsarUser.trim(), request_type: dsarType, details: dsarDetails.trim() },
        tid,
      );
      setDsarResult(res);
      toast.success('已创建 DSAR');
      setDsarOpen(false);
    } catch (err) {
      setDsarError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setDsarBusy(false);
    }
  };

  const handleQueryDsar = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dsarQueryId.trim()) return;
    setDsarError(null);
    setDsarQueryResult(null);
    try {
      const res = await api.admin.compliance.dsar.get(dsarQueryId.trim(), tid);
      setDsarQueryResult(res);
    } catch (err) {
      setDsarError(err instanceof ApiError ? err.message : '查询失败');
    }
  };

  const handleCreateDpia = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dpiaProject.trim()) { setDpiaError('请填写项目名称'); return; }
    setDpiaBusy(true);
    setDpiaError(null);
    try {
      const created = await api.admin.compliance.dpia.create(
        {
          project_name: dpiaProject.trim(),
          description: dpiaDesc.trim(),
          data_categories: dpiaCategories,
          risks: [],
          mitigations: [],
        },
        tid,
      );
      toast.success('已创建 DPIA');
      // Offer to complete it immediately.
      const conclusion = await prompt({
        title: '完成 DPIA（可选）',
        label: '结论',
        description: '填写结论以立即完成本次 DPIA，留空则稍后处理。',
      });
      if (conclusion !== null && conclusion.trim() && created.id) {
        const residual = await prompt({ title: '完成 DPIA', label: '残余风险', defaultValue: '无' });
        await api.admin.compliance.dpia.complete(
          created.id,
          { conclusion: conclusion.trim(), residual_risks: (residual ?? '').trim() || '无' },
          tid,
        );
        toast.success('DPIA 已完成');
      }
      setDpiaProject(''); setDpiaDesc(''); setDpiaCategories([]); setDpiaOpen(false);
    } catch (err) {
      setDpiaError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setDpiaBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">合规中心</h1>
        <p className="text-[12px] text-dim mt-0.5">ROPA、合规报告、DSAR 与 DPIA（→ compliance）</p>
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
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：合规端点不可达，接线后将自动展示。</div>}

      {/* ROPA */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">ROPA（处理活动记录）</div>
          {!ropaOpen && <button onClick={() => setRopaOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建</button>}
        </div>
        {ropaOpen && (
          <Dialog title="新建 ROPA" onClose={() => setRopaOpen(false)} dismissable={!rBusy} className="w-full max-w-2xl">
          <form onSubmit={handleCreateRopa} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">服务</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rService} onChange={(e) => setRService(e.target.value)} placeholder="billing" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">数据类别</label>
                <ComboBox options={DATA_CATEGORY_OPTIONS} value={rDataCategory} onChange={setRDataCategory} placeholder="选择或输入…" />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">处理目的</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rPurpose} onChange={(e) => setRPurpose(e.target.value)} placeholder="invoicing" />
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">法律依据</label>
                <GroupedSelect groups={LEGAL_BASIS_GROUPS} value={rLegalBasis} onChange={setRLegalBasis} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">数据主体 (逗号)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rSubjects} onChange={(e) => setRSubjects(e.target.value)} placeholder="customers" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">接收方 (逗号)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rRecipients} onChange={(e) => setRRecipients(e.target.value)} placeholder="processor-x" />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={rBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{rBusy ? '创建中…' : '创建'}</button>
              <button type="button" onClick={() => setRopaOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
            </div>
          </form>
          </Dialog>
        )}
        {!loading && !todo && ropa.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无 ROPA 记录</div>}
        {ropa.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">服务</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">目的</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">法律依据</th>
                </tr>
              </thead>
              <tbody>
                {ropa.map((r, i) => (
                  <tr key={r.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 text-[13px] text-ink">{r.service ?? '—'}</td>
                    <td className="px-3 py-2 text-[12px] text-ink-2">{r.purpose ?? '—'}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">{r.legal_basis ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Reports */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">合规报告</div>
          {!repOpen && <button onClick={() => setRepOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 生成报告</button>}
        </div>
        {repOpen && (
          <Dialog title="生成合规报告" onClose={() => setRepOpen(false)} dismissable={!repBusy} className="w-full max-w-xl">
          <form onSubmit={handleGenerateReport} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">报告类型</label>
                <ComboBox options={REPORT_TYPE_OPTIONS} value={repType} onChange={setRepType} placeholder="选择或输入…" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">合规模式</label>
                <MultiChipSelect options={COMPLIANCE_MODE_OPTIONS} value={repModes} onChange={setRepModes} placeholder="选择合规模式…" />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={repBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{repBusy ? '生成中…' : '生成'}</button>
              <button type="button" onClick={() => setRepOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
            </div>
          </form>
          </Dialog>
        )}
        {!loading && !todo && reports.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无合规报告</div>}
        {reports.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">合规模式</th>
                </tr>
              </thead>
              <tbody>
                {reports.map((r, i) => (
                  <tr key={r.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 text-[13px] text-ink">{r.report_type ?? '—'}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{r.compliance_modes?.join(', ') || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* DSAR */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">DSAR（数据主体请求）</div>
          {!dsarOpen && <button onClick={() => setDsarOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建 DSAR</button>}
        </div>
        {dsarOpen && (
          <Dialog title="新建 DSAR" onClose={() => setDsarOpen(false)} dismissable={!dsarBusy} className="w-full max-w-md">
          <form onSubmit={handleCreateDsar} className="flex flex-col gap-2">
            {dsarError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{dsarError}</div>}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">用户 ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={dsarUser} onChange={(e) => setDsarUser(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">请求类型</label>
              <CustomSelect
                options={[
                  { value: 'access', label: 'access（访问）' },
                  { value: 'erasure', label: 'erasure（删除）' },
                  { value: 'rectification', label: 'rectification（更正）' },
                  { value: 'portability', label: 'portability（可携带）' },
                ]}
                value={dsarType}
                onChange={(v) => setDsarType(v)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">详情</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dsarDetails} onChange={(e) => setDsarDetails(e.target.value)} />
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={dsarBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{dsarBusy ? '提交中…' : '创建'}</button>
              <button type="button" onClick={() => setDsarOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
            </div>
          </form>
          </Dialog>
        )}
        {dsarResult && (
          <div className="border border-line rounded-md p-3 bg-surface text-[12px] text-ink-2 font-mono">
            已创建 DSAR：{dsarResult.id} · 状态 {dsarResult.status ?? '—'}
          </div>
        )}
        <div className="flex flex-col md:flex-row gap-3">
          <form onSubmit={handleQueryDsar} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 flex-1">
            <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">按 ID 查询</div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">DSAR ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={dsarQueryId} onChange={(e) => setDsarQueryId(e.target.value)} />
            </div>
            <div>
              <button type="submit" className="px-2.5 py-1 text-[11px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2">查询</button>
            </div>
            {dsarQueryResult && (
              <div className="border-t border-line-2 pt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
                <span className="font-mono text-dim">用户</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.user_id ?? '—'}</span>
                <span className="font-mono text-dim">类型</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.request_type ?? '—'}</span>
                <span className="font-mono text-dim">状态</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.status ?? '—'}</span>
              </div>
            )}
          </form>
        </div>
      </section>

      {/* DPIA */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">DPIA（数据保护影响评估）</div>
          {!dpiaOpen && <button onClick={() => setDpiaOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建 DPIA</button>}
        </div>
        {dpiaOpen && (
          <Dialog title="新建 DPIA" onClose={() => setDpiaOpen(false)} dismissable={!dpiaBusy} className="w-full max-w-2xl">
          <form onSubmit={handleCreateDpia} className="flex flex-col gap-2">
            {dpiaError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{dpiaError}</div>}
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">项目名称</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dpiaProject} onChange={(e) => setDpiaProject(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">数据类别</label>
              <MultiChipSelect options={DATA_CATEGORY_VALUES} value={dpiaCategories} onChange={setDpiaCategories} placeholder="选择数据类别…" />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">描述</label>
            <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dpiaDesc} onChange={(e) => setDpiaDesc(e.target.value)} />
          </div>
          <div className="flex gap-2">
            <button type="submit" disabled={dpiaBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{dpiaBusy ? '创建中…' : '创建 DPIA'}</button>
            <button type="button" onClick={() => setDpiaOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">取消</button>
          </div>
        </form>
          </Dialog>
        )}
      </section>
    </div>
  );
}
