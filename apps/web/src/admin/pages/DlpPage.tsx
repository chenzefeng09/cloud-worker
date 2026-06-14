import { useCallback, useEffect, useState } from 'react';
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type {
  DlpClassification,
  DlpClassificationRow,
  DlpEvent,
  DlpScanResult,
  DlpSensitivity,
  DlpStats,
} from '../../api/types';

const SENSITIVITIES: DlpSensitivity[] = ['L1', 'L2', 'L3', 'L4', 'L5'];

const RESOURCE_TYPES: { value: string; label: string }[] = [
  { value: 'document', label: 'document（文档）' },
  { value: 'attachment', label: 'attachment（附件）' },
  { value: 'message', label: 'message（消息）' },
  { value: 'knowledge_base', label: 'knowledge_base（知识库）' },
  { value: 'skill', label: 'skill（技能）' },
  { value: 'run', label: 'run（运行）' },
];

// Filter dropdowns include an explicit "全部" option ('' = no filter).
const CHECK_TYPE_OPTIONS = [
  { value: '', label: '全部检查类型' },
  { value: 'scan', label: 'scan（内容扫描）' },
  { value: 'read', label: 'read（读取门禁）' },
  { value: 'download', label: 'download（下载门禁）' },
];

const DECISION_OPTIONS = [
  { value: '', label: '全部决策' },
  { value: 'ALLOW', label: 'ALLOW（放行）' },
  { value: 'DENY', label: 'DENY（拒绝）' },
  { value: 'QUARANTINE', label: 'QUARANTINE（隔离）' },
  { value: 'SCAN_REQUIRED', label: 'SCAN_REQUIRED（需扫描）' },
];

const RESOURCE_TYPE_FILTER = [{ value: '', label: '全部资源类型' }, ...RESOURCE_TYPES];
const SENSITIVITY_FILTER = [
  { value: '', label: '全部密级' },
  ...SENSITIVITIES.map((s) => ({ value: s, label: s })),
];

const DECISION_KEYS = ['ALLOW', 'DENY', 'QUARANTINE', 'SCAN_REQUIRED'];
// Decision → accent colour for the overview bar chart.
const DECISION_COLORS: Record<string, string> = {
  ALLOW: '#16a34a',
  DENY: '#dc2626',
  QUARANTINE: '#d97706',
  SCAN_REQUIRED: '#0ea5e9',
};
const SENSITIVITY_COLORS: Record<string, string> = {
  L1: '#94a3b8',
  L2: '#38bdf8',
  L3: '#0ea5e9',
  L4: '#d97706',
  L5: '#dc2626',
};

const PAGE_LIMIT = 50;

function fmtTime(s?: string | null): string {
  if (!s) return '—';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : formatDateTimeAuto(d);
}

function decisionBadgeClass(decision: string): string {
  switch (decision) {
    case 'ALLOW':
      return 'bg-green-50 text-green-700 border-green-200';
    case 'DENY':
      return 'bg-red-50 text-red-700 border-red-200';
    case 'QUARANTINE':
      return 'bg-amber-50 text-amber-700 border-amber-200';
    case 'SCAN_REQUIRED':
      return 'bg-sky-50 text-sky-700 border-sky-200';
    default:
      return 'bg-paper-2 text-ink-2 border-line';
  }
}

export function DlpPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();

  const tid = selectedTenantId ?? undefined;

  // ── Overview stats ──────────────────────────────────────────────────────────
  const [stats, setStats] = useState<DlpStats | null>(null);
  const [statsLoading, setStatsLoading] = useState(false);
  const [statsError, setStatsError] = useState<string | null>(null);

  // ── Events (scan/decision history) ──────────────────────────────────────────
  const [events, setEvents] = useState<DlpEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);
  const [eventsNextBefore, setEventsNextBefore] = useState<string | null>(null);
  const [evCheckType, setEvCheckType] = useState('');
  const [evDecision, setEvDecision] = useState('');

  // ── Classifications ledger ──────────────────────────────────────────────────
  const [rows, setRows] = useState<DlpClassificationRow[]>([]);
  const [rowsLoading, setRowsLoading] = useState(false);
  const [rowsError, setRowsError] = useState<string | null>(null);
  const [rowsNextBefore, setRowsNextBefore] = useState<string | null>(null);
  const [clSensitivity, setClSensitivity] = useState('');
  const [clResourceType, setClResourceType] = useState('');

  // ── Manual tools (collapsed) ────────────────────────────────────────────────
  const [toolsOpen, setToolsOpen] = useState(false);

  // Scan tool.
  const [content, setContent] = useState('');
  const [scanRt, setScanRt] = useState('document');
  const [scanRid, setScanRid] = useState('');
  const [scanResult, setScanResult] = useState<DlpScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);

  // Classification tagging.
  const [tagRt, setTagRt] = useState('document');
  const [tagRid, setTagRid] = useState('');
  const [sensitivity, setSensitivity] = useState<DlpSensitivity>('L3');
  const [tags, setTags] = useState('');
  const [reason, setReason] = useState('');
  const [tagging, setTagging] = useState(false);
  const [tagError, setTagError] = useState<string | null>(null);

  const [getRt, setGetRt] = useState('document');
  const [getRid, setGetRid] = useState('');
  const [classification, setClassification] = useState<DlpClassification | null>(null);
  const [getting, setGetting] = useState(false);
  const [getError, setGetError] = useState<string | null>(null);

  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  // ── Loaders ─────────────────────────────────────────────────────────────────

  const loadStats = useCallback(() => {
    setStatsLoading(true);
    setStatsError(null);
    api.admin.dlp
      .stats({}, tid)
      .then(setStats)
      .catch((err) => setStatsError(err instanceof ApiError ? err.message : '统计加载失败'))
      .finally(() => setStatsLoading(false));
  }, [tid]);

  const loadEvents = useCallback(
    (before?: string) => {
      setEventsLoading(true);
      setEventsError(null);
      api.admin.dlp
        .listEvents(
          {
            check_type: evCheckType || undefined,
            decision: evDecision || undefined,
            limit: PAGE_LIMIT,
            before,
          },
          tid,
        )
        .then((res) => {
          setEvents((prev) => (before ? [...prev, ...res.items] : res.items));
          setEventsNextBefore(res.next_before);
        })
        .catch((err) => setEventsError(err instanceof ApiError ? err.message : '历史加载失败'))
        .finally(() => setEventsLoading(false));
    },
    [tid, evCheckType, evDecision],
  );

  const loadRows = useCallback(
    (before?: string) => {
      setRowsLoading(true);
      setRowsError(null);
      api.admin.dlp
        .listClassifications(
          {
            sensitivity: clSensitivity || undefined,
            resource_type: clResourceType || undefined,
            limit: PAGE_LIMIT,
            before,
          },
          tid,
        )
        .then((res) => {
          setRows((prev) => (before ? [...prev, ...res.items] : res.items));
          setRowsNextBefore(res.next_before);
        })
        .catch((err) => setRowsError(err instanceof ApiError ? err.message : '密级资源加载失败'))
        .finally(() => setRowsLoading(false));
    },
    [tid, clSensitivity, clResourceType],
  );

  // Stats reload on tenant change.
  useEffect(() => {
    loadStats();
  }, [loadStats]);

  // Events / rows reload on tenant or their own filter change (fresh page).
  useEffect(() => {
    loadEvents();
  }, [loadEvents]);

  useEffect(() => {
    loadRows();
  }, [loadRows]);

  // ── Manual tool handlers ──────────────────────────────────────────────────────

  const handleScan = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!content.trim()) { setScanError('请填写待扫描内容'); return; }
    setScanning(true);
    setScanError(null);
    setScanResult(null);
    try {
      const res = await api.admin.dlp.scan(
        { content, resource_type: scanRt.trim(), resource_id: scanRid.trim() || 'adhoc-scan' },
        tid,
      );
      setScanResult(res);
    } catch (err) {
      setScanError(err instanceof ApiError ? err.message : '扫描失败');
    } finally {
      setScanning(false);
    }
  };

  const handleTag = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tagRid.trim()) { setTagError('请填写资源 ID'); return; }
    setTagging(true);
    setTagError(null);
    try {
      await api.admin.dlp.tagClassification(
        {
          resource_type: tagRt.trim(),
          resource_id: tagRid.trim(),
          sensitivity,
          tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
          reason: reason.trim() || null,
        },
        tid,
      );
      toast.success('已标注密级');
      setTags(''); setReason('');
      loadRows();
      loadStats();
    } catch (err) {
      setTagError(err instanceof ApiError ? err.message : '标注失败');
    } finally {
      setTagging(false);
    }
  };

  const handleGet = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!getRid.trim()) { setGetError('请填写资源 ID'); return; }
    setGetting(true);
    setGetError(null);
    setClassification(null);
    try {
      const res = await api.admin.dlp.getClassification(getRt.trim(), getRid.trim(), tid);
      setClassification(res);
    } catch (err) {
      setGetError(err instanceof ApiError ? err.message : '查询失败');
    } finally {
      setGetting(false);
    }
  };

  // ── Derived chart data ────────────────────────────────────────────────────────
  const decisionData = DECISION_KEYS.map((k) => ({ name: k, value: stats?.by_decision?.[k] ?? 0 }))
    // Surface any unexpected decisions the backend reports beyond the known set.
    .concat(
      Object.entries(stats?.by_decision ?? {})
        .filter(([k]) => !DECISION_KEYS.includes(k))
        .map(([name, value]) => ({ name, value })),
    );
  const sensitivityData = SENSITIVITIES.map((s) => ({ name: s, value: stats?.by_sensitivity?.[s] ?? 0 }));

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">数据防泄漏</h1>
        <p className="text-[12px] text-dim mt-0.5">治理面板：决策统计、扫描/决策历史、已标密级资源（→ dlp）</p>
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

      {/* ── 1. Overview stats ───────────────────────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">统计概览</div>
          <button
            type="button"
            onClick={loadStats}
            disabled={statsLoading}
            className="px-2 py-0.5 text-[11px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
          >
            {statsLoading ? '刷新中…' : '刷新'}
          </button>
        </div>
        {statsError && (
          <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
            {statsError}
          </div>
        )}
        {statsLoading && !stats && (
          <div className="font-mono text-[12px] text-dim">加载统计…</div>
        )}
        {stats && (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {/* Total events card */}
            <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-1 justify-center">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">事件总数（近 30 天）</div>
              <div className="text-[32px] font-display text-ink leading-none">
                {stats.total_events.toLocaleString()}
              </div>
              <div className="mt-1 flex flex-col gap-0.5">
                {DECISION_KEYS.filter((k) => (stats.by_decision?.[k] ?? 0) > 0).map((k) => (
                  <div key={k} className="flex items-center gap-1.5 text-[11px]">
                    <span
                      className="inline-block w-2 h-2 rounded-sm"
                      style={{ background: DECISION_COLORS[k] }}
                    />
                    <span className="font-mono text-dim">{k}</span>
                    <span className="font-mono text-ink-2 ml-auto">
                      {(stats.by_decision[k] ?? 0).toLocaleString()}
                    </span>
                  </div>
                ))}
                {DECISION_KEYS.every((k) => (stats.by_decision?.[k] ?? 0) === 0) && (
                  <span className="text-[11px] text-dim italic">暂无决策记录</span>
                )}
              </div>
            </div>

            {/* By decision chart */}
            <div className="border border-line rounded-md p-3 bg-surface flex flex-col gap-1">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">按决策分布</div>
              <div className="h-[160px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={decisionData} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                    <XAxis dataKey="name" fontSize={9} interval={0} tickLine={false} />
                    <YAxis fontSize={9} allowDecimals={false} width={32} />
                    <Tooltip
                      cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                      formatter={(v) => [Number(v).toLocaleString(), '事件数']}
                    />
                    <Bar dataKey="value" radius={[2, 2, 0, 0]}>
                      {decisionData.map((d) => (
                        <Cell key={d.name} fill={DECISION_COLORS[d.name] ?? '#64748b'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* By sensitivity chart */}
            <div className="border border-line rounded-md p-3 bg-surface flex flex-col gap-1">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">按密级分布</div>
              <div className="h-[160px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={sensitivityData} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                    <XAxis dataKey="name" fontSize={9} interval={0} tickLine={false} />
                    <YAxis fontSize={9} allowDecimals={false} width={32} />
                    <Tooltip
                      cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                      formatter={(v) => [Number(v).toLocaleString(), '资源数']}
                    />
                    <Bar dataKey="value" radius={[2, 2, 0, 0]}>
                      {sensitivityData.map((d) => (
                        <Cell key={d.name} fill={SENSITIVITY_COLORS[d.name] ?? '#64748b'} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            </div>
          </div>
        )}
      </section>

      {/* ── 2. Events: scan / decision history ──────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">扫描 / 决策历史</div>
        <div className="flex items-center gap-2 flex-wrap">
          <CustomSelect
            options={CHECK_TYPE_OPTIONS}
            value={evCheckType}
            onChange={setEvCheckType}
            className="min-w-[180px]"
          />
          <CustomSelect
            options={DECISION_OPTIONS}
            value={evDecision}
            onChange={setEvDecision}
            className="min-w-[180px]"
          />
        </div>
        {eventsError && (
          <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
            {eventsError}
          </div>
        )}
        {!eventsError && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">时间</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">检查类型</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">决策</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">用户</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">原因码</th>
                </tr>
              </thead>
              <tbody>
                {events.map((ev) => (
                  <tr key={ev.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                    <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
                      {fmtTime(ev.occurred_at)}
                    </td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink">{ev.check_type}</td>
                    <td className="px-3 py-2">
                      <span
                        className={`inline-flex items-center px-1.5 py-0.5 rounded-sm border font-mono text-[10.5px] ${decisionBadgeClass(ev.decision)}`}
                      >
                        {ev.decision}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[200px]">
                      {ev.resource_type ? `${ev.resource_type}/${ev.resource_id ?? ''}` : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]">
                      {ev.user_id ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">
                      {ev.reason_code ?? '—'}
                    </td>
                  </tr>
                ))}
                {events.length === 0 && !eventsLoading && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center font-mono text-[12px] text-dim italic">
                      无匹配事件
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center gap-3">
          {eventsLoading && <span className="font-mono text-[11px] text-dim">加载中…</span>}
          {eventsNextBefore && (
            <button
              type="button"
              onClick={() => loadEvents(eventsNextBefore)}
              disabled={eventsLoading}
              className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
            >
              加载更多
            </button>
          )}
        </div>
      </section>

      {/* ── 3. Classified resources ─────────────────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">已标密级资源</div>
        <div className="flex items-center gap-2 flex-wrap">
          <CustomSelect
            options={SENSITIVITY_FILTER}
            value={clSensitivity}
            onChange={setClSensitivity}
            className="min-w-[160px]"
          />
          <CustomSelect
            options={RESOURCE_TYPE_FILTER}
            value={clResourceType}
            onChange={setClResourceType}
            className="min-w-[200px]"
          />
        </div>
        {rowsError && (
          <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
            {rowsError}
          </div>
        )}
        {!rowsError && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源类型</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">资源 ID</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">密级</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">标签</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">标注人</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">标注时间</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={`${r.resource_type}/${r.resource_id}`}
                    className="border-b border-line-2 hover:bg-paper-2 transition-colors"
                  >
                    <td className="px-3 py-2 font-mono text-[12px] text-ink">{r.resource_type}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[200px]">
                      {r.resource_id}
                    </td>
                    <td className="px-3 py-2">
                      <span
                        className="inline-flex items-center px-1.5 py-0.5 rounded-sm font-mono text-[10.5px] text-white"
                        style={{ background: SENSITIVITY_COLORS[r.sensitivity] ?? '#64748b' }}
                      >
                        {r.sensitivity}
                      </span>
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[180px]">
                      {r.tags?.length ? r.tags.join(', ') : '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]">
                      {r.tagged_by ?? '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim whitespace-nowrap">
                      {fmtTime(r.tagged_at)}
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && !rowsLoading && (
                  <tr>
                    <td colSpan={6} className="px-3 py-6 text-center font-mono text-[12px] text-dim italic">
                      暂无已标密级资源
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center gap-3">
          {rowsLoading && <span className="font-mono text-[11px] text-dim">加载中…</span>}
          {rowsNextBefore && (
            <button
              type="button"
              onClick={() => loadRows(rowsNextBefore)}
              disabled={rowsLoading}
              className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50"
            >
              加载更多
            </button>
          )}
        </div>
      </section>

      {/* ── 4. Manual tools (collapsed) ─────────────────────────────────────── */}
      <section className="flex flex-col gap-2">
        <button
          type="button"
          onClick={() => setToolsOpen((o) => !o)}
          className="flex items-center gap-2 self-start font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim hover:text-ink"
        >
          <span className="text-[10px]">{toolsOpen ? '▼' : '▶'}</span>
          手动工具（内容扫描 / 密级标注 / 查询密级）
        </button>

        {toolsOpen && (
          <div className="flex flex-col gap-4">
            {/* Content scan */}
            <div className="flex flex-col gap-2">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">内容扫描</div>
              <form onSubmit={handleScan} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
                {scanError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{scanError}</div>}
                <div className="flex flex-col gap-1">
                  <label className="text-[11px] text-dim font-mono">待扫描内容</label>
                  <textarea
                    className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent resize-y min-h-[100px] font-mono"
                    value={content}
                    onChange={(e) => setContent(e.target.value)}
                    placeholder="粘贴待检测的文本…"
                  />
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源类型</label>
                    <CustomSelect options={RESOURCE_TYPES} value={scanRt} onChange={setScanRt} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源 ID（可选）</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={scanRid} onChange={(e) => setScanRid(e.target.value)} placeholder="留空即可，或填 doc-123" />
                    <span className="text-[10.5px] text-dim">留空将以 adhoc-scan 记录；填写则为平台内某文档/附件/技能等的唯一 ID</span>
                  </div>
                </div>
                <div>
                  <button type="submit" disabled={scanning} className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50">{scanning ? '扫描中…' : '开始扫描'}</button>
                </div>
              </form>
              {scanResult && (
                <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
                  <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">扫描结果</div>
                  <pre className="text-[11px] font-mono text-ink-2 whitespace-pre-wrap break-all bg-paper-2 rounded-sm p-2 overflow-auto max-h-[280px]">
                    {JSON.stringify(scanResult, null, 2)}
                  </pre>
                </div>
              )}
            </div>

            {/* Classification tag */}
            <div className="flex flex-col gap-2">
              <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">资源密级标注</div>
              <form onSubmit={handleTag} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
                {tagError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{tagError}</div>}
                <div className="grid grid-cols-3 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源类型</label>
                    <CustomSelect options={RESOURCE_TYPES} value={tagRt} onChange={setTagRt} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源 ID</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={tagRid} onChange={(e) => setTagRid(e.target.value)} placeholder="doc-123" />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">密级</label>
                    <CustomSelect
                      options={SENSITIVITIES.map((s) => ({ value: s, label: s }))}
                      value={sensitivity}
                      onChange={(v) => setSensitivity(v as DlpSensitivity)}
                    />
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">标签 (逗号分隔, 可选)</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="pii, finance" />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">原因 (可选)</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent" value={reason} onChange={(e) => setReason(e.target.value)} />
                  </div>
                </div>
                <div>
                  <button type="submit" disabled={tagging} className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50">{tagging ? '标注中…' : '标注密级'}</button>
                </div>
              </form>

              {/* Query classification */}
              <form onSubmit={handleGet} className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 max-w-2xl">
                <div className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">查询密级</div>
                {getError && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{getError}</div>}
                <div className="grid grid-cols-2 gap-2">
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源类型</label>
                    <CustomSelect options={RESOURCE_TYPES} value={getRt} onChange={setGetRt} />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-[11px] text-dim font-mono">资源 ID</label>
                    <input className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono" value={getRid} onChange={(e) => setGetRid(e.target.value)} placeholder="doc-123" />
                  </div>
                </div>
                <div>
                  <button type="submit" disabled={getting} className="px-3 py-1.5 text-[12px] font-mono border border-line bg-paper rounded-sm hover:bg-paper-2 disabled:opacity-50">{getting ? '查询中…' : '查询'}</button>
                </div>
                {classification && (
                  <div className="border-t border-line-2 pt-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[12px]">
                    <span className="font-mono text-dim">密级</span>
                    <span className="font-mono text-ink-2">{classification.sensitivity ?? '—'}</span>
                    <span className="font-mono text-dim">标签</span>
                    <span className="font-mono text-ink-2">{classification.tags?.join(', ') || '—'}</span>
                    <span className="font-mono text-dim">可降级</span>
                    <span className="font-mono text-ink-2">{classification.downgradable === undefined ? '—' : classification.downgradable ? '是' : '否'}</span>
                  </div>
                )}
              </form>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
