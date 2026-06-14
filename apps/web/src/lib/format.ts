/**
 * Format an ISO timestamp (or Date) as a Chinese relative time string used
 * throughout the workspace UI: 刚刚 / N 分钟前 / N 小时前 / N 天前.
 *
 * Anything older than 30 days falls back to a yyyy-mm-dd date so the value
 * stays parseable when scanning long task lists.
 */
export function formatRelativeTimeZh(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '—';
  const diffMs = Date.now() - d.getTime();
  const sec = Math.max(0, Math.round(diffMs / 1000));
  if (sec < 60) return '刚刚';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟前`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} 小时前`;
  const day = Math.round(hr / 24);
  if (day < 30) return `${day} 天前`;
  return d.toISOString().slice(0, 10);
}

/**
 * Bucket a timestamp into a coarse recency group used to section the sidebar
 * session list (ChatGPT/Claude style): 今天 / 昨天 / 过去 7 天 / 过去 30 天 / 更早.
 * Buckets are calendar-day based (local time) so "昨天" means the previous
 * calendar day, not "24–48h ago".
 */
export type RecencyBucket = '今天' | '昨天' | '过去 7 天' | '过去 30 天' | '更早';

export function recencyBucketZh(value: string | Date | null | undefined): RecencyBucket {
  if (!value) return '更早';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '更早';
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const dayMs = 86_400_000;
  const diffDays = Math.floor((startOfToday.getTime() - d.getTime()) / dayMs);
  if (diffDays < 0) return '今天'; // future/clock-skew → treat as today
  if (diffDays === 0) return '今天';
  if (diffDays === 1) return '昨天';
  if (diffDays < 7) return '过去 7 天';
  if (diffDays < 30) return '过去 30 天';
  return '更早';
}

/** Fixed display order for recency buckets. */
export const RECENCY_BUCKET_ORDER: RecencyBucket[] = [
  '今天',
  '昨天',
  '过去 7 天',
  '过去 30 天',
  '更早',
];

/**
 * Format an ISO timestamp as an absolute local date-time string
 * (e.g. "2026-06-07 21:13:27"), used in hover tooltips.
 */
export function formatAbsoluteTimeZh(value: string | Date | null | undefined): string {
  if (!value) return '—';
  const d = typeof value === 'string' ? new Date(value) : value;
  if (Number.isNaN(d.getTime())) return '—';
  const p = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  );
}

/** Map session status enum from the API to a Chinese label. */
export function sessionStatusZh(status: string | null | undefined): string {
  switch (status) {
    case 'ACTIVE':
      // Session-level "open / can continue", NOT "a run is executing".
      // Deliberately distinct from the task-level live → "运行中" so a settled
      // session with no active run doesn't read as if it's burning compute.
      return '进行中';
    case 'PAUSED':
      return '已暂停';
    case 'COMPLETED':
      return '已完成';
    case 'ERROR':
      return '出错';
    case 'ARCHIVED':
      return '已归档';
    default:
      return status ?? '—';
  }
}

/** Map cloud-worker task status strings (live/thinking/…) to Chinese. */
export function taskStatusZh(status: string | null | undefined): string {
  switch (status) {
    case 'live':
      return '运行中';
    case 'thinking':
      return '思考中';
    case 'review':
      return '待审核';
    case 'awaiting':
      return '等待中';
    case 'paused':
      return '已暂停';
    case 'done':
      return '已完成';
    case 'queued':
      return '已排队';
    case 'idle':
      return '空闲';
    default:
      return status ?? '—';
  }
}
