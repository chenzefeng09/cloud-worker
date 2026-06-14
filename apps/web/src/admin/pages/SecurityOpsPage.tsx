import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type {
  RiskAcceptRequest,
  ScanRun,
  SbomWatch,
  SecurityDrill,
  SecurityIncident,
  Vulnerability,
  VulnSlaDashboard,
} from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

const SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'] as const;
const INCIDENT_CATEGORIES = ['DATA_BREACH', 'RANSOMWARE', 'DDOS', 'INSIDER', 'OUTAGE', 'OTHER'] as const;
const DRILL_KINDS = ['TABLETOP', 'PLAYBOOK_DRILL', 'RED_BLUE_TEAM'] as const;

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

function csv(v: string): string[] {
  return v.split(',').map((x) => x.trim()).filter(Boolean);
}

// findings_count 下游是 {critical,high,medium,low} 计数对象（也可能是数字或缺省）
function fmtFindings(fc: unknown): string {
  if (fc == null) return '—';
  if (typeof fc === 'number') return String(fc);
  if (typeof fc === 'object') {
    const order = ['critical', 'high', 'medium', 'low'];
    const rec = fc as Record<string, unknown>;
    const parts = order
      .filter((k) => typeof rec[k] === 'number' && (rec[k] as number) > 0)
      .map((k) => `${k[0].toUpperCase()}:${rec[k]}`);
    return parts.length ? parts.join(' ') : '0';
  }
  return String(fc);
}

function severityClass(sev?: string): string {
  switch ((sev ?? '').toUpperCase()) {
    case 'CRITICAL': return 'text-red-700';
    case 'HIGH': return 'text-orange-600';
    case 'MEDIUM': return 'text-amber-600';
    case 'LOW': return 'text-green-700';
    default: return 'text-dim';
  }
}

type Tab = 'sla' | 'vulns' | 'incidents' | 'scans' | 'sbom' | 'drills';

// ─── SLA dashboard ──────────────────────────────────────────────────────────────

function SlaTab({ tenantId }: { tenantId: string | null }) {
  const [data, setData] = useState<VulnSlaDashboard | null>(null);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.securityOps.vulnerabilities
      .slaDashboard(tenantId ?? undefined)
      .then(setData)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  if (loading) return <div className="font-mono text-[12px] text-dim">加载中…</div>;
  if (todo) return <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>;
  if (error) return <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>;
  if (!data) return <div className="font-mono text-[12px] text-dim italic">暂无数据</div>;

  const overdue = data.overdue_by_severity ?? {};
  const byOwner = data.by_owner ?? {};
  const mttr = data.mttr_days ?? {};

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
      {/* Overdue by severity */}
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">逾期分级</div>
        {SEVERITIES.map((s) => (
          <div key={s} className="flex items-center justify-between text-[13px]">
            <span className={`font-mono text-[11px] ${severityClass(s)}`}>{s}</span>
            <span className="font-mono text-ink tabular-nums">{overdue[s] ?? 0}</span>
          </div>
        ))}
      </div>

      {/* MTTR */}
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">各级 MTTR（天）</div>
        {SEVERITIES.map((s) => (
          <div key={s} className="flex items-center justify-between text-[13px]">
            <span className={`font-mono text-[11px] ${severityClass(s)}`}>{s}</span>
            <span className="font-mono text-ink tabular-nums">
              {mttr[s] === undefined ? '—' : Number(mttr[s]).toFixed(1)}
            </span>
          </div>
        ))}
      </div>

      {/* By owner */}
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">按责任人逾期</div>
        {Object.keys(byOwner).length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic">暂无</div>
        ) : (
          Object.entries(byOwner).map(([owner, sevs]) => (
            <div key={owner} className="flex items-center justify-between text-[12px]">
              <span className="font-mono text-[11px] text-ink-2 truncate max-w-[120px]">{owner}</span>
              <span className="font-mono text-[11px] text-dim">
                {Object.entries(sevs ?? {}).map(([k, v]) => `${k}:${v}`).join(' ') || '—'}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ─── Vulnerabilities ─────────────────────────────────────────────────────────────

function VulnsTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const [items, setItems] = useState<Vulnerability[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // Filters (dotted alias keys).
  const [fSeverity, setFSeverity] = useState('');
  const [fStatus, setFStatus] = useState('');
  const [fSource, setFSource] = useState('');

  // Detail / edit drawer.
  const [active, setActive] = useState<Vulnerability | null>(null);
  const [editStatus, setEditStatus] = useState('');
  const [editAssignee, setEditAssignee] = useState('');
  const [editPr, setEditPr] = useState('');
  const [editNotes, setEditNotes] = useState('');
  const [saving, setSaving] = useState(false);

  // Risk-accept dialog.
  const [raTarget, setRaTarget] = useState<Vulnerability | null>(null);
  const [raRationale, setRaRationale] = useState('');
  const [raMitigations, setRaMitigations] = useState('');
  const [raExpires, setRaExpires] = useState('');
  const [raBusy, setRaBusy] = useState(false);
  const [raError, setRaError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    const params: Record<string, string | undefined> = {
      'filter.severity': fSeverity || undefined,
      'filter.status': fStatus || undefined,
      'filter.source': fSource || undefined,
    };
    api.admin.securityOps.vulnerabilities
      .list(params, tenantId ?? undefined)
      .then(setItems)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId, fSeverity, fStatus, fSource]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const openDetail = (v: Vulnerability) => {
    setActive(v);
    setEditStatus(v.status ?? '');
    setEditAssignee(v.assignee ?? '');
    setEditPr(v.fix_pr_url ?? '');
    setEditNotes(v.notes_md ?? '');
  };

  const handleSave = async () => {
    if (!active?.id) return;
    setSaving(true);
    try {
      await api.admin.securityOps.vulnerabilities.update(
        active.id,
        { status: editStatus.trim(), assignee: editAssignee.trim(), fix_pr_url: editPr.trim(), notes_md: editNotes },
        tenantId ?? undefined,
      );
      toast.success('已更新漏洞');
      setActive(null);
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '更新失败');
    } finally {
      setSaving(false);
    }
  };

  const handleRiskAccept = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!raTarget?.id) return;
    if (raRationale.trim().length < 20) { setRaError('理由至少需 20 个字符'); return; }
    setRaBusy(true);
    setRaError(null);
    try {
      const body: RiskAcceptRequest = {
        rationale: raRationale.trim(),
        mitigations: csv(raMitigations),
        expires_at: raExpires.trim() || null,
      };
      await api.admin.securityOps.vulnerabilities.riskAccept(raTarget.id, body, tenantId ?? undefined);
      toast.success('已记录风险接受');
      setRaTarget(null); setRaRationale(''); setRaMitigations(''); setRaExpires('');
      refresh();
    } catch (err) {
      setRaError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setRaBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {/* Filters */}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">severity</label>
          <CustomSelect
            options={[{ value: '', label: '全部' }, ...SEVERITIES.map((s) => ({ value: s, label: s }))]}
            value={fSeverity}
            onChange={(v) => setFSeverity(v)}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">status</label>
          <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono w-[140px]" value={fStatus} onChange={(e) => setFStatus(e.target.value)} placeholder="OPEN" />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">source</label>
          <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono w-[140px]" value={fSource} onChange={(e) => setFSource(e.target.value)} placeholder="trivy" />
        </div>
        <button onClick={refresh} className="px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">应用过滤</button>
      </div>

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>}
      {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无漏洞记录</div>}
      {!loading && !todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">CVE</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">标题</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">级别</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">CVSS</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">到期</th>
                <th className="px-3 py-2 w-40"></th>
              </tr>
            </thead>
            <tbody>
              {items.map((v, i) => (
                <tr key={v.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]">{v.cve_id ?? '—'}</td>
                  <td className="px-3 py-2 text-[12px] text-ink truncate max-w-[220px]">{v.title ?? '—'}</td>
                  <td className={`px-3 py-2 font-mono text-[11px] ${severityClass(v.severity)}`}>{v.severity ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2 tabular-nums">{v.cvss_score ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{v.status ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(v.due_at)}</td>
                  <td className="px-3 py-2 flex items-center gap-2">
                    <button onClick={() => openDetail(v)} className="text-[11px] font-mono text-ink-2 border border-line rounded-sm px-2 py-0.5 hover:bg-paper-2">详情</button>
                    <button onClick={() => { setRaTarget(v); setRaError(null); }} className="text-[11px] font-mono text-amber-700 border border-amber-200 rounded-sm px-2 py-0.5 hover:bg-amber-50">风险接受</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Detail / edit */}
      {active && (
        <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">漏洞详情 · {active.cve_id ?? active.id}</div>
            <button onClick={() => setActive(null)} className="text-[11px] font-mono text-dim hover:text-ink">关闭</button>
          </div>
          <div className="text-[12px] text-ink-2">{active.title ?? '—'}</div>
          <div className="font-mono text-[11px] text-dim">受影响包：{active.package_affected ?? '—'}</div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">状态</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={editStatus} onChange={(e) => setEditStatus(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">负责人</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={editAssignee} onChange={(e) => setEditAssignee(e.target.value)} />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">修复 PR 链接</label>
            <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={editPr} onChange={(e) => setEditPr(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">备注 (Markdown)</label>
            <textarea className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y min-h-[80px] font-mono" value={editNotes} onChange={(e) => setEditNotes(e.target.value)} />
          </div>
          <div>
            <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{saving ? '保存中…' : '保存'}</button>
          </div>
        </div>
      )}

      {/* Risk accept */}
      {raTarget && (
        <form onSubmit={handleRiskAccept} className="border border-amber-200 bg-amber-50/40 rounded-md p-4 flex flex-col gap-2 max-w-2xl">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">风险接受 · {raTarget.cve_id ?? raTarget.id}</div>
            <button type="button" onClick={() => setRaTarget(null)} className="text-[11px] font-mono text-dim hover:text-ink">取消</button>
          </div>
          {raError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{raError}</div>}
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">接受理由 (≥20 字符)</label>
            <textarea className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y min-h-[60px]" value={raRationale} onChange={(e) => setRaRationale(e.target.value)} placeholder="说明为何接受该风险…" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">缓解措施 (逗号分隔)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={raMitigations} onChange={(e) => setRaMitigations(e.target.value)} placeholder="WAF 规则, 监控告警" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">到期时间 (可选, ISO)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={raExpires} onChange={(e) => setRaExpires(e.target.value)} placeholder="2026-12-31T00:00:00Z" />
            </div>
          </div>
          <div>
            <button type="submit" disabled={raBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{raBusy ? '提交中…' : '确认接受'}</button>
          </div>
        </form>
      )}
    </div>
  );
}

// ─── Incidents ────────────────────────────────────────────────────────────────

function IncidentsTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const [items, setItems] = useState<SecurityIncident[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // Create form.
  const [creating, setCreating] = useState(false);
  const [severity, setSeverity] = useState<string>('HIGH');
  const [category, setCategory] = useState<string>('OTHER');
  const [title, setTitle] = useState('');
  const [ic, setIc] = useState('');
  const [affected, setAffected] = useState('');
  const [busy, setBusy] = useState(false);

  // Detail / edit.
  const [active, setActive] = useState<SecurityIncident | null>(null);
  const [eStatus, setEStatus] = useState('');
  const [eIc, setEIc] = useState('');
  const [eCust, setECust] = useState(false);
  const [eReg, setEReg] = useState(false);
  const [saving, setSaving] = useState(false);

  // Resolve.
  const [resTarget, setResTarget] = useState<SecurityIncident | null>(null);
  const [rootCause, setRootCause] = useState('');
  const [pirKey, setPirKey] = useState('');
  const [resBusy, setResBusy] = useState(false);
  const [resError, setResError] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.securityOps.incidents
      .list(tenantId ?? undefined)
      .then(setItems)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.securityOps.incidents.create(
        { severity, category, title: title.trim(), ic: ic.trim() || null, affected_tenants: csv(affected) },
        tenantId ?? undefined,
      );
      toast.success('已创建安全事件');
      setTitle(''); setIc(''); setAffected(''); setCreating(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '创建失败');
    } finally {
      setBusy(false);
    }
  };

  const openDetail = (inc: SecurityIncident) => {
    setActive(inc);
    setEStatus(inc.status ?? '');
    setEIc(inc.ic ?? '');
    setECust(!!inc.customers_notified);
    setEReg(!!inc.regulators_notified);
  };

  const handleSave = async () => {
    if (!active?.id) return;
    setSaving(true);
    try {
      await api.admin.securityOps.incidents.update(
        active.id,
        { status: eStatus.trim(), ic: eIc.trim(), customers_notified: eCust, regulators_notified: eReg },
        tenantId ?? undefined,
      );
      toast.success('已更新事件');
      setActive(null);
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : '更新失败');
    } finally {
      setSaving(false);
    }
  };

  const handleResolve = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!resTarget?.id) return;
    if (rootCause.trim().length < 20) { setResError('根因描述至少需 20 个字符'); return; }
    if (!pirKey.trim()) { setResError('请填写 PIR S3 Key'); return; }
    setResBusy(true);
    setResError(null);
    try {
      await api.admin.securityOps.incidents.resolve(
        resTarget.id,
        { root_cause: rootCause.trim(), pir_s3_key: pirKey.trim() },
        tenantId ?? undefined,
      );
      toast.success('事件已闭环');
      setResTarget(null); setRootCause(''); setPirKey('');
      refresh();
    } catch (err) {
      setResError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setResBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">安全事件</div>
        {!creating && <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 新建事件</button>}
      </div>

      {creating && (
        <Dialog title="新建安全事件" onClose={() => setCreating(false)} dismissable={!busy} className="w-full max-w-2xl">
          <form onSubmit={handleCreate} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">级别</label>
                <CustomSelect
                  options={SEVERITIES.map((s) => ({ value: s, label: s }))}
                  value={severity}
                  onChange={(v) => setSeverity(v)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">类别</label>
                <CustomSelect
                  options={INCIDENT_CATEGORIES.map((c) => ({ value: c, label: c }))}
                  value={category}
                  onChange={(v) => setCategory(v)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">标题</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="生产数据库异常访问" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">事件指挥 (IC, 可选)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={ic} onChange={(e) => setIc(e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">受影响租户 (逗号分隔)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={affected} onChange={(e) => setAffected(e.target.value)} />
              </div>
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setCreating(false)} disabled={busy} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50">取消</button>
              <button type="submit" disabled={busy || !title.trim()} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '创建中…' : '创建'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>}
      {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无安全事件</div>}
      {!loading && !todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">级别</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类别</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">标题</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">发现时间</th>
                <th className="px-3 py-2 w-40"></th>
              </tr>
            </thead>
            <tbody>
              {items.map((inc, i) => (
                <tr key={inc.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className={`px-3 py-2 font-mono text-[11px] ${severityClass(inc.severity)}`}>{inc.severity ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{inc.category ?? '—'}</td>
                  <td className="px-3 py-2 text-[12px] text-ink truncate max-w-[220px]">{inc.title ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{inc.status ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(inc.detected_at)}</td>
                  <td className="px-3 py-2 flex items-center gap-2">
                    <button onClick={() => openDetail(inc)} className="text-[11px] font-mono text-ink-2 border border-line rounded-sm px-2 py-0.5 hover:bg-paper-2">详情</button>
                    <button onClick={() => { setResTarget(inc); setResError(null); }} className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50">闭环</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Detail / edit */}
      {active && (
        <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">事件详情 · {active.title}</div>
            <button onClick={() => setActive(null)} className="text-[11px] font-mono text-dim hover:text-ink">关闭</button>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">状态</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={eStatus} onChange={(e) => setEStatus(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">事件指挥 (IC)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={eIc} onChange={(e) => setEIc(e.target.value)} />
            </div>
          </div>
          <label className="flex items-center gap-2 text-[12px] text-ink-2">
            <input type="checkbox" checked={eCust} onChange={(e) => setECust(e.target.checked)} /> 已通知客户
          </label>
          <label className="flex items-center gap-2 text-[12px] text-ink-2">
            <input type="checkbox" checked={eReg} onChange={(e) => setEReg(e.target.checked)} /> 已通知监管
          </label>
          <div>
            <button onClick={handleSave} disabled={saving} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{saving ? '保存中…' : '保存'}</button>
          </div>
        </div>
      )}

      {/* Resolve */}
      {resTarget && (
        <form onSubmit={handleResolve} className="border border-green-200 bg-green-50/40 rounded-md p-4 flex flex-col gap-2 max-w-2xl">
          <div className="flex items-center justify-between">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">闭环事件 · {resTarget.title}</div>
            <button type="button" onClick={() => setResTarget(null)} className="text-[11px] font-mono text-dim hover:text-ink">取消</button>
          </div>
          {resError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{resError}</div>}
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">根因 (≥20 字符)</label>
            <textarea className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y min-h-[60px]" value={rootCause} onChange={(e) => setRootCause(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">PIR 报告 S3 Key</label>
            <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={pirKey} onChange={(e) => setPirKey(e.target.value)} placeholder="pir/2026/inc-123.md" />
          </div>
          <div>
            <button type="submit" disabled={resBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{resBusy ? '提交中…' : '确认闭环'}</button>
          </div>
        </form>
      )}
    </div>
  );
}

// ─── Scans ────────────────────────────────────────────────────────────────────

function ScansTab({ tenantId }: { tenantId: string | null }) {
  const [items, setItems] = useState<ScanRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.securityOps.scans
      .runs(tenantId ?? undefined)
      .then(setItems)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  if (loading) return <div className="font-mono text-[12px] text-dim">加载中…</div>;
  if (todo) return <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>;
  if (error) return <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>;
  if (items.length === 0) return <div className="font-mono text-[12px] text-dim italic">暂无扫描记录</div>;

  return (
    <div className="border border-line rounded-md overflow-hidden">
      <table className="w-full text-left border-collapse">
        <thead className="bg-paper-2 border-b border-line">
          <tr>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">工具</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">目标</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">开始时间</th>
            <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">发现数</th>
          </tr>
        </thead>
        <tbody>
          {items.map((s, i) => (
            <tr key={s.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
              <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.type ?? '—'}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-dim">{s.tool ?? '—'}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[220px]">{s.target ?? '—'}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.status ?? '—'}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(s.started_at)}</td>
              <td className="px-3 py-2 font-mono text-[11px] text-ink-2 tabular-nums">{fmtFindings(s.findings_count)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── SBOM watch ──────────────────────────────────────────────────────────────────

function SbomTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const [items, setItems] = useState<SbomWatch[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [creating, setCreating] = useState(false);
  const [imageRef, setImageRef] = useState('');
  const [notify, setNotify] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.securityOps.sbom
      .watchList(tenantId ?? undefined)
      .then(setItems)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!imageRef.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await api.admin.securityOps.sbom.register(
        { image_ref: imageRef.trim(), notify: csv(notify) },
        tenantId ?? undefined,
      );
      toast.success('已注册 SBOM 监控');
      setImageRef(''); setNotify(''); setCreating(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '注册失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">SBOM 监控</div>
        {!creating && <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 注册镜像</button>}
      </div>

      {creating && (
        <Dialog title="注册镜像" onClose={() => setCreating(false)} dismissable={!busy} className="w-full max-w-xl">
          <form onSubmit={handleCreate} className="flex flex-col gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">镜像引用</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={imageRef} onChange={(e) => setImageRef(e.target.value)} placeholder="registry.acme.co/app:1.2.3" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">通知对象 (逗号分隔)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={notify} onChange={(e) => setNotify(e.target.value)} placeholder="secops@acme.co" />
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setCreating(false)} disabled={busy} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50">取消</button>
              <button type="submit" disabled={busy || !imageRef.trim()} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '注册中…' : '注册'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>}
      {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无监控镜像</div>}
      {!loading && !todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">镜像</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">注册时间</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">上次扫描</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">新增 CVE</th>
              </tr>
            </thead>
            <tbody>
              {items.map((s, i) => (
                <tr key={s.image_ref ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[260px]">{s.image_ref ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(s.registered_at)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(s.last_scanned_at)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2 tabular-nums">{s.new_cves_count ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Drills ───────────────────────────────────────────────────────────────────

function DrillsTab({ tenantId }: { tenantId: string | null }) {
  const toast = useToast();
  const [items, setItems] = useState<SecurityDrill[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [creating, setCreating] = useState(false);
  const [kind, setKind] = useState<string>('TABLETOP');
  const [scheduledAt, setScheduledAt] = useState('');
  const [participants, setParticipants] = useState('');
  const [runbook, setRunbook] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.securityOps.drills
      .list(tenantId ?? undefined)
      .then(setItems)
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => setLoading(false));
  }, [tenantId]);

  useEffect(() => { load(); }, [load, reloadKey]);
  const refresh = () => setReloadKey((k) => k + 1);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!scheduledAt.trim()) { setError('请填写计划时间'); return; }
    setBusy(true);
    setError(null);
    try {
      await api.admin.securityOps.drills.create(
        {
          kind,
          scheduled_at: scheduledAt.trim(),
          participants: csv(participants),
          scenario_runbook: runbook.trim() || null,
        },
        tenantId ?? undefined,
      );
      toast.success('已安排演练');
      setScheduledAt(''); setParticipants(''); setRunbook(''); setCreating(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '安排失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">演练</div>
        {!creating && <button onClick={() => setCreating(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ 安排演练</button>}
      </div>

      {creating && (
        <Dialog title="安排演练" onClose={() => setCreating(false)} dismissable={!busy} className="w-full max-w-2xl">
          <form onSubmit={handleCreate} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">类型</label>
                <CustomSelect
                  options={DRILL_KINDS.map((k) => ({ value: k, label: k }))}
                  value={kind}
                  onChange={(v) => setKind(v)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">计划时间 (ISO)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={scheduledAt} onChange={(e) => setScheduledAt(e.target.value)} placeholder="2026-07-01T10:00:00Z" />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">参与者 (逗号分隔)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={participants} onChange={(e) => setParticipants(e.target.value)} placeholder="alice, bob" />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">场景 Runbook (可选)</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={runbook} onChange={(e) => setRunbook(e.target.value)} />
            </div>
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setCreating(false)} disabled={busy} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50">取消</button>
              <button type="submit" disabled={busy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{busy ? '提交中…' : '安排'}</button>
            </div>
          </form>
        </Dialog>
      )}

      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">该能力暂未启用：安全运营端点不可达。</div>}
      {!loading && !todo && items.length === 0 && <div className="font-mono text-[12px] text-dim italic">暂无演练计划</div>}
      {!loading && !todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">计划时间</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">参与者</th>
              </tr>
            </thead>
            <tbody>
              {items.map((d, i) => (
                <tr key={d.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{d.kind ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(d.scheduled_at)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{d.status ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[200px]">{d.participants?.join(', ') || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function SecurityOpsPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const [tab, setTab] = useState<Tab>('sla');

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const tabs: { id: Tab; label: string }[] = [
    { id: 'sla', label: 'SLA 看板' },
    { id: 'vulns', label: '漏洞列表' },
    { id: 'incidents', label: '安全事件' },
    { id: 'scans', label: '扫描记录' },
    { id: 'sbom', label: 'SBOM 监控' },
    { id: 'drills', label: '演练' },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">安全运营</h1>
        <p className="text-[12px] text-dim mt-0.5">漏洞 SLA、安全事件、扫描、SBOM 与演练（→ security-ops）</p>
      </div>

      {/* Tenant selector (optional — platform-level service) */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <CustomSelect
          options={[{ value: '', label: '（全平台 / 不限租户）' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-line-2 flex-wrap">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-3 py-1.5 text-[12px] font-mono border-b-2 -mb-px transition-colors ${
              tab === t.id ? 'border-accent text-ink' : 'border-transparent text-dim hover:text-ink'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'sla' && <SlaTab tenantId={selectedTenantId} />}
      {tab === 'vulns' && <VulnsTab tenantId={selectedTenantId} />}
      {tab === 'incidents' && <IncidentsTab tenantId={selectedTenantId} />}
      {tab === 'scans' && <ScansTab tenantId={selectedTenantId} />}
      {tab === 'sbom' && <SbomTab tenantId={selectedTenantId} />}
      {tab === 'drills' && <DrillsTab tenantId={selectedTenantId} />}
    </div>
  );
}
