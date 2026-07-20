import { NavLink } from 'react-router-dom';
import { useChatStore } from '../../stores/chatStore';
import { useSessions } from '../../hooks/useSessions';
import { AccountMenu } from './AccountMenu';
import { canAccessAdmin } from '../../admin/AdminLayout';
import {
  RECENCY_BUCKET_ORDER,
  recencyBucketZh,
  formatAbsoluteTimeZh,
} from '../../lib/format';
import type { Session } from '../../api/types';

interface NavSpec {
  to: string;
  label: string;
  glyph: string;
  count?: string;
}

const NAV: NavSpec[] = [
  { to: '/home', label: '新任务', glyph: '◇' },
  { to: '/schedules', label: '定时任务', glyph: '◷' },
  // The workspace management page (base files + instructions + skills). This is
  // where the retired top-of-sidebar WorkspaceSwitcher's "管理工作区" link used
  // to lead; the base-files nav now owns that entry point.
  { to: '/workspace', label: '基础文件', glyph: '⌗' },
  { to: '/library', label: '资源库', glyph: '✦' },
  { to: '/artifacts', label: '产物', glyph: '◈' },
];

export function Sidebar() {
  const { sessions, loadMore, loadingMore, hasMore } = useSessions();
  const userProfile = useChatStore((s) => s.userProfile);

  const devBuildLabel = import.meta.env.DEV
    ? `dev ${import.meta.env.VITE_DEV_BUILD_STAMP ?? 'local'}`
    : null;

  // Section the recent sessions into calendar-recency buckets (ChatGPT/Claude
  // style) so a long list reads as 今天 / 昨天 / 过去 7 天 / … instead of one
  // undifferentiated wall. `sessions` already arrives newest-first from the
  // store, so each bucket preserves that order.
  const recentGroups = RECENCY_BUCKET_ORDER.map((bucket) => ({
    bucket,
    items: sessions.filter((s) => recencyBucketZh(s.updated_at) === bucket),
  })).filter((g) => g.items.length > 0);

  return (
    <aside className="w-[220px] shrink-0 border-r border-line p-3.5 flex flex-col gap-5 bg-paper overflow-hidden">
      {/* brand */}
      <NavLink to="/home" className="flex items-center gap-2.5 px-1 no-underline">
        <div className="relative w-[22px] h-[22px] bg-ink rounded-sm">
          <span className="absolute inset-[5px] border-[1.25px] border-paper rounded-[1px]" />
        </div>
        <div className="font-mono text-[12px] font-semibold tracking-[-0.01em] text-ink">
          cloud<em className="not-italic text-accent font-medium">·</em>worker
        </div>
      </NavLink>

      <nav className="flex-1 min-h-0 flex flex-col gap-0.5">
        <div className="flex flex-col gap-0.5 shrink-0">
        {NAV.map((n) => {
          const count = n.count;
          return (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) =>
                [
                  'relative flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-[13px] tracking-[-0.01em] border select-none cursor-pointer no-underline',
                  isActive
                    ? 'bg-surface border-line text-ink'
                    : 'text-ink-2 border-transparent hover:bg-paper-2 hover:text-ink',
                ].join(' ')
              }
              end={n.to === '/home'}
            >
              {({ isActive }) => (
                <>
                  {isActive && (
                    <span className="absolute -left-px top-2 bottom-2 w-0.5 bg-accent rounded-[1px]" />
                  )}
                  <span
                    className={`w-4 text-center font-mono text-[11px] ${
                      isActive ? 'text-accent' : 'text-dim'
                    }`}
                  >
                    {n.glyph}
                  </span>
                  <span className="flex-1 truncate">{n.label}</span>
                  {count && (
                    <span className="ml-auto font-mono text-[10px] text-dim">{count}</span>
                  )}
                </>
              )}
            </NavLink>
          );
        })}
        </div>

        <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim px-2 pt-3 pb-0.5 shrink-0">
          最近
        </div>
        {sessions.length === 0 && (
          <div className="font-mono italic text-dim text-[11px] px-2.5 py-1 shrink-0">暂无任务</div>
        )}
        <div className="flex-1 min-h-0 overflow-y-auto cw-scroll -mx-0.5 px-0.5">
          {recentGroups.map((g) => (
            <div key={g.bucket} className="mb-1.5">
              <div className="text-[10px] text-dim px-2 pt-1.5 pb-1 select-none">{g.bucket}</div>
              <div className="flex flex-col gap-px">
                {g.items.map((s) => (
                  <RecentItem key={s.id} session={s} />
                ))}
              </div>
            </div>
          ))}
          {hasMore && (
            <button
              onClick={loadMore}
              disabled={loadingMore}
              className="mt-1 w-full px-2 py-1.5 text-left font-mono text-[10px] text-dim hover:text-ink-2 disabled:opacity-50"
            >
              {loadingMore ? '加载中…' : '显示更多'}
            </button>
          )}
        </div>
      </nav>

      {/* Admin entry — visible to anyone with at least one admin permission */}
      {canAccessAdmin(userProfile?.permissions) && (
        <div className="pt-2 border-t border-line-2">
          <NavLink
            to="/admin"
            className={({ isActive }) =>
              [
                'relative flex items-center gap-2.5 px-2 py-1.5 rounded-sm text-[13px] tracking-[-0.01em] border select-none cursor-pointer no-underline',
                isActive
                  ? 'bg-surface border-line text-ink'
                  : 'text-ink-2 border-transparent hover:bg-paper-2 hover:text-ink',
              ].join(' ')
            }
          >
            {({ isActive }) => (
              <>
                {isActive && (
                  <span className="absolute -left-px top-2 bottom-2 w-0.5 bg-accent rounded-[1px]" />
                )}
                <span
                  className={`w-4 text-center font-mono text-[11px] ${
                    isActive ? 'text-accent' : 'text-dim'
                  }`}
                >
                  ⊞
                </span>
                <span className="flex-1 truncate">管理后台</span>
              </>
            )}
          </NavLink>
        </div>
      )}

      <div className="mt-auto flex flex-col gap-2 pt-3.5 border-t border-line">
        <AccountMenu />
        {devBuildLabel && (
          <div className="pl-[30px] font-mono text-[9px] text-dim" title="Frontend dev build stamp">
            {devBuildLabel}
          </div>
        )}
      </div>
    </aside>
  );
}

/**
 * A single recent-session row in the sidebar. Title-only (ChatGPT/Claude
 * style) — no status glyph, since a wall of coloured dots reads as noise once
 * the list is long. The session's last-updated time is exposed as a hover
 * tooltip instead.
 */
function RecentItem({ session }: { session: Session }) {
  return (
    <NavLink
      to={`/tasks/${session.id}`}
      title={formatAbsoluteTimeZh(session.updated_at)}
      className={({ isActive }) =>
        [
          'block px-2 py-1.5 rounded-sm text-[12.5px] cursor-pointer no-underline truncate',
          isActive
            ? 'bg-surface text-ink'
            : 'text-ink-2 hover:bg-paper-2 hover:text-ink',
        ].join(' ')
      }
    >
      {session.title || '未命名任务'}
    </NavLink>
  );
}
