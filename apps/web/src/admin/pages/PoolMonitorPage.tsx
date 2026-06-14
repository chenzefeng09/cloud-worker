import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { PoolSnapshot } from '../../api/types';
import { formatTimeAuto } from '../../i18n/dates';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

// Per-bucket memory size (GiB), mirrors sandbox-pool config memory_tier_sizes
// (small=1g, large=4g). Kept here only for the resident-RAM display; the pool
// service remains the authoritative source of the sizing.
const TIER_SIZE_GB: Record<string, number> = {
  small: 1,
  large: 4,
};

const TIER_LABELS: Record<string, string> = {
  small: '小档 (small)',
  large: '大档 (large)',
};

const REFRESH_INTERVAL_MS = 10_000;

interface TierRow {
  tier: string;
  idle: number;
  total: number;
  sizeGb: number;
  residentGb: number;
}

export function PoolMonitorPage() {
  const [snapshot, setSnapshot] = useState<PoolSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastRefreshed, setLastRefreshed] = useState<Date | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.ops
      .pool()
      .then((snap) => {
        setSnapshot(snap);
        setLastRefreshed(new Date());
      })
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
    if (autoRefresh) {
      timer.current = setInterval(load, REFRESH_INTERVAL_MS);
    }
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [autoRefresh, load]);

  const tierMap = snapshot?.by_memory_tier ?? {};
  const rows: TierRow[] = Object.entries(tierMap).map(([tier, info]) => {
    const sizeGb = TIER_SIZE_GB[tier] ?? 0;
    return {
      tier,
      idle: info.idle,
      total: info.total,
      sizeGb,
      residentGb: info.total * sizeGb,
    };
  });
  rows.sort((a, b) => a.tier.localeCompare(b.tier));

  const totalResidentGb = rows.reduce((sum, r) => sum + r.residentGb, 0);
  const totalIdle = rows.reduce((sum, r) => sum + r.idle, 0);
  const totalTotal = rows.reduce((sum, r) => sum + r.total, 0);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">温池监控</h1>
          <p className="text-[12px] text-dim mt-0.5">分档温沙箱池占用情况（只读，平台级，不区分租户）。</p>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <label className="flex items-center gap-1.5 text-[11px] font-mono text-dim cursor-pointer select-none">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="accent-ink"
            />
            自动刷新 (10s)
          </label>
          <button
            onClick={load}
            disabled={loading}
            className="px-3 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
          >
            {loading ? '刷新中…' : '刷新'}
          </button>
        </div>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力服务未启用（沙箱池代理端点不可达）。
        </div>
      )}

      {/* Cube-only caveat. */}
      <div className="border border-line rounded-md px-4 py-3 bg-paper-2 text-[12px] text-ink-2 flex gap-2">
        <span className="font-mono text-dim shrink-0">ⓘ</span>
        <span>
          常驻预留内存仅在 <span className="font-mono">Cube</span> 运行时有意义——温池会按档位预热并常驻容器；
          在 <span className="font-mono">Docker</span> 运行时为按需创建，下表常驻内存仅作容量参考。
        </span>
      </div>

      {!todo && (
        <section className="flex flex-col gap-2">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim flex items-center justify-between">
            <span>分档占用</span>
            {lastRefreshed && (
              <span className="normal-case tracking-normal text-dim">
                最后刷新 {formatTimeAuto(lastRefreshed)}
              </span>
            )}
          </div>
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="border-b border-line-2 bg-paper-2">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim">档位</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">空闲</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">总数</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">单沙箱内存</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">常驻预留内存</th>
                </tr>
              </thead>
              <tbody>
                {rows.length > 0 ? (
                  rows.map((r) => (
                    <tr key={r.tier} className="border-b border-line-2 hover:bg-paper-2">
                      <td className="px-3 py-2 font-mono text-[12px] text-ink">{TIER_LABELS[r.tier] ?? r.tier}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-ink-2 text-right">{r.idle}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-ink text-right">{r.total}</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-dim text-right">{r.sizeGb}g</td>
                      <td className="px-3 py-2 font-mono text-[12px] text-ink text-right">{r.residentGb}g</td>
                    </tr>
                  ))
                ) : (
                  <tr>
                    <td colSpan={5} className="px-3 py-4 font-mono text-[12px] text-dim italic">
                      {loading ? '加载中…' : '暂无分档数据'}
                    </td>
                  </tr>
                )}
              </tbody>
              {rows.length > 0 && (
                <tfoot className="border-t border-line bg-paper-2">
                  <tr>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim uppercase tracking-[0.08em]">合计</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink-2 text-right">{totalIdle}</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink text-right">{totalTotal}</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-dim text-right">—</td>
                    <td className="px-3 py-2 font-mono text-[12px] text-ink text-right font-semibold">
                      {totalResidentGb}g
                    </td>
                  </tr>
                </tfoot>
              )}
            </table>
          </div>
        </section>
      )}
    </div>
  );
}
