import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { usePrompt, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect, GroupedSelect, ComboBox, MultiChipSelect } from '../components/CustomSelect';
import type { ComplianceReport, DsarRecord, RopaRecord } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

// Lawful bases for processing personal data: the six bases of GDPR Article 6(1).
const LEGAL_BASIS_GROUPS = [
  {
    label: 'GDPR Article 6',
    options: [
      { value: 'CONSENT', label: 'CONSENT — Consent' },
      { value: 'CONTRACT', label: 'CONTRACT — Necessary for a contract' },
      { value: 'LEGAL_OBLIGATION', label: 'LEGAL_OBLIGATION — Legal obligation' },
      { value: 'VITAL_INTEREST', label: 'VITAL_INTEREST — Vital interests' },
      { value: 'PUBLIC_TASK', label: 'PUBLIC_TASK — Public task' },
      { value: 'LEGITIMATE_INTEREST', label: 'LEGITIMATE_INTEREST — Legitimate interests' },
    ],
  },
];

function csv(v: string): string[] {
  return v.split(',').map((x) => x.trim()).filter(Boolean);
}

// Report types: the backend doesn't validate the enum (passed through to the
// upstream foundation-compliance service), so these are presets and custom
// values are still allowed.
const REPORT_TYPE_OPTIONS = [
  { value: 'annual', label: 'annual — Annual report' },
  { value: 'quarterly', label: 'quarterly — Quarterly report' },
  { value: 'dpia', label: 'dpia — Data protection impact assessment' },
];

// Compliance modes: fixed values matching the lowercase values stored in the list.
const COMPLIANCE_MODE_OPTIONS = ['gdpr'];

// Data categories: not validated by the backend (passed through), so presets
// with custom values allowed. Shared by ROPA (single) and DPIA (multi).
const DATA_CATEGORY_OPTIONS = [
  { value: 'pii', label: 'pii — Personally identifiable information' },
  { value: 'contact', label: 'contact — Contact details' },
  { value: 'location', label: 'location — Location data' },
  { value: 'financial', label: 'financial — Financial data' },
  { value: 'health', label: 'health — Health data' },
  { value: 'biometric', label: 'biometric — Biometric data' },
  { value: 'behavioral', label: 'behavioral — Behavioral / usage data' },
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
  const [rLegalBasis, setRLegalBasis] = useState('CONSENT');
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
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : 'Failed to load'); })
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
      toast.success('ROPA record created');
      setRService(''); setRDataCategory(''); setRPurpose(''); setRSubjects(''); setRRecipients(''); setRopaOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create');
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
      toast.success('Compliance report generated');
      setRepType(''); setRepModes([]); setRepOpen(false);
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to generate');
    } finally {
      setRepBusy(false);
    }
  };

  const handleCreateDsar = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dsarUser.trim()) { setDsarError('Enter a user ID'); return; }
    setDsarBusy(true);
    setDsarError(null);
    setDsarResult(null);
    try {
      const res = await api.admin.compliance.dsar.create(
        { user_id: dsarUser.trim(), request_type: dsarType, details: dsarDetails.trim() },
        tid,
      );
      setDsarResult(res);
      toast.success('DSAR created');
      setDsarOpen(false);
    } catch (err) {
      setDsarError(err instanceof ApiError ? err.message : 'Failed to create');
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
      setDsarError(err instanceof ApiError ? err.message : 'Lookup failed');
    }
  };

  const handleCreateDpia = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!dpiaProject.trim()) { setDpiaError('Enter a project name'); return; }
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
      toast.success('DPIA created');
      // Offer to complete it immediately.
      const conclusion = await prompt({
        title: 'Complete DPIA (optional)',
        label: 'Conclusion',
        description: 'Enter a conclusion to complete this DPIA now, or leave it blank to finish later.',
      });
      if (conclusion !== null && conclusion.trim() && created.id) {
        const residual = await prompt({ title: 'Complete DPIA', label: 'Residual risks', defaultValue: 'None' });
        await api.admin.compliance.dpia.complete(
          created.id,
          { conclusion: conclusion.trim(), residual_risks: (residual ?? '').trim() || 'None' },
          tid,
        );
        toast.success('DPIA completed');
      }
      setDpiaProject(''); setDpiaDesc(''); setDpiaCategories([]); setDpiaOpen(false);
    } catch (err) {
      setDpiaError(err instanceof ApiError ? err.message : 'Failed to create');
    } finally {
      setDpiaBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Compliance center</h1>
        <p className="text-[12px] text-dim mt-0.5">ROPA, compliance reports, DSARs and DPIAs (→ compliance)</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Tenant</label>
        <CustomSelect
          options={[{ value: '', label: '(Default / current tenant)' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {error && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">Not available yet: the compliance endpoint is unreachable. Data will appear here once it’s connected.</div>}

      {/* ROPA */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">ROPA (records of processing activities)</div>
          {!ropaOpen && <button onClick={() => setRopaOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ New</button>}
        </div>
        {ropaOpen && (
          <Dialog title="New ROPA" onClose={() => setRopaOpen(false)} dismissable={!rBusy} className="w-full max-w-2xl">
          <form onSubmit={handleCreateRopa} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Service</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rService} onChange={(e) => setRService(e.target.value)} placeholder="billing" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Data category</label>
                <ComboBox options={DATA_CATEGORY_OPTIONS} value={rDataCategory} onChange={setRDataCategory} placeholder="Select or type…" />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Purpose of processing</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rPurpose} onChange={(e) => setRPurpose(e.target.value)} placeholder="invoicing" />
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Legal basis</label>
                <GroupedSelect groups={LEGAL_BASIS_GROUPS} value={rLegalBasis} onChange={setRLegalBasis} />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Data subjects (comma-separated)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rSubjects} onChange={(e) => setRSubjects(e.target.value)} placeholder="customers" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Recipients (comma-separated)</label>
                <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={rRecipients} onChange={(e) => setRRecipients(e.target.value)} placeholder="processor-x" />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={rBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{rBusy ? 'Creating…' : 'Create'}</button>
              <button type="button" onClick={() => setRopaOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
            </div>
          </form>
          </Dialog>
        )}
        {!loading && !todo && ropa.length === 0 && <div className="font-mono text-[12px] text-dim italic">No ROPA records</div>}
        {ropa.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Service</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Purpose</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Legal basis</th>
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
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Compliance reports</div>
          {!repOpen && <button onClick={() => setRepOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ Generate report</button>}
        </div>
        {repOpen && (
          <Dialog title="Generate compliance report" onClose={() => setRepOpen(false)} dismissable={!repBusy} className="w-full max-w-xl">
          <form onSubmit={handleGenerateReport} className="flex flex-col gap-2">
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Report type</label>
                <ComboBox options={REPORT_TYPE_OPTIONS} value={repType} onChange={setRepType} placeholder="Select or type…" />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Compliance mode</label>
                <MultiChipSelect options={COMPLIANCE_MODE_OPTIONS} value={repModes} onChange={setRepModes} placeholder="Select compliance modes…" />
              </div>
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={repBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{repBusy ? 'Generating…' : 'Generate'}</button>
              <button type="button" onClick={() => setRepOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
            </div>
          </form>
          </Dialog>
        )}
        {!loading && !todo && reports.length === 0 && <div className="font-mono text-[12px] text-dim italic">No compliance reports</div>}
        {reports.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Type</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Compliance mode</th>
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
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">DSAR (data subject requests)</div>
          {!dsarOpen && <button onClick={() => setDsarOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ New DSAR</button>}
        </div>
        {dsarOpen && (
          <Dialog title="New DSAR" onClose={() => setDsarOpen(false)} dismissable={!dsarBusy} className="w-full max-w-md">
          <form onSubmit={handleCreateDsar} className="flex flex-col gap-2">
            {dsarError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{dsarError}</div>}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">User ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={dsarUser} onChange={(e) => setDsarUser(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Request type</label>
              <CustomSelect
                options={[
                  { value: 'access', label: 'access — Right of access' },
                  { value: 'erasure', label: 'erasure — Right to erasure' },
                  { value: 'rectification', label: 'rectification — Right to rectification' },
                  { value: 'portability', label: 'portability — Data portability' },
                ]}
                value={dsarType}
                onChange={(v) => setDsarType(v)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Details</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dsarDetails} onChange={(e) => setDsarDetails(e.target.value)} />
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={dsarBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{dsarBusy ? 'Submitting…' : 'Create'}</button>
              <button type="button" onClick={() => setDsarOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
            </div>
          </form>
          </Dialog>
        )}
        {dsarResult && (
          <div className="border border-line rounded-md p-3 bg-surface text-[12px] text-ink-2 font-mono">
            DSAR created: {dsarResult.id} · status {dsarResult.status ?? '—'}
          </div>
        )}
        <div className="flex flex-col md:flex-row gap-3">
          <form onSubmit={handleQueryDsar} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 flex-1">
            <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">Look up by ID</div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">DSAR ID</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={dsarQueryId} onChange={(e) => setDsarQueryId(e.target.value)} />
            </div>
            <div>
              <button type="submit" className="px-2.5 py-1 text-[11px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2">Look up</button>
            </div>
            {dsarQueryResult && (
              <div className="border-t border-line-2 pt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
                <span className="font-mono text-dim">User</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.user_id ?? '—'}</span>
                <span className="font-mono text-dim">Type</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.request_type ?? '—'}</span>
                <span className="font-mono text-dim">Status</span>
                <span className="font-mono text-ink-2">{dsarQueryResult.status ?? '—'}</span>
              </div>
            )}
          </form>
        </div>
      </section>

      {/* DPIA */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">DPIA (data protection impact assessment)</div>
          {!dpiaOpen && <button onClick={() => setDpiaOpen(true)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">+ New DPIA</button>}
        </div>
        {dpiaOpen && (
          <Dialog title="New DPIA" onClose={() => setDpiaOpen(false)} dismissable={!dpiaBusy} className="w-full max-w-2xl">
          <form onSubmit={handleCreateDpia} className="flex flex-col gap-2">
            {dpiaError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{dpiaError}</div>}
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Project name</label>
              <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dpiaProject} onChange={(e) => setDpiaProject(e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Data category</label>
              <MultiChipSelect options={DATA_CATEGORY_VALUES} value={dpiaCategories} onChange={setDpiaCategories} placeholder="Select data categories…" />
            </div>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">Description</label>
            <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={dpiaDesc} onChange={(e) => setDpiaDesc(e.target.value)} />
          </div>
          <div className="flex gap-2">
            <button type="submit" disabled={dpiaBusy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">{dpiaBusy ? 'Creating…' : 'Create DPIA'}</button>
            <button type="button" onClick={() => setDpiaOpen(false)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
          </div>
        </form>
          </Dialog>
        )}
      </section>
    </div>
  );
}
