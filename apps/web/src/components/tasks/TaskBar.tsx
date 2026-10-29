import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ApiError } from '../../api/client';
import type { Task } from '../../api/types';
import { boardStatusLabel, formatElapsed } from '../../lib/format';
import { isActiveTask } from '../../hooks/useTasks';
import { useChatStore } from '../../stores/chatStore';
import { StatusDot, taskToDesignStatus } from '../shell/StatusDot';

const ACTIVE_POLL_MS = 4000;
const IDLE_POLL_MS = 30000;

/**
 * Lifecycle strip shown above the transcript of a background task: status,
 * attempt count, elapsed time and the Retry / Cancel controls.  Renders
 * nothing for ordinary chats (`kind !== 'task'`).
 */
export function TaskBar({ sessionId }: { sessionId: string }) {
  const navigate = useNavigate();
  const [task, setTask] = useState<Task | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Refresh as soon as this session's run starts or ends, not only on a tick.
  const live = useChatStore((s) => Boolean(s.streamingSessions[sessionId]));
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const next = await api.tasks.get(sessionId);
      if (mine === seq.current) setTask(next);
    } catch {
      /* not a task / transient — keep whatever we showed last */
    }
  }, [sessionId]);

  useEffect(() => {
    setTask(null);
    setError(null);
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load, live]);

  const isTask = task?.kind === 'task';
  const active = task ? isActiveTask(task) : false;
  useEffect(() => {
    if (!isTask) return;
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void load();
    }, active ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    return () => clearInterval(timer);
  }, [isTask, active, load]);

  if (!task || !isTask) return null;

  const run = task.latest_run;
  const elapsed = run?.started_at ? formatElapsed(run.started_at, active ? null : run.finished_at) : null;

  const retry = async () => {
    if (task.status === 'completed'
        && !window.confirm('Run this task again? Its current result will be replaced.')) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await api.tasks.retry(task.id);
      const store = useChatStore.getState();
      // Drop the cleared attempt locally; the new run streams in on its own.
      if (typeof res.rewound_from === 'number') store.truncateFromSequence(task.id, res.rewound_from);
      store.setSessionStreaming(task.id, true);
      setTask(res.task);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t restart the task.');
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.sessions.cancel(task.id);
      useChatStore.getState().setSessionStreaming(task.id, false);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Couldn’t cancel the task.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      data-testid="task-bar"
      className="flex items-center gap-3 px-6 py-2 border-b border-line bg-paper text-[12.5px] shrink-0 min-w-0"
    >
      <StatusDot status={taskToDesignStatus(task.status)} label={boardStatusLabel(task.status)} />
      <span className="font-mono text-[10.5px] text-dim truncate">
        {task.run_count > 1 ? `Attempt ${task.run_count}` : task.run_count === 1 ? 'First attempt' : 'Not started'}
        {elapsed ? ` · ${elapsed}` : ''}
        {run?.total_tokens ? ` · ${run.total_tokens.toLocaleString()} tokens` : ''}
        {task.options?.hitl === false ? ' · unattended' : ''}
      </span>
      {task.status === 'needs_input' && (
        <span className="text-accent truncate">Waiting for your answer below.</span>
      )}
      {task.status === 'failed' && run?.error_message && (
        <span className="text-accent truncate" title={run.error_message}>{run.error_message}</span>
      )}
      {error && <span role="alert" className="text-accent truncate">{error}</span>}
      <div className="flex-1" />
      {active ? (
        <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" disabled={busy} onClick={() => void cancel()}>
          Cancel
        </button>
      ) : (
        <button
          type="button"
          className="cw-btn cw-btn-ghost cw-btn-sm"
          disabled={busy}
          onClick={() => void retry()}
          title={task.run_count === 0 ? 'Start this task' : 'Run the latest attempt again in a fresh sandbox'}
        >
          {task.run_count === 0 ? 'Start' : 'Retry'}
        </button>
      )}
      <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={() => navigate('/tasks')}>
        All tasks
      </button>
    </div>
  );
}
