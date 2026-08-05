import { useMemo, useCallback } from 'react';
import { NavLink } from 'react-router-dom';
import { useChatStore } from '../../stores/chatStore';
import { useSessions } from '../../hooks/useSessions';
import { useWorkspace } from '../../lib/workspaceContext';
import { AccountMenu } from './AccountMenu';
import { canAccessAdmin } from '../../admin/AdminLayout';
import { formatAbsoluteTimeZh } from '../../lib/format';
import type { Session } from '../../api/types';

interface NavSpec {
  to: string;
  label: string;
  glyph: string;
  count?: string;
}

const NAV: NavSpec[] = [
  { to: '/home', label: '新任务', glyph: '◇' },
  { to: '/apps', label: '应用', glyph: '▦' },
  { to: '/workspace', label: '我的工作区', glyph: '⌗' },
  { to: '/library', label: '资源库', glyph: '✦' },
  { to: '/artifacts', label: '产物', glyph: '◈' },
];

const UNGROUPED = '__ungrouped__';

interface WorkspaceGroup {
  key: string;
  label: string;
  items: Session[];
}

export function Sidebar() {
  const { sessions, loadMore, loadingMore, hasMore } = useSessions();
  const { workspaces } = useWorkspace();
  const userProfile = useChatStore((s) => s.userProfile);
  const collapsed = useChatStore((s) => s.sidebarCollapsed);
  const setCollapsed = useChatStore((s) => s.setSidebarCollapsed);

  const devBuildLabel = import.meta.env.DEV
    ? `dev ${import.meta.env.VITE_DEV_BUILD_STAMP ?? 'local'}`
    : null;

  const wsNameMap = useMemo(() => {
    const m = new Map<string, string>();
    for (const w of workspaces) m.set(w.id, w.name);
    return m;
  }, [workspaces]);

  const groups = useMemo<WorkspaceGroup[]>(() => {
    const buckets = new Map<string, Session[]>();
    for (const s of sessions) {
      const key = s.workspace_id && wsNameMap.has(s.workspace_id)
        ? s.workspace_id
        : UNGROUPED;
      const list = buckets.get(key);
      if (list) list.push(s);
      else buckets.set(key, [s]);
    }

    const result: WorkspaceGroup[] = [];
    const ungrouped = buckets.get(UNGROUPED);
    buckets.delete(UNGROUPED);

    const sorted = [...buckets.entries()].sort((a, b) => {
      const aMax = a[1][0]?.updated_at ?? '';
      const bMax = b[1][0]?.updated_at ?? '';
      return bMax.localeCompare(aMax);
    });

    for (const [key, items] of sorted) {
      result.push({ key, label: wsNameMap.get(key) ?? key, items });
    }
    if (ungrouped && ungrouped.length > 0) {
      result.push({ key: UNGROUPED, label: '未分组', items: ungrouped });
    }
    return result;
  }, [sessions, wsNameMap]);

  const toggleGroup = useCallback((key: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, [setCollapsed]);

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
              end={n.to === '/home' || n.to === '/apps'}
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
          {groups.map((g) => (
            <WorkspaceGroupSection
              key={g.key}
              group={g}
              isCollapsed={collapsed.has(g.key)}
              onToggle={() => toggleGroup(g.key)}
            />
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

function WorkspaceGroupSection({
  group,
  isCollapsed,
  onToggle,
}: {
  group: WorkspaceGroup;
  isCollapsed: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="mb-1">
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-1 px-2 pt-2 pb-1 text-left select-none group"
      >
        <span className="font-mono text-[9px] text-dim group-hover:text-ink-2 transition-transform duration-150"
          style={{ transform: isCollapsed ? 'rotate(-90deg)' : 'rotate(0deg)' }}
        >
          ▼
        </span>
        <span className="text-[10.5px] text-dim group-hover:text-ink-2 truncate font-medium">
          {group.label}
        </span>
        <span className="ml-auto font-mono text-[9px] text-dim">
          {group.items.length}
        </span>
      </button>
      {!isCollapsed && (
        <div className="flex flex-col gap-px">
          {group.items.map((s) => (
            <RecentItem key={s.id} session={s} />
          ))}
        </div>
      )}
    </div>
  );
}

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
