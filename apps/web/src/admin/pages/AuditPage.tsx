import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { CustomSelect } from '../components/CustomSelect';
import type { AuditEvent, AuditChainVerifyResult } from '../../api/types';
import { AUDIT_ACTIONS, AUDIT_ACTION_GROUPS } from '../auditActions';
import { useTranslation } from '../../i18n';
import { formatDateTime } from '../../i18n/dates';

// ─── Action multi-select popover ───────────────────────────────────────────────

function ActionMultiSelect({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onClickOutside);
    return () => document.removeEventListener('mousedown', onClickOutside);
  }, [open]);

  // Reset and focus the search box each time the popover opens.
  useEffect(() => {
    if (open) {
      setQuery('');
      searchRef.current?.focus();
    }
  }, [open]);

  const toggle = (value: string) => {
    onChange(
      selected.includes(value)
        ? selected.filter((v) => v !== value)
        : [...selected, value],
    );
  };

  const selectedOptions = AUDIT_ACTIONS.filter((a) => selected.includes(a.value));

  // Match against translated label and the wire value (case-insensitive).
  const q = query.trim().toLowerCase();
  const matches = (a: (typeof AUDIT_ACTIONS)[number]) =>
    q === '' || t(a.labelKey).toLowerCase().includes(q) || a.value.toLowerCase().includes(q);
  const visibleGroups = AUDIT_ACTION_GROUPS.filter((groupKey) =>
    AUDIT_ACTIONS.some((a) => a.groupKey === groupKey && matches(a)),
  );

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between gap-2 border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper text-left hover:border-accent focus:outline-none focus:border-accent"
      >
        <span className="flex-1 min-w-0 truncate">
          {selected.length === 0 ? (
            <span className="text-dim">All actions</span>
          ) : selected.length <= 2 ? (
            <span className="flex items-center gap-1 flex-wrap">
              {selectedOptions.map((o) => (
                <span
                  key={o.value}
                  className="inline-flex items-center bg-paper-2 border border-line rounded-sm px-1.5 py-0.5 text-[11px] text-ink-2"
                >
                  {t(o.labelKey)}
                </span>
              ))}
            </span>
          ) : (
            <span className="text-ink-2">{selected.length} selected</span>
          )}
        </span>
        <span className="font-mono text-[10px] text-dim shrink-0">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className="absolute z-30 mt-1 w-full max-h-80 overflow-auto border border-line rounded-sm bg-surface shadow-lg p-1">
          <div className="sticky top-0 z-10 bg-surface pb-1">
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search action name or ID…"
              className="w-full border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent"
            />
          </div>
          {selected.length > 0 && (
            <button
              type="button"
              onClick={() => onChange([])}
              className="w-full text-left px-2 py-1 text-[11px] font-mono text-dim hover:text-ink hover:bg-paper-2 rounded-sm"
            >
              Clear selection ({selected.length})
            </button>
          )}
          {visibleGroups.length === 0 && (
            <div className="px-2 py-3 text-[11.5px] text-dim italic">No matching actions</div>
          )}
          {visibleGroups.map((groupKey) => (
            <div key={groupKey} className="mt-1">
              <div className="px-2 py-1 font-mono text-[9.5px] uppercase tracking-[0.1em] text-dim">
                {t(groupKey)}
              </div>
              {AUDIT_ACTIONS.filter((a) => a.groupKey === groupKey && matches(a)).map((a) => {
                const checked = selected.includes(a.value);
                return (
                  <label
                    key={a.value}
                    className="flex items-start gap-2 px-2 py-1 rounded-sm hover:bg-paper-2 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(a.value)}
                      className="mt-0.5 accent-accent"
                    />
                    <span className="flex flex-col leading-tight">
                      <span className="text-[12px] text-ink">{t(a.labelKey)}</span>
                      <span className="font-mono text-[10px] text-dim">{a.value}</span>
                    </span>
                  </label>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Format a Date into the local `YYYY-MM-DDTHH:mm` string a datetime-local input wants.
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// [from, to] window ending now and reaching back the given number of days.
function rangeForDays(days: number): [string, string] {
  const now = new Date();
  const start = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  return [toLocalInput(start), toLocalInput(now)];
}

const QUICK_RANGES: { label: string; days: number }[] = [
  { label: 'Last 24 hours', days: 1 },
  { label: 'Last 7 days', days: 7 },
  { label: 'Last 30 days', days: 30 },
  { label: 'Last 90 days', days: 90 },
];

export function AuditPage() {
  const { lang } = useTranslation();
  const { tenants, tenantsLoading, setTenants, setTenantsLoading } = useAdminStore();
  const [tenantId, setTenantId] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  // Default the window to the past 7 days so the form is query-ready on open.
  const [defaultFrom, defaultTo] = rangeForDays(7);
  const [from, setFrom] = useState(defaultFrom);
  const [to, setTo] = useState(defaultTo);
  // Which quick-range button is active; null once the user edits a field manually.
  const [activeRange, setActiveRange] = useState<number | null>(7);
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

  // CSV export state.
  const [exporting, setExporting] = useState(false);

  const currentFilter = (): Record<string, unknown> => {
    const filter: Record<string, unknown> = {};
    if (actions.length) filter.event_types = actions;
    if (from) filter.occurred_from = from;
    if (to) filter.occurred_to = to;
    return filter;
  };

  const handleExportCsv = async () => {
    if (!tenantId) {
      setError('Select a tenant before exporting.');
      return;
    }
    setExporting(true);
    setError(null);
    try {
      const blob = await api.admin.audit.exportEvents(currentFilter(), {
        format: 'csv',
        tenantId,
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `audit-events-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Export failed');
    } finally {
      setExporting(false);
    }
  };

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
        setVerifyError(err instanceof ApiError ? err.message : 'Verification failed');
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
      setError('Select a tenant before querying audit events.');
      return;
    }
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    setQueried(true);
    try {
      const body: Record<string, unknown> = { limit: 50, tenant_id: tenantId };
      if (actions.length) body.event_types = actions;
      if (from) body.occurred_from = from;
      if (to) body.occurred_to = to;
      const data = await api.admin.audit.query(body);
      setEvents(data.items);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
        setIsTodoEndpoint(true);
        setEvents([]);
      } else {
        setError(err instanceof ApiError ? err.message : 'Query failed');
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Audit log</h1>
        <p className="text-[12px] text-dim mt-0.5">Search platform activity events</p>
      </div>

      {/* Query form */}
      <form onSubmit={handleQuery} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-3">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Filters</div>
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">Tenant</label>
            <CustomSelect
              options={tenants.map((t) => ({ value: t.id, label: t.name }))}
              value={tenantId}
              onChange={(v) => setTenantId(v)}
              placeholder={tenantsLoading ? 'Loading…' : 'Select a tenant'}
              disabled={tenantsLoading}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">Action</label>
            <ActionMultiSelect selected={actions} onChange={setActions} />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">Start time</label>
            <input
              type="datetime-local"
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={from}
              onChange={(e) => {
                setFrom(e.target.value);
                setActiveRange(null);
              }}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] font-mono text-dim">End time</label>
            <input
              type="datetime-local"
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={to}
              onChange={(e) => {
                setTo(e.target.value);
                setActiveRange(null);
              }}
            />
          </div>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[11px] font-mono text-dim mr-0.5">Quick range</span>
          {QUICK_RANGES.map((r) => (
            <button
              key={r.days}
              type="button"
              onClick={() => {
                const [nextFrom, nextTo] = rangeForDays(r.days);
                setFrom(nextFrom);
                setTo(nextTo);
                setActiveRange(r.days);
              }}
              className={[
                'px-2 py-0.5 text-[11px] font-mono rounded-sm border transition-colors',
                activeRange === r.days
                  ? 'border-accent bg-accent-soft text-accent'
                  : 'border-line bg-paper text-ink-2 hover:bg-paper-2',
              ].join(' ')}
            >
              {r.label}
            </button>
          ))}
          {(from || to) && (
            <button
              type="button"
              onClick={() => {
                setFrom('');
                setTo('');
                setActiveRange(null);
              }}
              className="px-2 py-0.5 text-[11px] font-mono rounded-sm border border-line bg-paper text-dim hover:text-ink hover:bg-paper-2"
            >
              Clear time
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="submit"
            disabled={loading || !tenantId}
            title={!tenantId ? 'Select a tenant first' : undefined}
            className="px-3 py-1.5 bg-ink text-paper text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? 'Searching…' : 'Search'}
          </button>
          <button
            type="button"
            onClick={handleVerifyChain}
            disabled={verifying}
            className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
          >
            {verifying ? 'Verifying…' : 'Verify chain integrity'}
          </button>
          <button
            type="button"
            onClick={handleExportCsv}
            disabled={exporting || !tenantId}
            title={!tenantId ? 'Select a tenant first' : undefined}
            className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
          >
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
          <span className="text-[11px] text-dim">
            Verify the selected tenant’s audit hash chain and Merkle signatures (defaults to the current admin’s tenant)
          </span>
        </div>
      </form>

      {/* Chain integrity verification result */}
      {verifyTodo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          This service isn’t enabled (the chain verification proxy endpoint is unreachable).
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
              {verifyResult.valid ? 'Hash chain intact' : 'Chain break detected'}
            </span>
          </div>
          <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11.5px]">
            {verifyResult.events_checked !== undefined && (
              <>
                <span className="font-mono text-dim">Events verified</span>
                <span className="font-mono text-ink-2">{verifyResult.events_checked.toLocaleString()}</span>
              </>
            )}
            {verifyResult.signatures_verified !== undefined && (
              <>
                <span className="font-mono text-dim">Signatures valid</span>
                <span className="font-mono text-ink-2">{verifyResult.signatures_verified.toLocaleString()}</span>
              </>
            )}
            {verifyResult.signatures_failed !== undefined && (
              <>
                <span className="font-mono text-dim">Signatures invalid</span>
                <span className={`font-mono ${verifyResult.signatures_failed > 0 ? 'text-red-700' : 'text-ink-2'}`}>
                  {verifyResult.signatures_failed.toLocaleString()}
                </span>
              </>
            )}
            {verifyResult.tsa_present !== undefined && (
              <>
                <span className="font-mono text-dim">With trusted timestamp</span>
                <span className="font-mono text-ink-2">{verifyResult.tsa_present.toLocaleString()}</span>
              </>
            )}
            {verifyResult.effective_tenant_id && (
              <>
                <span className="font-mono text-dim">Tenant verified</span>
                <span className="font-mono text-ink-2 truncate">{verifyResult.effective_tenant_id}</span>
              </>
            )}
            {!verifyResult.valid && verifyResult.first_break_event_id && (
              <>
                <span className="font-mono text-red-700">First broken event</span>
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
          The audit proxy endpoint isn’t ready yet. Results will appear here once it’s connected.
        </div>
      )}

      {!isTodoEndpoint && queried && !loading && (
        events.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic">No matching events</div>
        ) : (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Time</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Action</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Resource</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Actor</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Tenant</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.event_id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
                      {ev.occurred_at ? formatDateTime(new Date(ev.occurred_at), lang) : '—'}
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
