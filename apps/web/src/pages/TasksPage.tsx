import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { useSessions } from '../hooks/useSessions';
import { formatRelativeTimeZh, sessionStatusZh } from '../lib/format';
import type { Session } from '../api/types';

const STATUS_ORDER: Session['status'][] = ['ACTIVE', 'PAUSED', 'COMPLETED', 'ERROR', 'ARCHIVED'];

/** Static progress placeholder per status — purely cosmetic, no backend data. */
function staticProgress(status: Session['status']): number {
  switch (status) {
    case 'ACTIVE':    return 0.6;
    case 'PAUSED':    return 0.4;
    case 'COMPLETED': return 1.0;
    case 'ERROR':     return 0.3;
    case 'ARCHIVED':  return 1.0;
  }
}

export function TasksPage() {
  const { sessions, loading, createSession, error } = useSessions();
  const navigate = useNavigate();

  const handleNew = async () => {
    const s = await createSession();
    if (s) navigate(`/tasks/${s.id}`);
  };

  const groups = STATUS_ORDER.map((status) => ({
    status,
    items: sessions.filter((s) => s.status === status),
  })).filter((g) => g.items.length > 0);

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页', to: '/home' }, { label: '任务' }]}
        actions={
          <button
            onClick={handleNew}
            disabled={loading}
            className="cw-btn cw-btn-accent cw-btn-sm disabled:opacity-50"
          >
            + 新建任务
          </button>
        }
      />

      <div className="cw-scroll flex-1 px-8 py-5" style={{ overflowX: 'auto' }}>
        <div style={{ minWidth: 700, maxWidth: 1080, display: 'flex', flexDirection: 'column', gap: 16 }}>
          {loading && sessions.length === 0 && (
            <div className="font-mono text-[11px] text-dim">加载中…</div>
          )}
          {error && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              加载任务失败：{error}
            </div>
          )}
          {!loading && sessions.length === 0 && !error && (
            <div className="font-mono italic text-dim text-[12px]">暂无任务</div>
          )}
          {groups.map((g) => (
            <div key={g.status}>
              <div className="cw-label" style={{ marginBottom: 8 }}>
                {sessionStatusZh(g.status)} · {g.items.length}
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                {g.items.map((s) => {
                  const progress = staticProgress(s.status);
                  const isActive = s.status === 'ACTIVE';
                  return (
                    <div
                      key={s.id}
                      onClick={() => navigate(`/tasks/${s.id}`)}
                      className="border border-line bg-surface rounded-md cursor-pointer hover:border-ink-2"
                      style={{
                        display: 'flex',
                        flexDirection: 'column',
                        gap: 8,
                        padding: '12px 14px',
                      }}
                    >
                      {/* Row 1: status dot + title + time */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <StatusDot status={sessionToDesignStatus(s.status)} />
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div
                            className="font-medium"
                            style={{
                              fontSize: 13.5,
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                            }}
                          >
                            {s.title || '未命名任务'}
                          </div>
                        </div>
                        <span className="font-mono text-dim" style={{ fontSize: 11, flexShrink: 0 }}>
                          {formatRelativeTimeZh(s.updated_at)}
                        </span>
                      </div>

                      {/* Row 2: session meta (replaces design's free-text summary) */}
                      <div className="text-dim" style={{ fontSize: 12 }}>
                        会话 {s.id.slice(0, 12)}
                      </div>

                      {/* Row 3: progress bar (static placeholder) */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                        <div
                          style={{
                            flex: 1,
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                          }}
                        >
                          <div
                            style={{
                              flex: 1,
                              height: 3,
                              background: 'var(--line-2)',
                              borderRadius: 2,
                              overflow: 'hidden',
                            }}
                          >
                            <div
                              style={{
                                width: `${progress * 100}%`,
                                height: '100%',
                                background: isActive ? 'var(--accent)' : 'var(--ink)',
                              }}
                            />
                          </div>
                          <span
                            className="font-mono text-dim"
                            style={{ fontSize: 11, width: 36, textAlign: 'right' }}
                          >
                            {Math.round(progress * 100)}%
                          </span>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
