/**
 * Format an ISO timestamp (or Date) as a Chinese relative time string used
 * throughout the workspace UI: 刚刚 / N 分钟前 / N 小时前 / N 天前.
 *
 * Anything older than 30 days falls back to a yyyy-mm-dd date so the value
 * stays parseable when scanning long task lists.
 */
export function formatRelativeTimeZh(value) {
    if (!value)
        return '—';
    const d = typeof value === 'string' ? new Date(value) : value;
    if (Number.isNaN(d.getTime()))
        return '—';
    const diffMs = Date.now() - d.getTime();
    const sec = Math.max(0, Math.round(diffMs / 1000));
    if (sec < 60)
        return '刚刚';
    const min = Math.round(sec / 60);
    if (min < 60)
        return `${min} 分钟前`;
    const hr = Math.round(min / 60);
    if (hr < 24)
        return `${hr} 小时前`;
    const day = Math.round(hr / 24);
    if (day < 30)
        return `${day} 天前`;
    return d.toISOString().slice(0, 10);
}
/** Map session status enum from the API to a Chinese label. */
export function sessionStatusZh(status) {
    switch (status) {
        case 'ACTIVE':
            return '运行中';
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
export function taskStatusZh(status) {
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
