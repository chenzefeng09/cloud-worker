import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, ApiError } from '../../api/client';
import { formatDateTimeAuto, formatTimeAuto } from '../../i18n/dates';
import type {
  BucketPurpose,
  StorageQuotaSummaryItem,
  PromQueryRangeResponse,
} from '../../api/types';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';

// Foundation lifecycle ships four well-known bucket purposes.  Listed here
// so the filter dropdown stays in sync with the upstream enum even when
// the summary endpoint returns an empty set (dev / fresh tenants).
const PURPOSES: BucketPurpose[] = ['audit', 'workspace', 'kb_raw', 'kb_derived'];

const isTodo = (err: unknown) =>
  err instanceof ApiError &&
  (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

// Human-readable byte size.  Keeps two significant figures past the unit
// boundary so 1.2 GB does not collapse to "1 GB" in the table.
function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}

type SizeUnit = 'B' | 'KB' | 'MB' | 'GB' | 'TB';

const SIZE_UNITS: SizeUnit[] = ['B', 'KB', 'MB', 'GB', 'TB'];

const UNIT_FACTORS: Record<SizeUnit, number> = {
  B: 1,
  KB: 1024,
  MB: 1024 ** 2,
  GB: 1024 ** 3,
  TB: 1024 ** 4,
};

// Split a byte count into a {value, unit} pair using the same unit selection as
// `formatBytes`: the largest unit that keeps the value in [1, 1024), capped at
// TB. The numeric part is trimmed to at most two decimals with trailing zeros
// stripped (10 GB → "10", not "10.00"). A zero/invalid limit prefills as 0 GB.
function splitBytes(n: number): { value: string; unit: SizeUnit } {
  if (!Number.isFinite(n) || n <= 0) return { value: '0', unit: 'GB' };
  let v = n;
  let i = 0;
  while (v >= 1024 && i < SIZE_UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  return { value: parseFloat(v.toFixed(2)).toString(), unit: SIZE_UNITS[i] };
}

function tonePill(usedPct: number, softWarn: number): string {
  if (usedPct >= 100) return 'bg-red-50 text-red-700 border-red-200';
  if (usedPct >= softWarn)
    return 'bg-amber-50 text-amber-700 border-amber-200';
  return 'bg-green-50 text-green-700 border-green-200';
}

// ─── Edit dialog ────────────────────────────────────────────────────────────────

interface EditState {
  open: boolean;
  item: StorageQuotaSummaryItem | null;
  limitValue: string;
  limitUnit: SizeUnit;
  softWarnInput: string;
  busy: boolean;
  error: string | null;
}

const EMPTY_EDIT: EditState = {
  open: false,
  item: null,
  limitValue: '',
  limitUnit: 'GB',
  softWarnInput: '',
  busy: false,
  error: null,
};

export function StorageManagementPage() {
  const toast = useToast();
  const confirm = useConfirm();

  const [items, setItems] = useState<StorageQuotaSummaryItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [filterPurpose, setFilterPurpose] = useState<'' | BucketPurpose>('');

  const [edit, setEdit] = useState<EditState>(EMPTY_EDIT);

  // Trend chart controls.
  const [chartTenantId, setChartTenantId] = useState<string>('');
  const [chartPurpose, setChartPurpose] = useState<'' | BucketPurpose>('');
  const [chartRange, setChartRange] = useState<'1h' | '6h' | '24h' | '7d'>('24h');
  const [series, setSeries] = useState<PromQueryRangeResponse | null>(null);
  const [seriesLoading, setSeriesLoading] = useState(false);
  const [seriesErr, setSeriesErr] = useState<string | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.lifecycle.storageQuotas
      .summary()
      .then((res) => setItems(res.items ?? []))
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load, reloadKey]);

  const filteredItems = useMemo(
    () =>
      filterPurpose
        ? items.filter((it) => it.bucket_purpose === filterPurpose)
        : items,
    [items, filterPurpose],
  );

  const refresh = () => setReloadKey((k) => k + 1);

  const openEdit = (it: StorageQuotaSummaryItem) => {
    const { value, unit } = splitBytes(it.limit_bytes);
    setEdit({
      open: true,
      item: it,
      limitValue: value,
      limitUnit: unit,
      softWarnInput: String(it.soft_warn_pct),
      busy: false,
      error: null,
    });
  };

  const closeEdit = () => setEdit(EMPTY_EDIT);

  const submitEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!edit.item) return;
    const value = parseFloat(edit.limitValue);
    if (!Number.isFinite(value) || value < 0) {
      setEdit((s) => ({ ...s, error: 'Invalid quota value' }));
      return;
    }
    const limitBytes = Math.round(value * UNIT_FACTORS[edit.limitUnit]);
    const softWarn = parseInt(edit.softWarnInput, 10);
    if (!Number.isFinite(softWarn) || softWarn < 0 || softWarn > 100) {
      setEdit((s) => ({ ...s, error: 'Soft warning threshold must be an integer from 0 to 100' }));
      return;
    }
    const { item } = edit;
    const ok = await confirm({
      title: 'Adjust tenant quota',
      message: `Set the ${item.bucket_purpose} quota for ${item.tenant_id} to ${formatBytes(limitBytes)} (soft warning at ${softWarn}%)?`,
      confirmText: 'Adjust',
    });
    if (!ok) return;
    setEdit((s) => ({ ...s, busy: true, error: null }));
    try {
      await api.admin.lifecycle.storageQuotas.patch(
        item.tenant_id,
        item.bucket_purpose,
        { limit_bytes: limitBytes, soft_warn_pct: softWarn },
      );
      toast.success('Updated');
      closeEdit();
      refresh();
    } catch (err) {
      setEdit((s) => ({
        ...s,
        busy: false,
        error: err instanceof ApiError ? err.message : 'Update failed',
      }));
    }
  };

  // ── Series fetch ──────────────────────────────────────────────────────────────

  const loadSeries = useCallback(() => {
    setSeriesLoading(true);
    setSeriesErr(null);
    const step =
      chartRange === '1h' || chartRange === '6h'
        ? '30s'
        : chartRange === '24h'
          ? '5m'
          : '30m';
    api.admin.metrics
      .getStorageQuotaSeries({
        range: chartRange,
        step,
        purpose: chartPurpose || undefined,
        tenant_id: chartTenantId || undefined,
      })
      .then((res) => setSeries(res))
      .catch((err) => {
        if (isTodo(err)) setSeries(null);
        else setSeriesErr(err instanceof ApiError ? err.message : 'Failed to load trend');
      })
      .finally(() => setSeriesLoading(false));
  }, [chartRange, chartPurpose, chartTenantId]);

  useEffect(() => {
    loadSeries();
  }, [loadSeries, reloadKey]);

  // Flatten Prometheus matrix into a single time-keyed table — sum across all
  // returned series at each timestamp so a "tenant + purpose" filter that hits
  // multiple metric labels still renders a coherent line.
  const chartData = useMemo(() => {
    const result = series?.data?.result ?? [];
    if (result.length === 0) return [] as { t: number; v: number }[];
    const bucket = new Map<number, number>();
    for (const s of result) {
      for (const [ts, val] of s.values) {
        const n = parseFloat(val);
        if (!Number.isFinite(n)) continue;
        bucket.set(ts, (bucket.get(ts) ?? 0) + n);
      }
    }
    return Array.from(bucket.entries())
      .sort((a, b) => a[0] - b[0])
      .map(([t, v]) => ({ t, v }));
  }, [series]);

  const tenantOptions = useMemo(() => {
    const ids = Array.from(new Set(items.map((i) => i.tenant_id))).sort();
    return [
      { value: '', label: 'All tenants' },
      ...ids.map((id) => ({ value: id, label: id })),
    ];
  }, [items]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">
          Storage / Tenant quotas
        </h1>
        <p className="text-[12px] text-dim mt-0.5">
          Cross-tenant quota summary, limit adjustments and trend monitoring (→ lifecycle + prometheus)
        </p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the storage quota summary endpoint is unreachable. Data will appear here once it’s connected.
        </div>
      )}

      {/* Filter row */}
      {!todo && (
        <div className="flex items-center gap-3">
          <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
            Bucket type
          </label>
          <CustomSelect
            options={[
              { value: '', label: 'All' },
              ...PURPOSES.map((p) => ({ value: p, label: p })),
            ]}
            value={filterPurpose}
            onChange={(v) => setFilterPurpose((v as BucketPurpose) || '')}
            className="min-w-[160px]"
          />
          <button
            onClick={refresh}
            className="ml-auto px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
          >
            Refresh
          </button>
        </div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}

      {/* Quotas table */}
      {!todo && !loading && filteredItems.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">
          No quota records{filterPurpose ? ` (bucket type: ${filterPurpose})` : ''}
        </div>
      )}

      {!todo && filteredItems.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Tenant
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Bucket
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Used
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Limit
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Usage
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Soft warning
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Updated
                </th>
                <th className="px-3 py-2 w-20"></th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.map((it) => {
                const pct = Math.max(0, Math.min(100, it.used_pct));
                return (
                  <tr
                    key={`${it.tenant_id}:${it.bucket_purpose}`}
                    className="border-b border-line-2 hover:bg-paper-2 transition-colors"
                  >
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[180px]">
                      {it.tenant_id}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                      {it.bucket_purpose}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">
                      {formatBytes(it.used_bytes)}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">
                      {formatBytes(it.limit_bytes)}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex items-center gap-2">
                        <div className="w-24 h-1.5 bg-paper-2 rounded-sm overflow-hidden border border-line">
                          <div
                            className={`h-full ${
                              it.used_pct >= 100
                                ? 'bg-red-500'
                                : it.used_pct >= it.soft_warn_pct
                                  ? 'bg-amber-500'
                                  : 'bg-green-500'
                            }`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span
                          className={`text-[10.5px] font-mono px-1.5 py-0.5 border rounded-sm ${tonePill(it.used_pct, it.soft_warn_pct)}`}
                        >
                          {it.used_pct.toFixed(1)}%
                        </span>
                      </div>
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">
                      {it.soft_warn_pct}%
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">
                      {fmt(it.updated_at)}
                    </td>
                    <td className="px-3 py-2">
                      <button
                        onClick={() => openEdit(it)}
                        className="text-[11px] font-mono text-ink-2 border border-line rounded-sm px-2 py-0.5 hover:bg-paper-2"
                      >
                        Adjust
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Trend chart */}
      <section className="flex flex-col gap-2 mt-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
          Trend — storage_quota_used_bytes
        </div>
        <div className="flex items-center gap-3 flex-wrap">
          <CustomSelect
            options={tenantOptions}
            value={chartTenantId}
            onChange={(v) => setChartTenantId(v)}
            className="min-w-[220px]"
          />
          <CustomSelect
            options={[
              { value: '', label: 'All buckets' },
              ...PURPOSES.map((p) => ({ value: p, label: p })),
            ]}
            value={chartPurpose}
            onChange={(v) => setChartPurpose((v as BucketPurpose) || '')}
            className="min-w-[140px]"
          />
          <CustomSelect
            options={[
              { value: '1h', label: 'Last hour' },
              { value: '6h', label: 'Last 6 hours' },
              { value: '24h', label: 'Last 24 hours' },
              { value: '7d', label: 'Last 7 days' },
            ]}
            value={chartRange}
            onChange={(v) => setChartRange(v as '1h' | '6h' | '24h' | '7d')}
            className="min-w-[140px]"
          />
        </div>

        {seriesErr && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">
            {seriesErr}
          </div>
        )}

        <div className="border border-line rounded-md p-3 bg-surface h-[260px]">
          {seriesLoading && (
            <div className="font-mono text-[12px] text-dim">Loading trend…</div>
          )}
          {!seriesLoading && chartData.length === 0 && (
            <div className="font-mono text-[12px] text-dim italic h-full flex items-center justify-center">
              No data
            </div>
          )}
          {!seriesLoading && chartData.length > 0 && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                <XAxis
                  dataKey="t"
                  tickFormatter={(t) =>
                    formatTimeAuto(new Date(t * 1000), {
                      hour: '2-digit',
                      minute: '2-digit',
                    })
                  }
                  fontSize={10}
                />
                <YAxis
                  tickFormatter={(v) => formatBytes(Number(v))}
                  width={70}
                  fontSize={10}
                />
                <Tooltip
                  labelFormatter={(t) =>
                    formatDateTimeAuto(new Date(Number(t) * 1000))
                  }
                  formatter={(value) => [formatBytes(Number(value) || 0), 'Used']}
                />
                <Line
                  type="monotone"
                  dataKey="v"
                  stroke="#0ea5e9"
                  strokeWidth={1.5}
                  dot={false}
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>

      {/* Edit modal */}
      {edit.open && edit.item && (
        <Dialog
          title="Adjust quota"
          onClose={closeEdit}
          dismissable={!edit.busy}
          className="w-full max-w-[420px]"
        >
          <div className="text-[12px] text-ink">
            {edit.item.tenant_id} · {edit.item.bucket_purpose}
          </div>
          <div className="text-[11px] text-dim">
            Currently used: {formatBytes(edit.item.used_bytes)} / limit{' '}
            {formatBytes(edit.item.limit_bytes)}
          </div>
          <form onSubmit={submitEdit} className="flex flex-col gap-3">
            {edit.error && (
              <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">
                {edit.error}
              </div>
            )}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Limit</label>
              <div className="flex gap-2">
                <input
                  value={edit.limitValue}
                  onChange={(e) => setEdit((s) => ({ ...s, limitValue: e.target.value }))}
                  inputMode="decimal"
                  className="flex-1 border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                  placeholder="100"
                />
                <CustomSelect
                  options={SIZE_UNITS.map((u) => ({ value: u, label: u }))}
                  value={edit.limitUnit}
                  onChange={(v) => setEdit((s) => ({ ...s, limitUnit: v as SizeUnit }))}
                  className="w-24"
                />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Soft warning threshold (%)</label>
              <input
                value={edit.softWarnInput}
                onChange={(e) => setEdit((s) => ({ ...s, softWarnInput: e.target.value }))}
                type="number"
                min={0}
                max={100}
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
              />
            </div>
            <div className="flex gap-2 justify-end mt-2">
              <button
                type="button"
                onClick={closeEdit}
                disabled={edit.busy}
                className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={edit.busy}
                className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {edit.busy ? 'Submitting…' : 'Confirm'}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}
