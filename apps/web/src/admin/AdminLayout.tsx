import { NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { useChatStore } from '../stores/chatStore';
import { TenantsPage } from './pages/TenantsPage';
import { UsersPage } from './pages/UsersPage';
import { QuotaPage } from './pages/QuotaPage';
import { AuditPage } from './pages/AuditPage';
import { SessionReplayPage } from './pages/SessionReplayPage';
import { ModelManagementPage } from './pages/ModelManagementPage';
import { AppReviewPage } from './pages/AppReviewPage';
import { RbacPage } from './pages/RbacPage';
import { OrgStructurePage } from './pages/OrgStructurePage';
import { FeatureFlagsPage } from './pages/FeatureFlagsPage';
import { SkillsAdminPage } from './pages/SkillsAdminPage';
import { McpServersPage } from './pages/McpServersPage';
import { KnowledgeAdminPage } from './pages/KnowledgeAdminPage';
import { OpsDashboardPage } from './pages/OpsDashboardPage';
import { PamPage } from './pages/PamPage';
import { ApiPlatformPage } from './pages/ApiPlatformPage';
import { NotificationAdminPage } from './pages/NotificationAdminPage';
import { PlanCataloguePage } from './pages/PlanCataloguePage';
import { DlpPage } from './pages/DlpPage';
import { CompliancePage } from './pages/CompliancePage';
import { SecurityOpsPage } from './pages/SecurityOpsPage';
import { LifecyclePage } from './pages/LifecyclePage';
import { DataProtectionPage } from './pages/DataProtectionPage';
import { SsoPage } from './pages/SsoPage';
import { DialogProvider } from './components/dialogs';
import { ErrorBoundary } from './components/ErrorBoundary';

interface AdminNavSpec {
  to: string;
  label: string;
  glyph: string;
}

interface AdminNavGroup {
  title: string;
  items: AdminNavSpec[];
}

const ADMIN_NAV_GROUPS: AdminNavGroup[] = [
  {
    title: '租户与组织',
    items: [
      { to: '/admin/tenants', label: '租户管理', glyph: '◫' },
      { to: '/admin/org', label: '组织架构', glyph: '⊞' },
    ],
  },
  {
    title: '用户与权限',
    items: [
      { to: '/admin/users', label: '用户管理', glyph: '◎' },
      { to: '/admin/rbac', label: '角色与权限', glyph: '⊙' },
      { to: '/admin/pam', label: '特权访问', glyph: '⊘' },
      { to: '/admin/sso', label: '身份源管理', glyph: '⊜' },
    ],
  },
  {
    title: '计费与配额',
    items: [
      { to: '/admin/plans', label: '套餐目录', glyph: '◈' },
      { to: '/admin/quota', label: '订阅与配额', glyph: '◷' },
    ],
  },
  {
    title: '模型与 AI',
    items: [
      { to: '/admin/models', label: '模型管理', glyph: '⊕' },
    ],
  },
  {
    title: '平台治理',
    items: [
      { to: '/admin/apps', label: '应用审核', glyph: '▣' },
      { to: '/admin/skills', label: '技能管理', glyph: '✦' },
      { to: '/admin/mcp', label: 'MCP 服务器', glyph: '⊟' },
      { to: '/admin/knowledge', label: '知识库', glyph: '❖' },
      { to: '/admin/ops', label: '运行与沙箱', glyph: '◳' },
      { to: '/admin/api-platform', label: 'API 平台', glyph: '⌘' },
    ],
  },
  {
    title: '运营与合规',
    items: [
      { to: '/admin/flags', label: 'Feature Flags', glyph: '⚑' },
      { to: '/admin/audit', label: '审计日志', glyph: '≡' },
      { to: '/admin/replay', label: '会话回放', glyph: '▷' },
      { to: '/admin/notification', label: '通知中心', glyph: '✉' },
      { to: '/admin/dlp', label: '数据防泄漏', glyph: '⛨' },
      { to: '/admin/compliance', label: '合规中心', glyph: '⚖' },
      { to: '/admin/security-ops', label: '安全运营', glyph: '⊛' },
      { to: '/admin/lifecycle', label: '数据生命周期', glyph: '♺' },
      { to: '/admin/data-protection', label: '数据保护', glyph: '⛉' },
    ],
  },
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
    <DialogProvider>
    <div className="h-full w-full flex min-w-0">
      {/* Admin sidebar */}
      <aside className="w-[220px] shrink-0 border-r border-line p-3.5 flex flex-col gap-5 bg-paper overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-2 px-1">
          <span className="font-mono text-[10px] font-semibold tracking-[0.12em] uppercase text-dim">
            管理后台
          </span>
        </div>

        <nav className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-4 -mr-1 pr-1">
          {ADMIN_NAV_GROUPS.map((group) => (
            <div key={group.title} className="flex flex-col gap-0.5">
              <div className="px-2 mb-1 font-mono text-[9.5px] font-semibold tracking-[0.12em] uppercase text-dim">
                {group.title}
              </div>
              {group.items.map((n) => (
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
            </div>
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
      <main className="flex-1 min-w-0 overflow-hidden">
        <div className="h-full w-full flex flex-col">
          <ErrorBoundary>
          <Routes>
            <Route path="tenants" element={<TenantsPage />} />
            <Route path="org" element={<OrgStructurePage />} />
            <Route path="users" element={<UsersPage />} />
            <Route path="rbac" element={<RbacPage />} />
            <Route path="plans" element={<PlanCataloguePage />} />
            <Route path="quota" element={<QuotaPage />} />
            <Route path="models" element={<ModelManagementPage />} />
            <Route path="apps" element={<AppReviewPage />} />
            <Route path="skills" element={<SkillsAdminPage />} />
            <Route path="mcp" element={<McpServersPage />} />
            <Route path="knowledge" element={<KnowledgeAdminPage />} />
            <Route path="ops" element={<OpsDashboardPage />} />
            <Route path="pam" element={<PamPage />} />
            <Route path="sso" element={<SsoPage />} />
            <Route path="api-platform" element={<ApiPlatformPage />} />
            <Route path="flags" element={<FeatureFlagsPage />} />
            <Route path="audit" element={<AuditPage />} />
            <Route path="replay" element={<SessionReplayPage />} />
            <Route path="notification" element={<NotificationAdminPage />} />
            <Route path="dlp" element={<DlpPage />} />
            <Route path="compliance" element={<CompliancePage />} />
            <Route path="security-ops" element={<SecurityOpsPage />} />
            <Route path="lifecycle" element={<LifecyclePage />} />
            <Route path="data-protection" element={<DataProtectionPage />} />
            <Route path="*" element={<Navigate to="/admin/tenants" replace />} />
          </Routes>
          </ErrorBoundary>
        </div>
      </main>
    </div>
    </DialogProvider>
  );
}
