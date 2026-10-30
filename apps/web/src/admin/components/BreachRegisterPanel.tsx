import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { BreachRecord, BreachRecordInput, BreachRiskLevel, BreachStatus } from '../../api/types';
import { formatDateTimeAuto } from '../../i18n/dates';
import { Dialog } from './Dialog';
import { useToast } from './dialogs';

/**
 * Personal-data breach register (GDPR Art. 33(5): document every breach,
 * notified or not). Each row shows the Art. 33(1) clock — notify the
 * supervisory authority within 72 hours of becoming aware — live, and the
 * form asks for the reasons when a due notification was late or never made
 * (the BFF enforces the same rule).
 */

const NOTIFY_WITHIN_MS = 72 * 60 * 60 * 1000;
const TICK_MS = 60 * 1000;

const STATUS_OPTIONS: Array<{ value: BreachStatus; label: string }> = [
  { value: 'open', label: 'Open' },
  { value: 'contained', label: 'Contained' },
  { value: 'closed', label: 'Closed' },
];

const RISK_OPTIONS: Array<{ value: BreachRiskLevel; label: string }> = [
  { value: 'low', label: 'Low — unlikely to result in a risk (no notification due)' },
  { value: 'medium', label: 'Medium — notify the supervisory authority' },
  { value: 'high', label: 'High — notify the authority and the individuals (Art. 34)' },
];

const RISK_SHORT: Record<BreachRiskLevel, string> = { low: 'Low', medium: 'Medium', high: 'High' };

const inputCls =
  'border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent';
const labelCls = 'text-[11px] text-dim font-mono';

type DelayInputs = Pick<BreachRecordInput, 'aware_at' | 'authority_notified_at' | 'status' | 'risk_level'>;

/** Mirrors the BFF rule: reasons are needed for a notification made more than
 *  72 hours after awareness, or a breach closed without one when it was due. */
function delayReasonRequired(r: DelayInputs): boolean {
  if (r.authority_notified_at) {
    return Date.parse(r.authority_notified_at) - Date.parse(r.aware_at) > NOTIFY_WITHIN_MS;
  }
  return r.status === 'closed' && r.risk_level !== 'low';
}

function formatDuration(ms: number): string {
  const minutes = Math.max(0, Math.round(ms / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} h`;
  return `${Math.floor(hours / 24)} d ${hours % 24} h`;
}

/** ISO timestamp → the value a `datetime-local` input shows (local time). */
function toLocalInput(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fromLocalInput(value: string): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function csv(v: string): string[] {
  return v.split(',').map((x) => x.trim()).filter(Boolean);
}

function AuthorityBadge({ breach, now }: { breach: BreachRecord; now: number }) {
  if (breach.authority_notified_at) {
    return <span className="text-ok">Notified {formatDateTimeAuto(breach.authority_notified_at)}</span>;
  }
  if (breach.risk_level === 'low') {
    return <span className="text-dim">Not required — low risk</span>;
  }
  if (breach.status === 'closed') {
    return <span className="text-warn">Closed without notification</span>;
  }
  const left = Date.parse(breach.notify_authority_by) - now;
  if (left <= 0) {
    return (
      <span className="text-accent font-medium">Overdue by {formatDuration(-left)} — notify now</span>
    );
  }
  return (
    <span className="text-warn">
      Notify by {formatDateTimeAuto(breach.notify_authority_by)} · {formatDuration(left)} left
    </span>
  );
}

interface FormState {
  title: string;
  description: string;
  occurredAt: string;
  awareAt: string;
  status: BreachStatus;
  risk: BreachRiskLevel;
  categories: string;
  subjects: string;
  records: string;
  consequences: string;
  measures: string;
  authorityAt: string;
  authorityRef: string;
  subjectsAt: string;
  delayReason: string;
  incidentId: string;
}

function blankForm(): FormState {
  return {
    title: '', description: '', occurredAt: '', awareAt: toLocalInput(new Date().toISOString()),
    status: 'open', risk: 'medium', categories: '', subjects: '', records: '',
    consequences: '', measures: '', authorityAt: '', authorityRef: '', subjectsAt: '',
    delayReason: '', incidentId: '',
  };
}

function formOf(b: BreachRecord): FormState {
  return {
    title: b.title,
    description: b.description,
    occurredAt: toLocalInput(b.occurred_at),
    awareAt: toLocalInput(b.aware_at),
    status: b.status,
    risk: b.risk_level,
    categories: b.data_categories.join(', '),
    subjects: b.approx_subjects == null ? '' : String(b.approx_subjects),
    records: b.approx_records == null ? '' : String(b.approx_records),
    consequences: b.likely_consequences,
    measures: b.measures_taken,
    authorityAt: toLocalInput(b.authority_notified_at),
    authorityRef: b.authority_reference ?? '',
    subjectsAt: toLocalInput(b.subjects_notified_at),
    delayReason: b.delay_reason ?? '',
    incidentId: b.security_incident_id ?? '',
  };
}

/** A whole number ≥ 0, null for blank, undefined when invalid. */
function countOf(v: string): number | null | undefined {
  if (!v.trim()) return null;
  const n = Number(v.trim());
  return Number.isInteger(n) && n >= 0 ? n : undefined;
}

function bodyOf(f: FormState): BreachRecordInput {
  return {
    title: f.title.trim(),
    description: f.description.trim(),
    occurred_at: fromLocalInput(f.occurredAt),
    aware_at: fromLocalInput(f.awareAt) ?? '',
    status: f.status,
    risk_level: f.risk,
    data_categories: csv(f.categories),
    approx_subjects: countOf(f.subjects) ?? null,
    approx_records: countOf(f.records) ?? null,
    likely_consequences: f.consequences.trim(),
    measures_taken: f.measures.trim(),
    authority_notified_at: fromLocalInput(f.authorityAt),
    authority_reference: f.authorityRef.trim() || null,
    subjects_notified_at: fromLocalInput(f.subjectsAt),
    delay_reason: f.delayReason.trim() || null,
    security_incident_id: f.incidentId.trim() || null,
  };
}

type Editing = { mode: 'create' } | { mode: 'edit'; breach: BreachRecord; initial: FormState };

export function BreachRegisterPanel({ tenantId }: { tenantId?: string }) {
  const toast = useToast();
  const [items, setItems] = useState<BreachRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [editing, setEditing] = useState<Editing | null>(null);
  const [form, setForm] = useState<FormState>(blankForm);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setItems(await api.admin.compliance.breaches.list(tenantId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to load the breach register');
    } finally {
      setLoading(false);
    }
  }, [tenantId]);

  useEffect(() => { void load(); }, [load]);

  // Keep the 72-hour countdowns current.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const openCreate = () => {
    setForm(blankForm());
    setFormError(null);
    setEditing({ mode: 'create' });
  };

  const openEdit = (breach: BreachRecord) => {
    const initial = formOf(breach);
    setForm(initial);
    setFormError(null);
    setEditing({ mode: 'edit', breach, initial });
  };

  const draft = bodyOf(form);
  const needsDelayReason = Boolean(draft.aware_at) && delayReasonRequired(draft);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!editing) return;
    if (!draft.title) { setFormError('Enter a title'); return; }
    if (!draft.aware_at) { setFormError('Enter when you became aware of the breach'); return; }
    if (countOf(form.subjects) === undefined || countOf(form.records) === undefined) {
      setFormError('Approximate counts must be whole numbers (0 or more)');
      return;
    }
    if (needsDelayReason && !draft.delay_reason) {
      setFormError('Record the reasons for the late or missing notification (GDPR Art. 33(1))');
      return;
    }
    setBusy(true);
    setFormError(null);
    try {
      if (editing.mode === 'create') {
        await api.admin.compliance.breaches.create(draft, tenantId);
        toast.success('Breach recorded');
      } else {
        // Send only what changed, so untouched timestamps keep their seconds.
        const before = bodyOf(editing.initial);
        const changes: Partial<BreachRecordInput> = {};
        for (const key of Object.keys(draft) as Array<keyof BreachRecordInput>) {
          if (JSON.stringify(draft[key]) !== JSON.stringify(before[key])) {
            (changes as Record<string, unknown>)[key] = draft[key];
          }
        }
        if (Object.keys(changes).length > 0) {
          await api.admin.compliance.breaches.update(editing.breach.id, changes, tenantId);
          toast.success('Breach record updated');
        }
      }
      setEditing(null);
      void load();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : 'Failed to save');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="flex flex-col gap-2" aria-label="Personal data breaches">
      <div className="flex items-center justify-between">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Personal data breaches</div>
        {!editing && (
          <button onClick={openCreate} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2">
            + Record breach
          </button>
        )}
      </div>
      <p className="text-[11px] text-dim">
        Record every breach of personal data, whether or not it is notified (GDPR Art. 33(5)). Notify the
        supervisory authority within 72 hours of becoming aware, unless the breach is unlikely to result in a
        risk to individuals; tell the individuals too when the risk is high (Art. 34).
      </p>

      {error && <div role="alert" className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {!loading && !error && items.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">No breaches recorded</div>
      )}
      {items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Breach</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Aware since</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Risk</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Authority</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {items.map((b) => (
                <tr key={b.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors align-top">
                  <td className="px-3 py-2 text-[13px] text-ink">
                    {b.title}
                    {b.risk_level === 'high' && !b.subjects_notified_at && b.status !== 'closed' && (
                      <div className="text-[11px] text-warn">Tell the affected individuals (Art. 34)</div>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{formatDateTimeAuto(b.aware_at)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{RISK_SHORT[b.risk_level]}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2 capitalize">{b.status}</td>
                  <td className="px-3 py-2 text-[12px]"><AuthorityBadge breach={b} now={now} /></td>
                  <td className="px-3 py-2 text-right">
                    <button
                      onClick={() => openEdit(b)}
                      aria-label={`Edit ${b.title}`}
                      className="px-2 py-0.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
                    >
                      Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <Dialog
          title={editing.mode === 'create' ? 'Record personal data breach' : 'Edit breach record'}
          onClose={() => setEditing(null)}
          dismissable={!busy}
          className="w-full max-w-2xl max-h-[90vh] overflow-y-auto"
        >
          <form onSubmit={handleSubmit} className="flex flex-col gap-2" noValidate>
            {formError && <div role="alert" className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{formError}</div>}
            <div className="flex flex-col gap-1">
              <label htmlFor="breach-title" className={labelCls}>Title</label>
              <input id="breach-title" className={inputCls} value={form.title} onChange={(e) => set('title', e.target.value)} placeholder="Payroll file emailed to the wrong recipient" />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-aware" className={labelCls}>Became aware at</label>
                <input id="breach-aware" type="datetime-local" className={inputCls} value={form.awareAt} onChange={(e) => set('awareAt', e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-occurred" className={labelCls}>Occurred at (if known)</label>
                <input id="breach-occurred" type="datetime-local" className={inputCls} value={form.occurredAt} onChange={(e) => set('occurredAt', e.target.value)} />
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-risk" className={labelCls}>Risk to individuals</label>
                <select id="breach-risk" className={inputCls} value={form.risk} onChange={(e) => set('risk', e.target.value as BreachRiskLevel)}>
                  {RISK_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-status" className={labelCls}>Status</label>
                <select id="breach-status" className={inputCls} value={form.status} onChange={(e) => set('status', e.target.value as BreachStatus)}>
                  {STATUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </div>
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-categories" className={labelCls}>Data categories (comma-separated)</label>
                <input id="breach-categories" className={inputCls} value={form.categories} onChange={(e) => set('categories', e.target.value)} placeholder="contact, financial" />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-subjects" className={labelCls}>Individuals affected (approx.)</label>
                <input id="breach-subjects" type="number" min={0} step={1} inputMode="numeric" className={`${inputCls} font-mono`} value={form.subjects} onChange={(e) => set('subjects', e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-records" className={labelCls}>Records affected (approx.)</label>
                <input id="breach-records" type="number" min={0} step={1} inputMode="numeric" className={`${inputCls} font-mono`} value={form.records} onChange={(e) => set('records', e.target.value)} />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="breach-description" className={labelCls}>What happened</label>
              <textarea id="breach-description" rows={2} className={inputCls} value={form.description} onChange={(e) => set('description', e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="breach-consequences" className={labelCls}>Likely consequences for individuals</label>
              <textarea id="breach-consequences" rows={2} className={inputCls} value={form.consequences} onChange={(e) => set('consequences', e.target.value)} />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="breach-measures" className={labelCls}>Measures taken or proposed</label>
              <textarea id="breach-measures" rows={2} className={inputCls} value={form.measures} onChange={(e) => set('measures', e.target.value)} />
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-authority-at" className={labelCls}>Authority notified at</label>
                <input id="breach-authority-at" type="datetime-local" className={inputCls} value={form.authorityAt} onChange={(e) => set('authorityAt', e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-authority-ref" className={labelCls}>Authority reference</label>
                <input id="breach-authority-ref" className={`${inputCls} font-mono`} value={form.authorityRef} onChange={(e) => set('authorityRef', e.target.value)} />
              </div>
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-subjects-at" className={labelCls}>Individuals notified at</label>
                <input id="breach-subjects-at" type="datetime-local" className={inputCls} value={form.subjectsAt} onChange={(e) => set('subjectsAt', e.target.value)} />
              </div>
            </div>
            {(needsDelayReason || form.delayReason) && (
              <div className="flex flex-col gap-1">
                <label htmlFor="breach-delay" className={labelCls}>
                  Reasons for the late or missing notification{needsDelayReason ? ' (required)' : ''}
                </label>
                <textarea id="breach-delay" rows={2} className={inputCls} value={form.delayReason} onChange={(e) => set('delayReason', e.target.value)} />
              </div>
            )}
            <div className="flex flex-col gap-1">
              <label htmlFor="breach-incident" className={labelCls}>Security incident ID (optional)</label>
              <input id="breach-incident" className={`${inputCls} font-mono`} value={form.incidentId} onChange={(e) => set('incidentId', e.target.value)} placeholder="inc_…" />
            </div>
            <div className="flex gap-2">
              <button type="submit" disabled={busy} className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50">
                {busy ? 'Saving…' : editing.mode === 'create' ? 'Record breach' : 'Save changes'}
              </button>
              <button type="button" onClick={() => setEditing(null)} className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink">Cancel</button>
            </div>
          </form>
        </Dialog>
      )}
    </section>
  );
}
