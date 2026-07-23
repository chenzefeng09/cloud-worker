import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { RunCard, RUN_TERMINAL } from '../components/app/RunCard';
import { api, ApiError } from '../api/client';
import type { App, AppRunHistoryItem } from '../api/types';

// ─── App execution history ────────────────────────────────────────────────────
//
// Lists the runs triggered for a single App (GET /apps/{id}/runs). Each card
// surfaces status / time / source / inputs / outcome and links through to the
// full AppResultPage. While any visible run is still non-terminal the page
// auto-refreshes every 5s so a freshly-triggered run flips to "已完成" without
// a manual reload.

const PAGE_SIZE = 20;
const REFRESH_INTERVAL_MS = 5000;

export function AppHistoryPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [app, setApp] = useState<App | null>(null);
  const [items, setItems] = useState<AppRunHistoryItem[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingInitial, setLoadingInitial] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ─── Initial load: fetch the app (for the header name) + first runs page ────
  useEffect(() => {
    if (!id) {
      setError('未指定应用。');
      setLoadingInitial(false);
      return;
    }
    let alive = true;
    setLoadingInitial(true);
    setError(null);

    Promise.all([api.apps.get(id), api.apps.listRuns(id, { limit: PAGE_SIZE, offset: 0 })])
      .then(([a, resp]) => {
        if (!alive) return;
        setApp(a);
        setItems(resp.items);
        setOffset(resp.items.length);
        setHasMore(resp.items.length === PAGE_SIZE);
      })
      .catch((err) => {
        if (!alive) return;
        setError(err instanceof ApiError ? err.message : '加载执行历史失败');
      })
      .finally(() => {
        if (alive) setLoadingInitial(false);
      });

    return () => {
      alive = false;
    };
  }, [id]);

  // ─── Load more: append the next page ────────────────────────────────────────
  const handleLoadMore = useCallback(async () => {
    if (!id || loadingMore) return;
    setLoadingMore(true);
    try {
      const resp = await api.apps.listRuns(id, { limit: PAGE_SIZE, offset });
      setItems((prev) => [...prev, ...resp.items]);
      setOffset((prev) => prev + resp.items.length);
      setHasMore(resp.items.length === PAGE_SIZE);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '加载更多失败');
    } finally {
      setLoadingMore(false);
    }
  }, [id, offset, loadingMore]);

  // ─── Refresh: re-fetch the loaded range (used by the auto-refresh poller) ───
  // Re-fetches `items.length` (or PAGE_SIZE when empty) from offset 0 and
  // replaces the list so a RUNNING run's status flips without a manual reload.
  // New runs created during polling surface at the top (newest-first ordering).
  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      const limit = Math.max(items.length, PAGE_SIZE);
      const resp = await api.apps.listRuns(id, { limit, offset: 0 });
      setItems(resp.items);
      setOffset(resp.items.length);
      // If the refresh returned fewer rows than we had loaded, the tail is
      // gone (deleted / no more). Otherwise preserve the prior hasMore.
      setHasMore((prev) => (resp.items.length < limit ? false : prev));
    } catch {
      /* swallow polling errors - the next tick will retry */
    }
  }, [id, items.length]);

  // ─── Auto-refresh while any visible run is non-terminal ─────────────────────
  const hasActiveRun = items.some((r) => !RUN_TERMINAL.has(r.status));
  useEffect(() => {
    if (!hasActiveRun) {
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
      return;
    }
    refreshTimerRef.current = setInterval(refresh, REFRESH_INTERVAL_MS);
    return () => {
      if (refreshTimerRef.current) {
        clearInterval(refreshTimerRef.current);
        refreshTimerRef.current = null;
      }
    };
  }, [hasActiveRun, refresh]);

  const handleOpen = (run: AppRunHistoryItem) => {
    navigate(`/apps/runs/${run.session_id}/${run.id}`);
  };

  const crumbs = [
    { label: '首页', to: '/home' },
    { label: '应用', to: '/apps' },
    { label: app?.name || '应用', to: id ? `/apps/${id}` : undefined },
    { label: '执行历史' },
  ];

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={crumbs}
        actions={
          id ? (
            <button
              type="button"
              onClick={() => navigate(`/apps/${id}`)}
              className="cw-btn cw-btn-ghost cw-btn-sm"
            >
              返回应用
            </button>
          ) : undefined
        }
      />

      <div className="cw-scroll flex-1 min-h-0 overflow-auto px-[5vw] py-6">
        <div className="w-[min(880px,100%)] mx-auto flex flex-col gap-4">
          {/* Header */}
          <div className="flex items-center gap-2">
            <h1 className="m-0 font-display font-normal tracking-[-0.01em] text-[20px] leading-[1.2]">
              执行历史
            </h1>
            {app && (
              <span className="text-[13px] text-dim truncate">· {app.name}</span>
            )}
          </div>

          {/* Error */}
          {error && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {error}
            </div>
          )}

          {/* Loading */}
          {loadingInitial && !error && (
            <div className="font-mono text-[12px] text-dim">加载中…</div>
          )}

          {/* Empty */}
          {!loadingInitial && !error && items.length === 0 && (
            <div className="border border-line border-dashed bg-surface rounded-md p-6 flex flex-col gap-3 items-center justify-center min-h-[200px]">
              <div className="font-mono italic text-dim text-[12.5px]">还没有执行记录</div>
              {id && (
                <button
                  type="button"
                  onClick={() => navigate(`/apps/${id}/run`)}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                >
                  运行此应用
                </button>
              )}
            </div>
          )}

          {/* Run list */}
          {!loadingInitial && items.length > 0 && (
            <div className="flex flex-col gap-2.5">
              {items.map((run) => (
                <RunCard key={run.id} run={run} onOpen={handleOpen} />
              ))}
            </div>
          )}

          {/* Load more */}
          {!loadingInitial && items.length > 0 && hasMore && (
            <div className="flex justify-center pt-2">
              <button
                type="button"
                onClick={handleLoadMore}
                disabled={loadingMore}
                className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-50"
              >
                {loadingMore ? '加载中…' : '加载更多'}
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
