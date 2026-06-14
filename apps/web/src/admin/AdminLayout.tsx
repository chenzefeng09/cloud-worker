import type { ReactNode } from 'react';
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
import { LargeAttachmentPage } from './pages/LargeAttachmentPage';
import { PoolMonitorPage } from './pages/PoolMonitorPage';
import { DataProtectionPage } from './pages/DataProtectionPage';
import { StorageManagementPage } from './pages/StorageManagementPage';
import { OrphanReportsPage } from './pages/OrphanReportsPage';
import { SsoPage } from './pages/SsoPage';
import { DialogProvider } from './components/dialogs';
import { ErrorBoundary } from './components/ErrorBoundary';
import { hasPermission, isPlatformAdmin } from '../lib/permissions';

interface AdminNavSpec {
  to: string;
  label: string;
  glyph: string;
  /**
   * Permission token required for this module to be visible/usable. When
   * omitted, the module is platform-super-admin only (requires `*` /
   * `platform.admin`). As more modules gain tenant-scoped support they should
   * declare their `requires` token here and add a matching per-route gate.
   */
  requires?: string;
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
      { to: '/admin/org', label: '组织架构', glyph: '⊞', requires: 'org.read' },
    ],
  },
  {
    title: '用户与权限',
    items: [
      { to: '/admin/users', label: '用户管理', glyph: '◎' },
      { to: '/admin/rbac', label: '角色与权限', glyph: '⊙', requires: 'authz.role.manage' },
      { to: '/admin/pam', label: '特权访问', glyph: '⊘' },
      { to: '/admin/sso', label: '身份源管理', glyph: '⊜' },
    ],
  },
  {
    title: '计费与配额',
    items: [
      { to: '/admin/plans', label: '套餐目录', glyph: '◈' },
      { to: '/admin/quota', label: '订阅与配额', glyph: '◷', requires: 'quota.read' },
    ],
  },
  {
    title: '模型与 AI',
    items: [
      { to: '/admin/models', label: '模型管理', glyph: '⊕', requires: 'model.read' },
    ],
  },
  {
    title: '平台治理',
    items: [
      { to: '/admin/apps', label: '应用审核', glyph: '▣' },
      { to: '/admin/skills', label: '技能管理', glyph: '✦', requires: 'skill.create' },
      { to: '/admin/mcp', label: 'MCP 服务器', glyph: '⊟', requires: 'mcp_server.create' },
      { to: '/admin/knowledge', label: '知识库', glyph: '❖', requires: 'document.create' },
      { to: '/admin/ops', label: '运行与沙箱', glyph: '◳', requires: 'sandbox.read' },
      { to: '/admin/pool-monitor', label: '温池监控', glyph: '▦' },
      { to: '/admin/large-attachment', label: '大附件能力', glyph: '⊠', requires: 'session.attachment.large.create' },
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
  {
    title: '存储管理',
    items: [
      { to: '/admin/storage-quotas', label: '租户配额', glyph: '▤' },
      { to: '/admin/storage-orphans', label: '孤儿对账', glyph: '◌' },
    ],
  },
];

/** Whether a given nav item is visible/usable for the supplied permission set. */
function isNavItemVisible(item: AdminNavSpec, perms: string[] | undefined): boolean {
  return item.requires ? hasPermission(perms, item.requires) : isPlatformAdmin(perms);
}

/**
 * Whether the user may enter the admin console at all — true if they are a
 * platform admin OR hold the permission required by at least one admin module.
 * Shared by the route-level gate (WorkspaceLayout), the sidebar entry, and the
 * component-level gate below so all three stay in lock-step.
 */
export function canAccessAdmin(perms: string[] | undefined): boolean {
  if (isPlatformAdmin(perms)) return true;
  return ADMIN_NAV_GROUPS.some((g) => g.items.some((i) => isNavItemVisible(i, perms)));
}

/** First admin path the user can actually reach, for default-landing redirects. */
function firstAccessiblePath(perms: string[] | undefined): string {
  for (const group of ADMIN_NAV_GROUPS) {
    for (const item of group.items) {
      if (isNavItemVisible(item, perms)) return item.to;
    }
  }
  return '/home';
}

/**
 * Per-route guard. Renders children only when the permission set satisfies
 * `required` (or, when `required` is omitted, the user is a platform admin).
 * Otherwise bounces to the user's first reachable admin page (or /home).
 */
function RequirePerm({
  perms,
  required,
  children,
}: {
  perms: string[] | undefined;
  required?: string;
  children: ReactNode;
}) {
  const ok = required ? hasPermission(perms, required) : isPlatformAdmin(perms);
  if (!ok) {
    return <Navigate to={firstAccessiblePath(perms)} replace />;
  }
  return <>{children}</>;
}

/**
 * Admin shell. Entry is gated by effective authz permissions — platform admins
 * see everything; tenant admins see only the modules they hold permissions for.
 * Enforced at the route level in WorkspaceLayout and re-checked here.
 */
export function AdminLayout() {
  const userProfile = useChatStore((s) => s.userProfile);
  const perms = userProfile?.permissions;

  // Double-check gate: belt-and-suspenders, route-level gate should already block
  if (!canAccessAdmin(perms)) {
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
          {ADMIN_NAV_GROUPS.map((group) => {
            const visibleItems = group.items.filter((n) => isNavItemVisible(n, perms));
            if (visibleItems.length === 0) return null;
            return (
            <div key={group.title} className="flex flex-col gap-0.5">
              <div className="px-2 mb-1 font-mono text-[9.5px] font-semibold tracking-[0.12em] uppercase text-dim">
                {group.title}
              </div>
              {visibleItems.map((n) => (
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
            );
          })}
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
            {/* Tenant-scoped modules are gated on a module-create token so tenant
                admins may reach them (RBAC → authz.role.manage; skills →
                skill.create; mcp → mcp_server.create; knowledge →
                document.create; ops → sandbox.read; large-attachment →
                session.attachment.large.create; models →
                model.read — tenant admins pick their tenant defaults while the
                provider directory stays platform-admin-only).  Pool-monitor
                stays platform-admin-only (no `requires`). These must match the
                `requires` token declared
                in ADMIN_NAV_GROUPS *and* the BFF guard's action so "visible"
                tracks "reachable".  Every other module has no `required`, so
                RequirePerm falls back to isPlatformAdmin (platform-admin only). */}
            <Route path="rbac" element={<RequirePerm perms={perms} required="authz.role.manage"><RbacPage /></RequirePerm>} />
            <Route path="tenants" element={<RequirePerm perms={perms}><TenantsPage /></RequirePerm>} />
            <Route path="org" element={<RequirePerm perms={perms} required="org.read"><OrgStructurePage /></RequirePerm>} />
            <Route path="users" element={<RequirePerm perms={perms}><UsersPage /></RequirePerm>} />
            <Route path="plans" element={<RequirePerm perms={perms}><PlanCataloguePage /></RequirePerm>} />
            <Route path="quota" element={<RequirePerm perms={perms} required="quota.read"><QuotaPage /></RequirePerm>} />
            <Route path="models" element={<RequirePerm perms={perms} required="model.read"><ModelManagementPage /></RequirePerm>} />
            <Route path="apps" element={<RequirePerm perms={perms}><AppReviewPage /></RequirePerm>} />
            <Route path="skills" element={<RequirePerm perms={perms} required="skill.create"><SkillsAdminPage /></RequirePerm>} />
            <Route path="mcp" element={<RequirePerm perms={perms} required="mcp_server.create"><McpServersPage /></RequirePerm>} />
            <Route path="knowledge" element={<RequirePerm perms={perms} required="document.create"><KnowledgeAdminPage /></RequirePerm>} />
            <Route path="ops" element={<RequirePerm perms={perms} required="sandbox.read"><OpsDashboardPage /></RequirePerm>} />
            <Route path="pool-monitor" element={<RequirePerm perms={perms}><PoolMonitorPage /></RequirePerm>} />
            <Route path="large-attachment" element={<RequirePerm perms={perms} required="session.attachment.large.create"><LargeAttachmentPage /></RequirePerm>} />
            <Route path="pam" element={<RequirePerm perms={perms}><PamPage /></RequirePerm>} />
            <Route path="sso" element={<RequirePerm perms={perms}><SsoPage /></RequirePerm>} />
            <Route path="api-platform" element={<RequirePerm perms={perms}><ApiPlatformPage /></RequirePerm>} />
            <Route path="flags" element={<RequirePerm perms={perms}><FeatureFlagsPage /></RequirePerm>} />
            <Route path="audit" element={<RequirePerm perms={perms}><AuditPage /></RequirePerm>} />
            <Route path="replay" element={<RequirePerm perms={perms}><SessionReplayPage /></RequirePerm>} />
            <Route path="notification" element={<RequirePerm perms={perms}><NotificationAdminPage /></RequirePerm>} />
            <Route path="dlp" element={<RequirePerm perms={perms}><DlpPage /></RequirePerm>} />
            <Route path="compliance" element={<RequirePerm perms={perms}><CompliancePage /></RequirePerm>} />
            <Route path="security-ops" element={<RequirePerm perms={perms}><SecurityOpsPage /></RequirePerm>} />
            <Route path="lifecycle" element={<RequirePerm perms={perms}><LifecyclePage /></RequirePerm>} />
            <Route path="data-protection" element={<RequirePerm perms={perms}><DataProtectionPage /></RequirePerm>} />
            <Route path="storage-quotas" element={<RequirePerm perms={perms}><StorageManagementPage /></RequirePerm>} />
            <Route path="storage-orphans" element={<RequirePerm perms={perms}><OrphanReportsPage /></RequirePerm>} />
            <Route path="*" element={<Navigate to={firstAccessiblePath(perms)} replace />} />
          </Routes>
          </ErrorBoundary>
        </div>
      </main>
    </div>
    </DialogProvider>
  );
}
