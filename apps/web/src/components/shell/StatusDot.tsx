import type { Session, TaskStatus } from '../../api/types';

export type DesignStatus =
  | 'live'
  | 'thinking'
  | 'review'
  | 'awaiting'
  | 'paused'
  | 'queued'
  | 'idle'
  | 'done'
  | 'failed'
  | 'cancelled';

interface StatusConfig {
  c: string;
  anim: boolean;
  l: string;
}

const STATUS_MAP: Record<DesignStatus, StatusConfig> = {
  live:     { c: '#d23a1f', anim: true,  l: 'live' },
  thinking: { c: '#d23a1f', anim: true,  l: 'thinking' },
  review:   { c: '#c68a17', anim: false, l: 'review' },
  awaiting: { c: '#c68a17', anim: false, l: 'awaiting' },
  paused:   { c: '#75756f', anim: false, l: 'paused' },
  done:     { c: '#2c8f5d', anim: false, l: 'done' },
  queued:   { c: '#75756f', anim: false, l: 'queued' },
  idle:     { c: '#75756f', anim: false, l: 'idle' },
  failed:   { c: '#d23a1f', anim: false, l: 'failed' },
  cancelled:{ c: '#75756f', anim: false, l: 'cancelled' },
};

export function StatusDot({
  status,
  label,
}: {
  status: DesignStatus;
  label?: string;
}) {
  const s = STATUS_MAP[status] ?? STATUS_MAP.idle;
  const text = label ?? s.l;

  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span
        className={`cw-dot${s.anim ? ' animate-blip' : ''}`}
        style={{ background: s.c }}
      />
      <span className="cw-label" style={{ fontSize: 11 }}>
        {text}
      </span>
    </span>
  );
}

/** Task board status → dot. */
export function taskToDesignStatus(s: TaskStatus): DesignStatus {
  switch (s) {
    case 'running':     return 'live';
    case 'queued':      return 'queued';
    case 'needs_input': return 'awaiting';
    case 'completed':   return 'done';
    case 'failed':      return 'failed';
    case 'cancelled':   return 'cancelled';
    case 'idle':        return 'idle';
  }
}

export function sessionToDesignStatus(s: Session['status']): DesignStatus {
  switch (s) {
    case 'ACTIVE':    return 'thinking';
    case 'COMPLETED': return 'done';
    case 'PAUSED':    return 'paused';
    case 'ERROR':     return 'awaiting';
    case 'ARCHIVED':  return 'idle';
  }
}
