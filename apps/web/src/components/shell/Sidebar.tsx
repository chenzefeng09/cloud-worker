import { NavLink, useNavigate } from 'react-router-dom';
import { useWorkspace } from '../../lib/workspaceContext';
import { useChatStore } from '../../stores/chatStore';
import { useSessions } from '../../hooks/useSessions';
import { api } from '../../api/client';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

interface NavSpec {
  to: string;
  label: string;
  glyph: string;
  count?: string;
  countFrom?: 'tasks';
}

const NAV: NavSpec[] = [
  { to: '/home', label: '首页', glyph: '◇' },
  { to: '/tasks', label: '任务', glyph: '▤', countFrom: 'tasks' },
  { to: '/files', label: '基础文件', glyph: '⌗' },
  { to: '/library', label: '资源库', glyph: '✦', count: '13' },
  { to: '/fleet', label: '工作节点', glyph: '◈', count: '6' },
];

export function Sidebar() {
  const { ws } = useWorkspace();
  const { sessions } = useSessions();
  const userProfile = useChatStore((s) => s.userProfile);
  const logout = useChatStore((s) => s.logout);
  const navigate = useNavigate();

  const navCounts = {
    tasks: String(sessions.length),
  };

  const recent = sessions.slice(0, 3);
  const devBuildLabel = import.meta.env.DEV
    ? `dev ${import.meta.env.VITE_DEV_BUILD_STAMP ?? 'local'}`
    : null;

  const handleLogout = async () => {
    try {
      await api.auth.logout();
    } catch {
      // The BFF logout endpoint is best-effort; even on failure we clear local
      // state so the user lands back on the login page.
    }
    logout();
    navigate('/home');
  };

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

      <WorkspaceSwitcher />

      <nav className="flex flex-col gap-0.5">
        {NAV.map((n) => {
          const count = n.countFrom ? navCounts[n.countFrom] : n.count;
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

        <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim px-2 pt-3 pb-1">
          最近 · {ws.name}
        </div>
        {recent.length === 0 && (
          <div className="font-mono italic text-dim text-[11px] px-2.5 py-1">暂无任务</div>
        )}
        {recent.map((s) => (
          <NavLink
            key={s.id}
            to={`/tasks/${s.id}`}
            className={({ isActive }) =>
              [
                'flex items-center gap-2.5 px-2 py-1.5 rounded-sm border cursor-pointer no-underline truncate',
                isActive
                  ? 'bg-surface border-line text-ink'
                  : 'border-transparent text-ink-2 hover:bg-paper-2 hover:text-ink',
              ].join(' ')
            }
          >
            <span
              className="w-4 text-center font-mono text-[11px]"
              style={{
                color:
                  s.status === 'ACTIVE'
                    ? 'var(--cw-accent, #d23a1f)'
                    : s.status === 'COMPLETED'
                      ? '#2c8f5d'
                      : '#75756f',
              }}
            >
              {s.status === 'ACTIVE'
                ? '●'
                : s.status === 'COMPLETED'
                  ? '✓'
                  : s.status === 'PAUSED'
                    ? '‖'
                    : '◷'}
            </span>
            <span className="truncate text-[12.5px]">{s.title || '未命名任务'}</span>
          </NavLink>
        ))}
      </nav>

      {/* Admin entry — only super_admin */}
      {userProfile?.roles?.includes('super_admin') && (
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
        <NavLink
          to="/profile"
          className={({ isActive }) =>
            [
              'flex items-center gap-2 mt-1 text-left text-[12.5px] no-underline',
              isActive ? 'text-ink' : 'text-ink-2 hover:text-ink',
            ].join(' ')
          }
        >
          <span className="inline-flex w-[22px] h-[22px] items-center justify-center rounded-full border border-line bg-surface font-mono text-[10px] font-semibold text-ink shrink-0">
            {userProfile?.display_name?.[0]?.toUpperCase() ?? 'Y'}
          </span>
          <span className="truncate">{userProfile?.email ?? 'you@acme.co'}</span>
        </NavLink>
        <button
          onClick={handleLogout}
          className="text-left font-mono text-[10px] text-dim hover:text-ink-2 pl-[30px]"
        >
          登出
        </button>
        {devBuildLabel && (
          <div className="pl-[30px] font-mono text-[9px] text-dim" title="Frontend dev build stamp">
            {devBuildLabel}
          </div>
        )}
      </div>
    </aside>
  );
}
