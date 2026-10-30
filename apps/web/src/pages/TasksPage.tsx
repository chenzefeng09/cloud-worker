import { useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import type { Task, TaskKind, TaskStatus } from '../api/types';
import { PageHead } from '../components/shell/PageHead';
import { TaskCard } from '../components/tasks/TaskCard';
import { CreateTaskDialog } from '../components/tasks/CreateTaskDialog';
import { useTasks } from '../hooks/useTasks';
import { useWorkspace } from '../lib/workspaceContext';

interface FilterTab {
  id: string;
  label: string;
  statuses?: TaskStatus[];
}

const TABS: FilterTab[] = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active', statuses: ['queued', 'running'] },
  { id: 'needs_input', label: 'Needs input', statuses: ['needs_input'] },
  { id: 'completed', label: 'Completed', statuses: ['completed'] },
  { id: 'failed', label: 'Failed', statuses: ['failed'] },
  // Stopped on purpose, so not a failure.
  { id: 'cancelled', label: 'Cancelled', statuses: ['cancelled'] },
];

const KIND_FILTERS: { id: string; label: string; kinds?: TaskKind[] }[] = [
  { id: 'all', label: 'Everything' },
  { id: 'task', label: 'Tasks', kinds: ['task'] },
  { id: 'chat', label: 'Chats', kinds: ['chat'] },
  { id: 'app_run', label: 'App runs', kinds: ['app_run'] },
];

/**
 * Task board — every piece of agentic work the user started, with its live
 * lifecycle (queued → running → needs input → completed / failed).  Tasks run
 * in their sandbox in the background; this page only polls their state.
 */
export function TasksPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabId = searchParams.get('status') ?? 'all';
  const kindId = searchParams.get('kind') ?? 'all';
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [creating, setCreating] = useState(searchParams.get('new') === '1');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'info' | 'error'; text: string } | null>(null);
  const { workspaces } = useWorkspace();

  const tab = TABS.find((t) => t.id === tabId) ?? TABS[0];
  const kindFilter = KIND_FILTERS.find((k) => k.id === kindId) ?? KIND_FILTERS[0];
  const params = useMemo(
    () => ({
      status: tab.statuses,
      kind: kindFilter.kinds,
      q: appliedQuery || undefined,
      limit: 30,
    }),
    [tab, kindFilter, appliedQuery],
  );
  const { items, counts, loading, loadingMore, error, hasMore, refresh, loadMore, upsert, remove } =
    useTasks(params);

  const wsName = useMemo(() => {
    const m = new Map<string, string>();
    for (const w of workspaces) m.set(w.id, w.name);
    return m;
  }, [workspaces]);

  const setFilter = (key: 'status' | 'kind', value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value === 'all') next.delete(key);
    else next.set(key, value);
    next.delete('new');
    setSearchParams(next, { replace: true });
  };

  const tabCount = (t: FilterTab) =>
    t.statuses
      ? t.statuses.reduce((sum, s) => sum + (counts[s] ?? 0), 0)
      : Object.values(counts).reduce((sum, n) => sum + n, 0);

  const openTask = (task: Task) => {
    if (task.kind === 'app_run' && task.latest_run) {
      navigate(`/apps/runs/${task.id}/${task.latest_run.id}`);
      return;
    }
    navigate(`/tasks/${task.id}`);
  };

  const withBusy = async (task: Task, fn: () => Promise<void>) => {
    setBusyId(task.id);
    setNotice(null);
    try {
      await fn();
    } catch (err) {
      setNotice({
        tone: 'error',
        text: err instanceof ApiError ? err.message : 'Something went wrong. Please try again.',
      });
    } finally {
      setBusyId(null);
    }
  };

  const retry = (task: Task) => {
    if (task.status === 'completed'
        && !window.confirm(`Run “${task.title || 'this task'}” again? Its current result will be replaced.`)) {
      return Promise.resolve();
    }
    return withBusy(task, async () => {
      const { task: updated } = await api.tasks.retry(task.id);
      upsert(updated);
      setNotice({ tone: 'info', text: `“${updated.title || 'Task'}” is running again.` });
    });
  };

  const cancel = (task: Task) =>
    withBusy(task, async () => {
      await api.sessions.cancel(task.id);
      upsert(await api.tasks.get(task.id));
    });

  const del = (task: Task) => {
    if (!window.confirm(`Delete “${task.title || 'Untitled task'}”? Its conversation and files will be removed.`)) {
      return;
    }
    void withBusy(task, async () => {
      await api.sessions.delete(task.id);
      remove(task.id);
    });
  };

  const onCreated = (task: Task | null, startError?: string) => {
    if (task) {
      upsert(task);
      setNotice({ tone: 'info', text: `“${task.title || 'Task'}” started. It keeps running if you leave this page.` });
    } else {
      setNotice({
        tone: 'error',
        text: `The task was saved but couldn’t start${startError ? ` (${startError})` : ''}. Use Start on its card to try again.`,
      });
    }
    void refresh();
  };

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: 'Tasks' }]}
        actions={
          <button type="button" className="cw-btn cw-btn-primary cw-btn-sm" onClick={() => setCreating(true)}>
            <span className="font-mono">+</span> New task
          </button>
        }
      />

      <div className="flex flex-col gap-3 px-6 pt-4 pb-2 border-b border-line shrink-0">
        <div role="tablist" aria-label="Task status" className="flex items-center gap-1 flex-wrap">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={t.id === tab.id}
              onClick={() => setFilter('status', t.id)}
              className={[
                'px-2.5 py-1 rounded-sm text-[12.5px] border',
                t.id === tab.id
                  ? 'bg-surface border-line text-ink font-medium'
                  : 'border-transparent text-ink-2 hover:bg-paper-2',
              ].join(' ')}
            >
              {t.label}
              <span className="ml-1.5 font-mono text-[10.5px] text-dim">{tabCount(t)}</span>
            </button>
          ))}
          <div className="flex-1" />
          <select
            aria-label="Kind"
            className="border border-line rounded-sm px-2 py-1 text-[12.5px] bg-paper"
            value={kindFilter.id}
            onChange={(e) => setFilter('kind', e.target.value)}
          >
            {KIND_FILTERS.map((k) => (
              <option key={k.id} value={k.id}>{k.label}</option>
            ))}
          </select>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setAppliedQuery(query.trim());
            }}
          >
            <input
              type="search"
              aria-label="Search tasks"
              className="border border-line rounded-sm px-2 py-1 text-[12.5px] bg-paper w-[180px]"
              placeholder="Search titles…"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                if (!e.target.value) setAppliedQuery('');
              }}
            />
          </form>
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto cw-scroll px-6 py-4">
        {notice && (
          <div
            role={notice.tone === 'error' ? 'alert' : 'status'}
            className={[
              'mb-3 px-3 py-2 rounded-md border text-[12.5px] flex items-center gap-2',
              notice.tone === 'error'
                ? 'border-accent-line bg-accent-soft text-accent'
                : 'border-line bg-surface text-ink-2',
            ].join(' ')}
          >
            <span className="flex-1">{notice.text}</span>
            <button type="button" aria-label="Dismiss" className="font-mono text-[11px] text-dim hover:text-ink" onClick={() => setNotice(null)}>
              ✕
            </button>
          </div>
        )}
        {error && (
          <div role="alert" className="mb-3 px-3 py-2 rounded-md border border-accent-line bg-accent-soft text-accent text-[12.5px]">
            {error}
          </div>
        )}

        {loading && items.length === 0 ? (
          <div className="font-mono italic text-dim text-[12px]">Loading tasks…</div>
        ) : items.length === 0 ? (
          <div className="border border-dashed border-line rounded-md p-8 flex flex-col items-center gap-3 text-center">
            <div className="text-[14px] font-medium">
              {tab.id === 'all' && kindFilter.id === 'all' && !appliedQuery ? 'No tasks yet' : 'Nothing matches these filters'}
            </div>
            <div className="text-[12.5px] text-dim max-w-[420px]">
              Describe a goal, attach files, and the agent works on it in its own sandbox while you do something else.
            </div>
            <button type="button" className="cw-btn cw-btn-primary cw-btn-sm" onClick={() => setCreating(true)}>
              <span className="font-mono">+</span> New task
            </button>
          </div>
        ) : (
          <div role="list" className="grid grid-cols-1 xl:grid-cols-2 gap-3">
            {items.map((t) => (
              <TaskCard
                key={t.id}
                task={t}
                workspaceName={t.workspace_id ? wsName.get(t.workspace_id) : undefined}
                busy={busyId === t.id}
                onOpen={openTask}
                onRetry={(task) => void retry(task)}
                onCancel={(task) => void cancel(task)}
                onDelete={del}
              />
            ))}
          </div>
        )}

        {hasMore && (
          <div className="flex justify-center pt-4">
            <button
              type="button"
              className="cw-btn cw-btn-ghost cw-btn-sm"
              onClick={() => void loadMore()}
              disabled={loadingMore}
            >
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          </div>
        )}
      </div>

      <CreateTaskDialog
        open={creating}
        onClose={() => setCreating(false)}
        onCreated={onCreated}
      />
    </div>
  );
}
