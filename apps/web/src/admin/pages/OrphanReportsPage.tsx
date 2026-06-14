import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { OrphanReport } from '../../api/types';
import { formatDateTimeAuto } from '../../i18n/dates';

const PAGE_LIMIT = 20;

const isTodo = (err: unknown) =>
  err instanceof ApiError &&
  (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

function fmtDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return `${ms} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const rs = (s - m * 60).toFixed(0);
  return `${m}m ${rs}s`;
}

function Badge({ children, tone }: { children: React.ReactNode; tone: 'ok' | 'warn' | 'neutral' }) {
  const cls =
    tone === 'ok'
      ? 'bg-green-50 text-green-700 border-green-200'
      : tone === 'warn'
        ? 'bg-amber-50 text-amber-700 border-amber-200'
        : 'bg-paper-2 text-dim border-line';
  return (
    <span className={`inline-block text-[10.5px] font-mono px-1.5 py-0.5 border rounded-sm ${cls}`}>
      {children}
    </span>
  );
}

export function OrphanReportsPage() {
  const [items, setItems] = useState<OrphanReport[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<OrphanReport | null>(null);

  const loadInitial = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    api.admin.lifecycle.orphanReports
      .list({ limit: PAGE_LIMIT })
      .then((res) => {
        setItems(res.items ?? []);
        setNextCursor(res.next_cursor ?? null);
      })
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : '加载失败');
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  const loadMore = async () => {
    if (!nextCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await api.admin.lifecycle.orphanReports.list({
        limit: PAGE_LIMIT,
        before_id: nextCursor,
      });
      setItems((prev) => [...prev, ...(res.items ?? [])]);
      setNextCursor(res.next_cursor ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载更多失败');
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">
          存储管理 / 孤儿对账
        </h1>
        <p className="text-[12px] text-dim mt-0.5">
          DB ↔ S3 双向对账历史报告（→ lifecycle / orphan_reports）
        </p>
      </div>

      {/* Intro card */}
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 text-[12px] text-ink-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
          对账说明
        </div>
        <div>
          <span className="font-mono text-[11px] text-accent">方向 A</span>
          ：扫描对象存储中存在但数据库无记录的对象（"裸对象"），超过宽限期后自动删除。
        </div>
        <div>
          <span className="font-mono text-[11px] text-accent">方向 B</span>
          ：扫描数据库中标记存在但对象存储已消失的资源（"幽灵记录"），仅记录不修复，需人工介入。
        </div>
        <div>
          <span className="font-mono text-[11px] text-amber-700">dry_run</span>
          ：标记为 dry_run 的报告不会真正删除任何对象，仅统计。生产环境的报告应为 dry_run=false。
        </div>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力暂未启用：孤儿对账端点不可达，接线后将自动展示。
        </div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">加载中…</div>}

      {!todo && !loading && items.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">暂无对账记录</div>
      )}

      {!todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  开始时间
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  模式
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  宽限期
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  方向 A · 孤儿
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  方向 A · 删除
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  方向 B · 缺失
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  耗时
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  className="border-b border-line-2 hover:bg-paper-2 transition-colors cursor-pointer"
                >
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                    {fmt(r.started_at)}
                  </td>
                  <td className="px-3 py-2">
                    {r.dry_run ? (
                      <Badge tone="warn">dry_run</Badge>
                    ) : (
                      <Badge tone="ok">live</Badge>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {r.grace_period_days} 天
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                    {r.direction_a_orphans}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                    {r.direction_a_deleted}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">
                    {r.direction_b_missing}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {fmtDuration(r.duration_ms)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {nextCursor && (
        <div className="flex justify-center">
          <button
            onClick={loadMore}
            disabled={loadingMore}
            className="px-3 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
          >
            {loadingMore ? '加载中…' : '加载更多'}
          </button>
        </div>
      )}

      {/* Detail modal */}
      {selected && (
        <div
          className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center"
          onClick={() => setSelected(null)}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            className="bg-surface border border-line rounded-md p-4 w-[520px] max-h-[80vh] overflow-auto flex flex-col gap-3"
          >
            <div className="flex items-center justify-between">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                对账报告详情
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-[11px] font-mono text-dim hover:text-ink"
              >
                ✕
              </button>
            </div>
            <div className="text-[12px] text-ink font-mono">{selected.id}</div>

            <DetailRow label="开始时间" value={fmt(selected.started_at)} />
            <DetailRow label="结束时间" value={fmt(selected.completed_at)} />
            <DetailRow label="耗时" value={fmtDuration(selected.duration_ms)} />
            <DetailRow
              label="模式"
              value={selected.dry_run ? 'dry_run（仅统计）' : 'live（执行删除）'}
            />
            <DetailRow label="宽限期" value={`${selected.grace_period_days} 天`} />

            <div className="border-t border-line pt-2 mt-1" />

            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
              方向 A — 孤儿对象（S3 → DB）
            </div>
            <DetailRow label="检出孤儿" value={String(selected.direction_a_orphans)} />
            <DetailRow label="已删除" value={String(selected.direction_a_deleted)} />

            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mt-2">
              方向 B — 幽灵记录（DB → S3）
            </div>
            <DetailRow label="缺失对象" value={String(selected.direction_b_missing)} />

            <div className="flex justify-end mt-2">
              <button
                onClick={() => setSelected(null)}
                className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-3 text-[12px]">
      <span className="text-dim">{label}</span>
      <span className="font-mono text-ink-2">{value}</span>
    </div>
  );
}
