import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { useConfirm } from '../components/dialogs';
import { formatDateTimeAuto } from '../../i18n/dates';
import { CustomSelect } from '../components/CustomSelect';
import type {
  DailyTokenUsage,
  OpsRun,
  PoolSnapshot,
  RunStatus,
  SandboxInfo,
  Tenant,
} from '../../api/types';

// ─── Constants ────────────────────────────────────────────────────────────────

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'PENDING', label: '待处理' },
  { value: 'RUNNING', label: '运行中' },
  { value: 'COMPLETED', label: '已完成' },
  { value: 'FAILED', label: '失败' },
  { value: 'CANCELLED', label: '已取消' },
];

const STATUS_COLORS: Record<string, string> = {
  COMPLETED: '#22c55e',
  RUNNING: '#f59e0b',
  PENDING: '#6b7280',
  FAILED: '#ef4444',
  CANCELLED: '#94a3b8',
};

const STATUS_LABELS: Record<string, string> = {
  PENDING: '待处理',
  RUNNING: '运行中',
  COMPLETED: '已完成',
  FAILED: '失败',
  CANCELLED: '已取消',
};

const TENANT_PALETTE = [
  '#ef4444', '#f59e0b', '#22c55e', '#3b82f6',
  '#8b5cf6', '#ec4899', '#14b8a6', '#f97316',
];

const RUN_STATUS_CLASS: Record<string, string> = {
  PENDING: 'text-dim',
  RUNNING: 'text-warn',
  COMPLETED: 'text-ok',
  FAILED: 'text-accent',
  CANCELLED: 'text-dim',
};

const PAGE_SIZE = 20;

// ─── SVG Pie Chart (pure SVG, no library) ─────────────────────────────────────

interface PieSlice {
  label: string;
  value: number;
  color: string;
}

function PieChart({ data, size = 200 }: { data: PieSlice[]; size?: number }) {
  const total = data.reduce((s, d) => s + d.value, 0);
  if (total === 0) {
    return (
      <div className="flex items-center justify-center h-[160px]">
        <span className="font-mono text-[12px] text-dim italic">暂无数据</span>
      </div>
    );
  }

  const cx = size / 2;
  const cy = size / 2;
  const outerR = size * 0.4;
  const innerR = size * 0.25;

  // Build path segments
  const segments: { d: string; color: string }[] = [];
  let cumAngle = -Math.PI / 2; // start from top

  for (const slice of data) {
    if (slice.value === 0) continue;
    const angle = (slice.value / total) * 2 * Math.PI;
    const startAngle = cumAngle;
    const endAngle = cumAngle + angle;

    const x1Outer = cx + outerR * Math.cos(startAngle);
    const y1Outer = cy + outerR * Math.sin(startAngle);
    const x2Outer = cx + outerR * Math.cos(endAngle);
    const y2Outer = cy + outerR * Math.sin(endAngle);
    const x1Inner = cx + innerR * Math.cos(endAngle);
    const y1Inner = cy + innerR * Math.sin(endAngle);
    const x2Inner = cx + innerR * Math.cos(startAngle);
    const y2Inner = cy + innerR * Math.sin(startAngle);

    const largeArc = angle > Math.PI ? 1 : 0;

    // Single-item case: draw two half-circle arcs to avoid zero-length arc
    if (data.filter((d) => d.value > 0).length === 1) {
      const midAngle = startAngle + Math.PI;
      const xMidOuter = cx + outerR * Math.cos(midAngle);
      const yMidOuter = cy + outerR * Math.sin(midAngle);
      const xMidInner = cx + innerR * Math.cos(midAngle);
      const yMidInner = cy + innerR * Math.sin(midAngle);
      // Slightly perturb end point to avoid exact overlap
      const eps = 0.0001;
      const x2OuterAdj = cx + outerR * Math.cos(endAngle - eps);
      const y2OuterAdj = cy + outerR * Math.sin(endAngle - eps);
      const x1InnerAdj = cx + innerR * Math.cos(endAngle - eps);
      const y1InnerAdj = cy + innerR * Math.sin(endAngle - eps);

      const d = [
        `M ${x1Outer} ${y1Outer}`,
        `A ${outerR} ${outerR} 0 0 1 ${xMidOuter} ${yMidOuter}`,
        `A ${outerR} ${outerR} 0 0 1 ${x2OuterAdj} ${y2OuterAdj}`,
        `L ${x1InnerAdj} ${y1InnerAdj}`,
        `A ${innerR} ${innerR} 0 0 0 ${xMidInner} ${yMidInner}`,
        `A ${innerR} ${innerR} 0 0 0 ${x2Inner} ${y2Inner}`,
        'Z',
      ].join(' ');
      segments.push({ d, color: slice.color });
    } else {
      const d = [
        `M ${x1Outer} ${y1Outer}`,
        `A ${outerR} ${outerR} 0 ${largeArc} 1 ${x2Outer} ${y2Outer}`,
        `L ${x1Inner} ${y1Inner}`,
        `A ${innerR} ${innerR} 0 ${largeArc} 0 ${x2Inner} ${y2Inner}`,
        'Z',
      ].join(' ');
      segments.push({ d, color: slice.color });
    }

    cumAngle = endAngle;
  }

  // Center label
  const centerLabel = total.toLocaleString();

  return (
    <div className="flex flex-col items-center gap-3">
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} className="shrink-0">
        {segments.map((seg, i) => (
          <path key={i} d={seg.d} fill={seg.color} stroke="var(--color-surface, #fff)" strokeWidth="1.5" />
        ))}
        <text
          x={cx}
          y={cy}
          textAnchor="middle"
          dominantBaseline="central"
          className="fill-ink"
          style={{ fontSize: '18px', fontFamily: 'var(--font-display, sans-serif)', fontWeight: 600 }}
        >
          {centerLabel}
        </text>
      </svg>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 justify-center">
        {data.filter((d) => d.value > 0).map((d) => (
          <div key={d.label} className="flex items-center gap-1.5">
            <span className="inline-block w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: d.color }} />
            <span className="font-mono text-[11px] text-dim">{d.label}</span>
            <span className="font-mono text-[11px] text-ink-2">{d.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function PieSection({ title, data }: { title: string; data: PieSlice[] }) {
  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">{title}</div>
      <PieChart data={data} size={180} />
    </section>
  );
}

// ─── Pagination ──────────────────────────────────────────────────────────────

function Pagination({
  total,
  page,
  pageSize,
  onChange,
}: {
  total: number;
  page: number;
  pageSize: number;
  onChange: (p: number) => void;
}) {
  const pages = Math.ceil(total / pageSize);
  if (total <= pageSize) return null;
  const from = page * pageSize + 1;
  const to = Math.min((page + 1) * pageSize, total);
  return (
    <div className="flex items-center justify-between px-3 py-2 border-t border-line">
      <span className="font-mono text-[11px] text-dim">
        显示 {from}–{to} / 共 {total} 条
      </span>
      <div className="flex gap-2">
        <button
          disabled={page === 0}
          onClick={() => onChange(page - 1)}
          className="font-mono text-[11px] text-dim hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
        >
          ← 上一页
        </button>
        <button
          disabled={page >= pages - 1}
          onClick={() => onChange(page + 1)}
          className="font-mono text-[11px] text-dim hover:text-ink disabled:opacity-30 disabled:cursor-not-allowed"
        >
          下一页 →
        </button>
      </div>
    </div>
  );
}

// ─── Token usage bar chart (CSS/SVG, no chart lib) ────────────────────────────

function UsageChartBlock({
  days,
  loading,
  error,
  isTodoEndpoint,
}: {
  days: DailyTokenUsage[] | null;
  loading: boolean;
  error: string | null;
  isTodoEndpoint: boolean;
}) {
  const max = days && days.length > 0 ? Math.max(...days.map((d) => d.tokens), 1) : 1;

  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">Token 用量趋势（近 14 天）</div>
      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      {isTodoEndpoint && (
        <div className="text-[12px] text-dim italic">该能力暂未启用：用量统计端点尚未就绪。</div>
      )}
      {days && days.length === 0 && !isTodoEndpoint && (
        <div className="font-mono text-[12px] text-dim italic">暂无用量数据</div>
      )}
      {days && days.length > 0 && (
        <div className="flex items-end gap-1.5 h-40 pt-2">
          {days.map((d) => {
            const pct = Math.round((d.tokens / max) * 100);
            return (
              <div key={d.day} className="flex-1 flex flex-col items-center justify-end gap-1 group h-full">
                <span className="text-[9px] font-mono text-dim opacity-0 group-hover:opacity-100 transition-opacity">
                  {d.tokens.toLocaleString()}
                </span>
                <div
                  className="w-full bg-accent/70 group-hover:bg-accent rounded-sm transition-colors"
                  style={{ height: `${Math.max(pct, 2)}%` }}
                  title={`${d.day}: ${d.tokens.toLocaleString()} tokens`}
                />
                <span className="text-[9px] font-mono text-dim">{d.day.slice(5)}</span>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function sandboxKey(s: SandboxInfo): string {
  return s.container_id ?? s.id ?? '';
}

function isTodoError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);
}

function groupBy<T>(items: T[], keyFn: (item: T) => string): Record<string, T[]> {
  const map: Record<string, T[]> = {};
  for (const item of items) {
    const k = keyFn(item);
    (map[k] ??= []).push(item);
  }
  return map;
}

// ─── Page ─────────────────────────────────────────────────────────────────────

type PoolTab = 'sandbox' | 'runs';

export function OpsDashboardPage() {
  // Platform admins may filter/act across tenants (sending ?tenant_id=); tenant
  // admins are locked to their own tenant — the BFF scopes everything to the
  // caller's tenant, so we never send a tenant_id override for them (a foreign
  // one would 403 at the BFF cross-tenant gate anyway).
  const userProfile = useChatStore((s) => s.userProfile);
  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);

  // ── Filter state ───────────────────────────────────────────────────────────
  const [tenantId, setTenantId] = useState('');
  const [statusFilter, setStatusFilter] = useState('');

  // The tenant_id sent to the BFF: only a platform admin acting cross-tenant
  // sends an override; a tenant_admin always sends undefined (own tenant).
  const apiTenantId = canSwitchTenant ? (tenantId || undefined) : undefined;

  // ── Sub-tab state ─────────────────────────────────────────────────────────
  const [poolTab, setPoolTab] = useState<PoolTab>('sandbox');

  // ── Search & pagination state ──────────────────────────────────────────────
  const [sandboxSearch, setSandboxSearch] = useState('');
  const [runsSearch, setRunsSearch] = useState('');
  const [sandboxPage, setSandboxPage] = useState(0);
  const [runsPage, setRunsPage] = useState(0);

  // Reset pagination when filters or search change
  useEffect(() => { setSandboxPage(0); }, [tenantId, sandboxSearch]);
  useEffect(() => { setRunsPage(0); }, [tenantId, statusFilter, runsSearch]);

  // ── Tenant list ────────────────────────────────────────────────────────────
  const [tenants, setTenants] = useState<Tenant[]>([]);

  useEffect(() => {
    api.admin.tenants.list()
      .then((r) => setTenants(r.items))
      .catch(() => {/* tenant list non-critical */});
  }, []);

  const tenantOptions = useMemo(() => [
    { value: '', label: '全部租户' },
    ...tenants.map((t) => ({ value: t.id, label: t.name || t.id })),
  ], [tenants]);

  const tenantNameMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const t of tenants) m.set(t.id, t.name);
    return m;
  }, [tenants]);

  // ── Usage data ─────────────────────────────────────────────────────────────
  const [usageDays, setUsageDays] = useState<DailyTokenUsage[] | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [usageTodo, setUsageTodo] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setUsageLoading(true);
    setUsageError(null);
    setUsageTodo(false);
    api.admin.ops
      .usageDaily({ days: 14, tenant_id: apiTenantId })
      .then((data) => { if (!cancelled) setUsageDays(data.days); })
      .catch((err) => {
        if (cancelled) return;
        if (isTodoError(err)) setUsageTodo(true);
        else setUsageError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => { if (!cancelled) setUsageLoading(false); });
    return () => { cancelled = true; };
  }, [apiTenantId]);

  // ── Sandbox data ───────────────────────────────────────────────────────────
  const [pool, setPool] = useState<PoolSnapshot | null>(null);
  const [allSandboxes, setAllSandboxes] = useState<SandboxInfo[]>([]);
  const [sandboxLoading, setSandboxLoading] = useState(false);
  const [sandboxError, setSandboxError] = useState<string | null>(null);
  const [sandboxTodo, setSandboxTodo] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSandboxLoading(true);
    setSandboxError(null);
    setSandboxTodo(false);
    Promise.all([api.admin.ops.pool(), api.admin.ops.sandboxes()])
      .then(([p, s]) => {
        if (cancelled) return;
        setPool(p);
        setAllSandboxes(s);
      })
      .catch((err) => {
        if (cancelled) return;
        if (isTodoError(err)) setSandboxTodo(true);
        else setSandboxError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => { if (!cancelled) setSandboxLoading(false); });
    return () => { cancelled = true; };
  }, []);

  // Client-side tenant filter + search for sandboxes
  const filteredSandboxes = useMemo(() => {
    let list = tenantId ? allSandboxes.filter((s) => s.tenant_id === tenantId) : allSandboxes;
    if (sandboxSearch) {
      const q = sandboxSearch.toLowerCase();
      list = list.filter((s) => {
        const id = (s.container_id ?? s.id ?? '').toLowerCase();
        const img = (s.image ?? '').toLowerCase();
        return id.includes(q) || img.includes(q);
      });
    }
    return list;
  }, [allSandboxes, tenantId, sandboxSearch]);

  const pagedSandboxes = useMemo(
    () => filteredSandboxes.slice(sandboxPage * PAGE_SIZE, (sandboxPage + 1) * PAGE_SIZE),
    [filteredSandboxes, sandboxPage],
  );

  const handleSandboxStopped = useCallback((id: string) => {
    setAllSandboxes((prev) => prev.filter((x) => sandboxKey(x) !== id));
  }, []);

  // ── Sandbox stop action ────────────────────────────────────────────────────
  const confirm = useConfirm();
  const [stopError, setStopError] = useState<string | null>(null);

  const stopSandbox = useCallback(async (s: SandboxInfo) => {
    const id = sandboxKey(s);
    if (!id) return;
    if (!(await confirm({ title: '停止沙箱', message: `确认停止沙箱「${s.name ?? id}」？`, tone: 'danger', confirmText: '停止' }))) return;
    try {
      await api.admin.ops.stopSandbox(id);
      handleSandboxStopped(id);
    } catch (err) {
      setStopError(err instanceof ApiError ? err.message : '停止失败');
    }
  }, [confirm, handleSandboxStopped]);

  // ── Runs data ──────────────────────────────────────────────────────────────
  const [runs, setRuns] = useState<OpsRun[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsTodo, setRunsTodo] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setRunsLoading(true);
    setRunsError(null);
    setRunsTodo(false);
    api.admin.ops
      .runs({
        tenant_id: apiTenantId,
        status: (statusFilter as RunStatus) || undefined,
      })
      .then((data) => { if (!cancelled) setRuns(data); })
      .catch((err) => {
        if (cancelled) return;
        if (isTodoError(err)) setRunsTodo(true);
        else setRunsError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => { if (!cancelled) setRunsLoading(false); });
    return () => { cancelled = true; };
  }, [apiTenantId, statusFilter]);

  const filteredRuns = useMemo(() => {
    if (!runsSearch) return runs;
    const q = runsSearch.toLowerCase();
    return runs.filter((r) =>
      r.id.toLowerCase().includes(q) || (r.task_description ?? '').toLowerCase().includes(q),
    );
  }, [runs, runsSearch]);

  const pagedRuns = useMemo(
    () => filteredRuns.slice(runsPage * PAGE_SIZE, (runsPage + 1) * PAGE_SIZE),
    [filteredRuns, runsPage],
  );

  // ── Pie chart data ─────────────────────────────────────────────────────────

  const runStatusPie = useMemo<PieSlice[]>(() => {
    const grouped = groupBy(runs, (r) => r.status);
    return Object.entries(grouped).map(([status, items]) => ({
      label: STATUS_LABELS[status] ?? status,
      value: items.length,
      color: STATUS_COLORS[status] ?? '#6b7280',
    }));
  }, [runs]);

  const sandboxStatusPie = useMemo<PieSlice[]>(() => {
    const grouped = groupBy(filteredSandboxes, (s) => s.status);
    return Object.entries(grouped).map(([status, items]) => ({
      label: status,
      value: items.length,
      color: STATUS_COLORS[status.toUpperCase()] ?? '#6b7280',
    }));
  }, [filteredSandboxes]);

  const sandboxTenantPie = useMemo<PieSlice[]>(() => {
    // Use ALL sandboxes (not filtered) for tenant distribution
    const grouped = groupBy(allSandboxes, (s) => s.tenant_id);
    return Object.entries(grouped).map(([tid, items], i) => ({
      label: tenantNameMap.get(tid) || (tid.length > 8 ? `${tid.slice(0, 8)}…` : tid),
      value: items.length,
      color: TENANT_PALETTE[i % TENANT_PALETTE.length],
    }));
  }, [allSandboxes, tenantNameMap]);

  // ── Derived error for sandbox section ─────────────────────────────────────
  const sandboxDisplayError = stopError || sandboxError;

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">运行与沙箱</h1>
        <p className="text-[12px] text-dim mt-0.5">Token 用量、沙箱池与运行监控</p>
      </div>

      {/* Filter bar — the tenant filter is platform-admin only; tenant admins
          are scoped to their own tenant by the BFF and see no tenant picker. */}
      <div className="flex items-center gap-3 flex-wrap">
        {canSwitchTenant && (
          <CustomSelect
            options={tenantOptions}
            value={tenantId}
            onChange={setTenantId}
            placeholder="全部租户"
            className="w-48"
          />
        )}
        <CustomSelect
          options={STATUS_OPTIONS}
          value={statusFilter}
          onChange={setStatusFilter}
          placeholder="全部状态"
          className="w-36"
        />
      </div>

      <UsageChartBlock
        days={usageDays}
        loading={usageLoading}
        error={usageError}
        isTodoEndpoint={usageTodo}
      />

      {/* Pie charts row */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <PieSection title="运行状态分布" data={runStatusPie} />
        <PieSection title="沙箱状态分布" data={sandboxStatusPie} />
        <PieSection title="沙箱按租户分布" data={sandboxTenantPie} />
      </div>

      {/* Combined sandbox pool + runs section */}
      <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
        <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">沙箱池</div>

        {sandboxDisplayError && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{sandboxDisplayError}</div>
        )}
        {sandboxTodo && (
          <div className="text-[12px] text-dim italic">该能力暂未启用：沙箱池端点尚未就绪。</div>
        )}

        {/* Pool cards — always visible */}
        {pool && (
          <div className="flex flex-wrap gap-3">
            <div className="border border-line-2 rounded-md px-3 py-2 min-w-[100px]">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">总数</div>
              <div className="text-[18px] font-display text-ink">{pool.total}</div>
            </div>
            {Object.entries(pool.by_template ?? {}).map(([tpl, info]) => (
              <div key={tpl} className="border border-line-2 rounded-md px-3 py-2 min-w-[100px]">
                <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim truncate max-w-[140px]">{tpl}</div>
                <div className="text-[13px] font-mono text-ink-2">
                  空闲 {info.idle} / 共 {info.total}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Tab bar */}
        <div className="flex gap-4 border-b border-line">
          <button
            className={`pb-2 text-[13px] font-mono transition-colors border-b-2 ${
              poolTab === 'sandbox'
                ? 'border-accent text-ink'
                : 'border-transparent text-dim hover:text-ink-2'
            }`}
            onClick={() => setPoolTab('sandbox')}
          >
            沙箱列表
          </button>
          <button
            className={`pb-2 text-[13px] font-mono transition-colors border-b-2 ${
              poolTab === 'runs'
                ? 'border-accent text-ink'
                : 'border-transparent text-dim hover:text-ink-2'
            }`}
            onClick={() => setPoolTab('runs')}
          >
            运行列表
          </button>
        </div>

        {/* Tab content: Sandbox list */}
        {poolTab === 'sandbox' && (
          <>
            <input
              type="text"
              value={sandboxSearch}
              onChange={(e) => setSandboxSearch(e.target.value)}
              placeholder="搜索容器 ID 或镜像…"
              className="w-64 border border-line rounded-sm px-2 py-1.5 text-[13px] font-mono bg-paper text-ink placeholder:text-dim focus:outline-none focus:border-accent transition-colors"
            />
            {sandboxLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {!sandboxTodo && !sandboxLoading && filteredSandboxes.length === 0 && (
              <div className="font-mono text-[12px] text-dim italic">暂无运行中的沙箱</div>
            )}
            {pagedSandboxes.length > 0 && (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">容器 ID</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">镜像</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">租户</th>
                      <th className="px-3 py-2 w-20"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {pagedSandboxes.map((s) => {
                      const id = sandboxKey(s);
                      return (
                        <tr key={id || s.name} className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
                          <td className="px-3 py-2 font-mono text-[12px] text-ink-2 truncate max-w-[160px]">
                            {id ? `${id.slice(0, 12)}…` : '—'}
                          </td>
                          <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[180px]">{s.image || '—'}</td>
                          <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.status}</td>
                          <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[120px]" title={s.tenant_id}>
                            {tenantNameMap.get(s.tenant_id) || s.tenant_id}
                          </td>
                          <td className="px-3 py-2">
                            <button
                              onClick={() => stopSandbox(s)}
                              className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-accent hover:opacity-80 transition-opacity"
                            >
                              停止
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <Pagination
                  total={filteredSandboxes.length}
                  page={sandboxPage}
                  pageSize={PAGE_SIZE}
                  onChange={setSandboxPage}
                />
              </div>
            )}
          </>
        )}

        {/* Tab content: Runs list */}
        {poolTab === 'runs' && (
          <>
            <input
              type="text"
              value={runsSearch}
              onChange={(e) => setRunsSearch(e.target.value)}
              placeholder="搜索运行 ID 或任务描述…"
              className="w-64 border border-line rounded-sm px-2 py-1.5 text-[13px] font-mono bg-paper text-ink placeholder:text-dim focus:outline-none focus:border-accent transition-colors"
            />
            {runsLoading && <div className="font-mono text-[12px] text-dim">加载中…</div>}
            {runsError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{runsError}</div>}
            {runsTodo && (
              <div className="text-[12px] text-dim italic">该能力暂未启用：运行列表端点不可达，接线后将自动展示。</div>
            )}
            {!runsTodo && !runsLoading && filteredRuns.length === 0 && !runsError && (
              <div className="font-mono text-[12px] text-dim italic">暂无运行记录</div>
            )}
            {pagedRuns.length > 0 && (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">运行 ID</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">状态</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">任务</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Token</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">创建时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {pagedRuns.map((r) => (
                      <tr key={r.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[12px] text-ink-2 truncate max-w-[140px]">
                          {r.id.slice(0, 12)}…
                        </td>
                        <td className={`px-3 py-2 font-mono text-[11px] ${RUN_STATUS_CLASS[r.status] ?? 'text-dim'}`}>
                          {r.status}
                        </td>
                        <td className="px-3 py-2 text-[12px] text-dim truncate max-w-[240px]">
                          {r.task_description || '—'}
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                          {r.total_tokens != null ? r.total_tokens.toLocaleString() : '—'}
                        </td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">
                          {formatDateTimeAuto(new Date(r.created_at))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <Pagination
                  total={filteredRuns.length}
                  page={runsPage}
                  pageSize={PAGE_SIZE}
                  onChange={setRunsPage}
                />
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
