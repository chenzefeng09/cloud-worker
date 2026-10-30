import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { Task, TaskListParams, TaskStatus } from '../api/types';

/** Statuses that change on their own — while any is visible, poll fast. */
export const ACTIVE_TASK_STATUSES: readonly TaskStatus[] = ['queued', 'running', 'needs_input'];

const FAST_POLL_MS = 4000;
const SLOW_POLL_MS = 30000;

const EMPTY_COUNTS: Record<TaskStatus, number> = {
  queued: 0, running: 0, needs_input: 0, completed: 0, failed: 0, cancelled: 0, idle: 0,
};

export function isActiveTask(task: Pick<Task, 'status'>): boolean {
  return ACTIVE_TASK_STATUSES.includes(task.status);
}

/**
 * The task board's data: one filtered page plus "load more", kept fresh by
 * polling (fast while something visible is in flight, slow otherwise, paused
 * while the tab is hidden).  `params` is compared by value, so callers can
 * pass a fresh object every render.
 */
export function useTasks(params: TaskListParams) {
  const [items, setItems] = useState<Task[]>([]);
  const [counts, setCounts] = useState<Record<TaskStatus, number>>(EMPTY_COUNTS);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = JSON.stringify(params);
  // Pages beyond the first that the user loaded; a poll refreshes the same
  // number of rows so "load more" results don't vanish on the next tick.
  const extraPagesRef = useRef(0);
  const requestSeq = useRef(0);

  const fetchPage = useCallback(async () => {
    const seq = ++requestSeq.current;
    const base: TaskListParams = JSON.parse(key);
    try {
      let page = await api.tasks.list(base);
      let rows = page.items;
      for (let i = 0; i < extraPagesRef.current && page.next_cursor; i += 1) {
        page = await api.tasks.list({ ...base, cursor: page.next_cursor });
        rows = rows.concat(page.items);
      }
      if (seq !== requestSeq.current) return; // a newer request superseded this one
      setItems(rows);
      setCursor(page.next_cursor);
      setCounts({ ...EMPTY_COUNTS, ...page.counts });
      setError(null);
    } catch (err) {
      if (seq !== requestSeq.current) return;
      setError(err instanceof ApiError ? err.message : 'Couldn’t load tasks.');
    } finally {
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [key]);

  // Filters changed → start over from the first page.
  useEffect(() => {
    extraPagesRef.current = 0;
    setLoading(true);
    void fetchPage();
  }, [fetchPage]);

  const anyActive = items.some(isActiveTask);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const schedule = () => {
      timer = setTimeout(async () => {
        if (document.visibilityState === 'visible') await fetchPage();
        schedule();
      }, anyActive ? FAST_POLL_MS : SLOW_POLL_MS);
    };
    schedule();
    const onVisible = () => {
      if (document.visibilityState === 'visible') void fetchPage();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [fetchPage, anyActive]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.tasks.list({ ...(JSON.parse(key) as TaskListParams), cursor });
      extraPagesRef.current += 1;
      setItems((prev) => {
        const seen = new Set(prev.map((t) => t.id));
        return prev.concat(page.items.filter((t) => !seen.has(t.id)));
      });
      setCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t load more tasks.');
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, key, loadingMore]);

  /** Replace one task in place (after retry/cancel) without waiting for a poll. */
  const upsert = useCallback((task: Task) => {
    setItems((prev) => {
      const idx = prev.findIndex((t) => t.id === task.id);
      if (idx === -1) return [task, ...prev];
      const next = prev.slice();
      next[idx] = task;
      return next;
    });
  }, []);

  const remove = useCallback((id: string) => {
    setItems((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return {
    items,
    counts,
    loading,
    loadingMore,
    error,
    hasMore: !!cursor,
    refresh: fetchPage,
    loadMore,
    upsert,
    remove,
  };
}

/**
 * Lightweight live count of tasks needing attention (running + waiting on the
 * user) for the sidebar badge.  One `limit=1` request every 20s while visible.
 */
export function useActiveTaskCount(): { running: number; needsInput: number } {
  const [counts, setCounts] = useState({ running: 0, needsInput: 0 });
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      if (document.visibilityState !== 'visible') return;
      try {
        const page = await api.tasks.list({ limit: 1 });
        if (!cancelled) {
          setCounts({
            running: (page.counts?.running ?? 0) + (page.counts?.queued ?? 0),
            needsInput: page.counts?.needs_input ?? 0,
          });
        }
      } catch {
        /* failure-open: the badge simply doesn't update */
      }
    };
    void tick();
    const timer = setInterval(tick, 20000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);
  return counts;
}
