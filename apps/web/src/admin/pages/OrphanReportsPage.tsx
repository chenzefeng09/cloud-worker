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
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
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
      setError(err instanceof ApiError ? err.message : 'Failed to load more');
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">
          Storage / Orphan reconciliation
        </h1>
        <p className="text-[12px] text-dim mt-0.5">
          History of bidirectional DB ↔ S3 reconciliation reports (→ lifecycle / orphan_reports)
        </p>
      </div>

      {/* Intro card */}
      <div className="border border-line rounded-md p-4 bg-surface flex flex-col gap-2 text-[12px] text-ink-2">
        <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
          How reconciliation works
        </div>
        <div>
          <span className="font-mono text-[11px] text-accent">Direction A</span>
          : finds objects in object storage with no database record ("bare objects") and deletes them automatically after the grace period.
        </div>
        <div>
          <span className="font-mono text-[11px] text-accent">Direction B</span>
          : finds resources the database marks as present but that are missing from object storage ("ghost records"). These are only reported, not repaired — manual follow-up is required.
        </div>
        <div>
          <span className="font-mono text-[11px] text-amber-700">dry_run</span>
          : reports marked dry_run only count objects and delete nothing. Production reports should be dry_run=false.
        </div>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}
      {todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the orphan reconciliation endpoint is unreachable. Reports will appear here once it’s connected.
        </div>
      )}

      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}

      {!todo && !loading && items.length === 0 && (
        <div className="font-mono text-[12px] text-dim italic">No reconciliation reports</div>
      )}

      {!todo && items.length > 0 && (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Started
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Mode
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Grace period
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Direction A · Orphans
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Direction A · Deleted
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Direction B · Missing
                </th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Duration
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
                    {r.grace_period_days} days
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
            {loadingMore ? 'Loading…' : 'Load more'}
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
                Reconciliation report
              </div>
              <button
                onClick={() => setSelected(null)}
                className="text-[11px] font-mono text-dim hover:text-ink"
              >
                ✕
              </button>
            </div>
            <div className="text-[12px] text-ink font-mono">{selected.id}</div>

            <DetailRow label="Started" value={fmt(selected.started_at)} />
            <DetailRow label="Completed" value={fmt(selected.completed_at)} />
            <DetailRow label="Duration" value={fmtDuration(selected.duration_ms)} />
            <DetailRow
              label="Mode"
              value={selected.dry_run ? 'dry_run (count only)' : 'live (deletes objects)'}
            />
            <DetailRow label="Grace period" value={`${selected.grace_period_days} days`} />

            <div className="border-t border-line pt-2 mt-1" />

            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
              Direction A — Orphaned objects (S3 → DB)
            </div>
            <DetailRow label="Orphans found" value={String(selected.direction_a_orphans)} />
            <DetailRow label="Deleted" value={String(selected.direction_a_deleted)} />

            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mt-2">
              Direction B — Ghost records (DB → S3)
            </div>
            <DetailRow label="Missing objects" value={String(selected.direction_b_missing)} />

            <div className="flex justify-end mt-2">
              <button
                onClick={() => setSelected(null)}
                className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
              >
                Close
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
