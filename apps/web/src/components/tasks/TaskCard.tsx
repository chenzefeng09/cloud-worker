import type { Task } from '../../api/types';
import { boardStatusLabel, formatElapsed, formatRelativeTime } from '../../lib/format';
import { isActiveTask } from '../../hooks/useTasks';
import { Chip } from '../shell/Chip';
import { StatusDot, taskToDesignStatus } from '../shell/StatusDot';

const KIND_LABEL: Record<Task['kind'], string> = {
  task: 'Task',
  chat: 'Chat',
  app_run: 'App',
};

interface TaskCardProps {
  task: Task;
  /** Workspace name, when the task belongs to one. */
  workspaceName?: string;
  busy?: boolean;
  onOpen: (task: Task) => void;
  onRetry: (task: Task) => void;
  onCancel: (task: Task) => void;
  onDelete: (task: Task) => void;
}

/** One row of the task board: status, what it was asked, how it ended. */
export function TaskCard({
  task, workspaceName, busy, onOpen, onRetry, onCancel, onDelete,
}: TaskCardProps) {
  const run = task.latest_run;
  const active = isActiveTask(task);
  const canRetry = !active && (task.run_count > 0 || task.kind === 'task');
  const outcome =
    task.status === 'failed' ? run?.error_message
    : task.status === 'completed' ? run?.result_summary
    : null;
  const elapsed = run?.started_at
    ? formatElapsed(run.started_at, active ? null : run.finished_at)
    : null;

  return (
    <div
      role="listitem"
      data-testid={`task-${task.id}`}
      className="border border-line bg-surface rounded-md p-3.5 flex flex-col gap-2 hover:border-ink-2 transition-colors"
    >
      <div className="flex items-center gap-2 min-w-0">
        <StatusDot status={taskToDesignStatus(task.status)} label={boardStatusLabel(task.status)} />
        <Chip>{KIND_LABEL[task.kind]}</Chip>
        {run?.source === 'schedule' && <Chip>Scheduled</Chip>}
        {task.options?.hitl === false && <Chip>Unattended</Chip>}
        {workspaceName && <span className="font-mono text-[10.5px] text-dim truncate">{workspaceName}</span>}
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-dim shrink-0" title={task.updated_at}>
          {formatRelativeTime(task.updated_at)}
        </span>
      </div>

      <button
        type="button"
        onClick={() => onOpen(task)}
        className="text-left text-[14px] font-semibold tracking-[-0.01em] leading-[1.3] truncate hover:text-accent"
      >
        {task.title || 'Untitled task'}
      </button>

      {task.instructions && (
        <div className="text-[12.5px] text-ink-2 leading-[1.45] line-clamp-2 whitespace-pre-line">
          {task.instructions}
        </div>
      )}

      {task.status === 'needs_input' && (
        <div className="text-[12px] text-accent">
          Waiting for your answer or approval — open the task to respond.
        </div>
      )}
      {outcome && (
        <div
          className={`text-[12px] leading-[1.45] line-clamp-2 ${
            task.status === 'failed' ? 'text-accent' : 'text-dim'
          }`}
        >
          {outcome}
        </div>
      )}

      <div className="flex items-center gap-2 pt-0.5">
        <span className="font-mono text-[10.5px] text-dim">
          {task.run_count > 1 ? `${task.run_count} attempts` : task.run_count === 1 ? '1 attempt' : 'No runs yet'}
          {elapsed ? ` · ${elapsed}` : ''}
          {run?.total_tokens ? ` · ${run.total_tokens.toLocaleString()} tokens` : ''}
        </span>
        <div className="flex-1" />
        {active && (
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            disabled={busy}
            onClick={() => onCancel(task)}
          >
            Cancel
          </button>
        )}
        {canRetry && (
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            disabled={busy}
            onClick={() => onRetry(task)}
            title={task.run_count === 0 ? 'Start this task' : 'Run the latest attempt again in a fresh sandbox'}
          >
            {task.run_count === 0 ? 'Start' : 'Retry'}
          </button>
        )}
        {!active && (
          <button
            type="button"
            className="cw-btn cw-btn-ghost cw-btn-sm"
            disabled={busy}
            onClick={() => onDelete(task)}
          >
            Delete
          </button>
        )}
        <button type="button" className="cw-btn cw-btn-sm" onClick={() => onOpen(task)}>
          Open →
        </button>
      </div>
    </div>
  );
}
