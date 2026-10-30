import type { TaskStatus } from '../api/types';

/**
 * Format an ISO timestamp (or Date) as a short relative time string used
 * throughout the workspace UI: just now / 5 min ago / 3 hr ago / 2 days ago.
 *
 * Anything older than 30 days falls back to a yyyy-mm-dd date so the value
 * stays parseable when scanning long task lists.
 */
export function formatRelativeTime(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '—';
  const diffMs = Date.now() - d.getTime();
  const sec = Math.max(0, Math.round(diffMs / 1000));
  if (sec < 60) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} hr ago`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} ${day === 1 ? 'day' : 'days'} ago`;
  return d.toISOString().slice(0, 10);
}

/**
 * Bucket a timestamp into a coarse recency group used to section the sidebar
 * session list (ChatGPT/Claude style): Today / Yesterday / Previous 7 days /
 * Previous 30 days / Older. Buckets are calendar-day based (local time) so
 * "Yesterday" means the previous calendar day, not "24–48h ago".
 */
export type RecencyBucket = 'Today' | 'Yesterday' | 'Previous 7 days' | 'Previous 30 days' | 'Older';

export function recencyBucket(value: string | Date | null | undefined): RecencyBucket {
  if (!value) return 'Older';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return 'Older';
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const dayMs = 86_400_000;
  const diffDays = Math.floor((startOfToday.getTime() - d.getTime()) / dayMs);
  if (diffDays < 0) return 'Today'; // future/clock-skew → treat as today
  if (diffDays === 0) return 'Today';
  if (diffDays === 1) return 'Yesterday';
  if (diffDays < 7) return 'Previous 7 days';
  if (diffDays < 30) return 'Previous 30 days';
  return 'Older';
}

/** Fixed display order for recency buckets. */
export const RECENCY_BUCKET_ORDER: RecencyBucket[] = [
  'Today',
  'Yesterday',
  'Previous 7 days',
  'Previous 30 days',
  'Older',
];

/**
 * Format an ISO timestamp as an absolute local date-time string
 * (e.g. "2026-06-07 21:13:27"), used in hover tooltips.
 */
export function formatAbsoluteTime(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

/**
 * Who a user id stands for, for display.  `name` is the display name the BFF
 * resolves beside the id; the caller is marked "(you)".  An id with no name is
 * an erased user's placeholder (not a `usr_` id) or one identity could not
 * resolve — neither is worth showing raw.
 */
export function personLabel(
  userId: string | null | undefined,
  name: string | null | undefined,
  currentUserId?: string | null,
): string {
  if (!userId) return '—';
  const isMe = Boolean(currentUserId) && userId === currentUserId;
  if (name) return isMe ? `${name} (you)` : name;
  if (isMe) return 'You';
  return userId.startsWith('usr_') ? 'Unknown user' : 'Deleted user';
}

/** Map session status enum from the API to a display label. */
export function sessionStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'ACTIVE':
      // Session-level "open / can continue", NOT "a run is executing".
      // Deliberately distinct from the task-level live → "Running" so a settled
      // session with no active run doesn't read as if it's burning compute.
      return 'In progress';
    case 'PAUSED':
      return 'Paused';
    case 'COMPLETED':
      return 'Completed';
    case 'ERROR':
      return 'Error';
    case 'ARCHIVED':
      return 'Archived';
    default:
      return status ?? '—';
  }
}

/** Map cloud-worker task status strings (live/thinking/…) to display labels. */
export function taskStatusLabel(status: string | null | undefined): string {
  switch (status) {
    case 'live':
      return 'Running';
    case 'thinking':
      return 'Thinking';
    case 'review':
      return 'In review';
    case 'awaiting':
      return 'Waiting';
    case 'paused':
      return 'Paused';
    case 'done':
      return 'Completed';
    case 'queued':
      return 'Queued';
    case 'idle':
      return 'Idle';
    default:
      return status ?? '—';
  }
}

/** Task board status (server vocabulary) → display label. */
export function boardStatusLabel(status: TaskStatus): string {
  switch (status) {
    case 'queued':
      return 'Queued';
    case 'running':
      return 'Running';
    case 'needs_input':
      return 'Needs input';
    case 'completed':
      return 'Completed';
    case 'failed':
      return 'Failed';
    case 'cancelled':
      return 'Cancelled';
    case 'idle':
      return 'Not started';
  }
}

/**
 * Compact elapsed time between two instants ("42s", "3m 05s", "1h 12m").
 * `end` defaults to now so a running task's clock keeps ticking.
 */
export function formatElapsed(
  start: string | Date | null | undefined,
  end?: string | Date | null,
): string {
  if (!start) return '—';
  const s = typeof start === 'string' ? new Date(start) : start;
  const e = end ? (typeof end === 'string' ? new Date(end) : end) : new Date();
  const secs = Math.max(0, Math.round((e.getTime() - s.getTime()) / 1000));
  if (Number.isNaN(secs)) return '—';
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${String(secs % 60).padStart(2, '0')}s`;
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;
}
