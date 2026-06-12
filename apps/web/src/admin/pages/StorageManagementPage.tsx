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
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN');
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

function parseSizeInput(raw: string): number | null {
  const m = raw
    .trim()
    .toUpperCase()
    .match(/^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)?$/);
  if (!m) return null;
  const v = parseFloat(m[1]);
  const unit = m[2] || 'B';
  const factor = { B: 1, KB: 1024, MB: 1024 ** 2, GB: 1024 ** 3, TB: 1024 ** 4 }[
    unit
  ];
  if (!factor || !Number.isFinite(v) || v < 0) return null;
  return Math.round(v * factor);
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
  limitInput: string;
  softWarnInput: string;
  busy: boolean;
  error: string | null;
}

const EMPTY_EDIT: EditState = {
  open: false,
  item: null,
  limitInput: '',
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
        else setError(err instanceof ApiError ? err.message : '加载失败');
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

  const openEdit = (it: StorageQuotaSummaryItem) =>
    setEdit({
      open: true,
      item: it,
      limitInput: `${(it.limit_bytes / (1024 ** 3)).toFixed(2)} GB`,
      softWarnInput: String(it.soft_warn_pct),
      busy: false,
      error: null,
    });

  const closeEdit = () => setEdit(EMPTY_EDIT);

  const submitEdit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!edit.item) return;
    const limitBytes = parseSizeInput(edit.limitInput);
    if (limitBytes === null) {
      setEdit((s) => ({ ...s, error: '配额格式无效（示例：100 GB / 1024 MB）' }));
      return;
    }
    const softWarn = parseInt(edit.softWarnInput, 10);
    if (!Number.isFinite(softWarn) || softWarn < 0 || softWarn > 100) {
      setEdit((s) => ({ ...s, error: '软警戒阈值需为 0–100 之间的整数' }));
      return;
    }
    const { item } = edit;
    const ok = await confirm({
      title: '调整租户配额',
      message: `将 ${item.tenant_id} 的 ${item.bucket_purpose} 配额调整为 ${formatBytes(limitBytes)}（软警戒 ${softWarn}%）？`,
      confirmText: '确认调整',
    });
    if (!ok) return;
    setEdit((s) => ({ ...s, busy: true, error: null }));
    try {
      await api.admin.lifecycle.storageQuotas.patch(
        item.tenant_id,
        item.bucket_purpose,
        { limit_bytes: limitBytes, soft_warn_pct: softWarn },
      );
      toast.success('已更新');
      closeEdit();
      refresh();
    } catch (err) {
      setEdit((s) => ({
        ...s,
        busy: false,
        error: err instanceof ApiError ? err.message : '更新失败',
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
        else setSeriesErr(err instanceof ApiError ? err.message : '加载趋势失败');
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
      { value: '', label: '全部租户' },
      ...ids.map((id) => ({ value: id, label: id })),
    ];
  }, [items]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">
          存储管理 / 租户配额
        </h1>
        <p className="text-[12px] text-dim mt-0.5">
          跨租户配额汇总、上限调整与趋势监控（→ lifecycle + prometheus）
        </p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：存储配额汇总端点不可达，接线后将自动展示。
        </div>
      )}

      {/* Filter row */}
      {!todo && (
        <div className="flex items-center gap-3">
          <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
            桶类型过滤
          </label>
          <CustomSelect
            options={[
              { value: '', label: '全部' },
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
            刷新
          </button>
        </div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {/* Quotas table */}
      {!todo && !loading && filteredItems.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">
          无配额记录{filterPurpose ? `（桶类型：${filterPurpose}）` : ''}
        </div>
      )}

      {!todo && filteredItems.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  租户
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  桶
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  已用
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  上限
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  占用率
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  软警戒
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  更新时间
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
                        调整
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
          趋势 — storage_quota_used_bytes
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
              { value: '', label: '全部桶' },
              ...PURPOSES.map((p) => ({ value: p, label: p })),
            ]}
            value={chartPurpose}
            onChange={(v) => setChartPurpose((v as BucketPurpose) || '')}
            className="min-w-[140px]"
          />
          <CustomSelect
            options={[
              { value: '1h', label: '近 1 小时' },
              { value: '6h', label: '近 6 小时' },
              { value: '24h', label: '近 24 小时' },
              { value: '7d', label: '近 7 天' },
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
            <div className="font-mono text-[12px] text-dim">加载趋势…</div>
          )}
          {!seriesLoading && chartData.length === 0 && (
            <div className="font-mono text-[12px] text-dim italic h-full flex items-center justify-center">
              暂无数据
            </div>
          )}
          {!seriesLoading && chartData.length > 0 && (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 0, left: 4 }}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.3} />
                <XAxis
                  dataKey="t"
                  tickFormatter={(t) =>
                    new Date(t * 1000).toLocaleTimeString('zh-CN', {
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
                    new Date(Number(t) * 1000).toLocaleString('zh-CN')
                  }
                  formatter={(value) => [formatBytes(Number(value) || 0), '已用']}
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
          title="调整配额"
          onClose={closeEdit}
          dismissable={!edit.busy}
          className="w-full max-w-[420px]"
        >
          <div className="text-[12px] text-ink">
            {edit.item.tenant_id} · {edit.item.bucket_purpose}
          </div>
          <div className="text-[11px] text-dim">
            当前已用：{formatBytes(edit.item.used_bytes)} ／ 上限{' '}
            {formatBytes(edit.item.limit_bytes)}
          </div>
          <form onSubmit={submitEdit} className="flex flex-col gap-3">
            {edit.error && (
              <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">
                {edit.error}
              </div>
            )}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">
                上限（支持 B / KB / MB / GB / TB）
              </label>
              <input
                value={edit.limitInput}
                onChange={(e) => setEdit((s) => ({ ...s, limitInput: e.target.value }))}
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                placeholder="100 GB"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">软警戒阈值 (%)</label>
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
                取消
              </button>
              <button
                type="submit"
                disabled={edit.busy}
                className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {edit.busy ? '提交中…' : '确认'}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </div>
  );
}
