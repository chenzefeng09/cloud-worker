import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { useChatStore } from '../stores/chatStore';
import { TenantsPage } from './pages/TenantsPage';
import { UsersPage } from './pages/UsersPage';
import { QuotaPage } from './pages/QuotaPage';
import { AuditPage } from './pages/AuditPage';
import { SessionReplayPage } from './pages/SessionReplayPage';
import { EndpointDiagnosticsPage } from './pages/EndpointDiagnosticsPage';
import { AppReviewPage } from './pages/AppReviewPage';

interface AdminNavSpec {
  to: string;
  label: string;
  glyph: string;
}

const ADMIN_NAV: AdminNavSpec[] = [
  { to: '/admin/tenants', label: '租户管理', glyph: '◫' },
  { to: '/admin/users', label: '用户管理', glyph: '◎' },
  { to: '/admin/apps', label: '应用审核', glyph: '▣' },
  { to: '/admin/quota', label: '配额套餐', glyph: '◷' },
  { to: '/admin/audit', label: '审计日志', glyph: '≡' },
  { to: '/admin/replay', label: '会话回放', glyph: '▷' },
  { to: '/admin/diagnostics', label: '端点诊断', glyph: '⊕' },
];

/**
 * Admin-only shell. Rendered only when the authenticated user has
 * the 'super_admin' role — enforced at the route level in WorkspaceLayout.
 */
export function AdminLayout() {
  const userProfile = useChatStore((s) => s.userProfile);

  // Double-check gate: belt-and-suspenders, route-level gate should already block
  if (!userProfile?.roles?.includes('super_admin')) {
    return <Navigate to="/home" replace />;
  }

  return (
    <div className="h-full flex min-w-0">
      {/* Admin sidebar */}
      <aside className="w-[220px] shrink-0 border-r border-line p-3.5 flex flex-col gap-5 bg-paper overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-2 px-1">
          <span className="font-mono text-[10px] font-semibold tracking-[0.12em] uppercase text-dim">
            管理后台
          </span>
        </div>

        <nav className="flex flex-col gap-0.5">
          {ADMIN_NAV.map((n) => (
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
                </>
              )}
            </NavLink>
          ))}
        </nav>

        {/* Back to main */}
        <div className="mt-auto pt-3.5 border-t border-line">
          <NavLink
            to="/home"
            className="flex items-center gap-2 text-[12px] text-dim hover:text-ink no-underline"
          >
            <span className="font-mono text-[10px]">←</span>
            <span>返回工作区</span>
          </NavLink>
          <div className="mt-2 font-mono text-[10px] text-dim truncate">
            {userProfile?.email}
          </div>
        </div>
      </aside>

      {/* Admin main content */}
      <main className="flex-1 flex flex-col min-w-0 overflow-hidden">
        <Routes>
          <Route path="tenants" element={<TenantsPage />} />
          <Route path="users" element={<UsersPage />} />
          <Route path="apps" element={<AppReviewPage />} />
          <Route path="quota" element={<QuotaPage />} />
          <Route path="audit" element={<AuditPage />} />
          <Route path="replay" element={<SessionReplayPage />} />
          <Route path="diagnostics" element={<EndpointDiagnosticsPage />} />
          <Route path="*" element={<Navigate to="/admin/tenants" replace />} />
        </Routes>
      </main>
    </div>
  );
}
