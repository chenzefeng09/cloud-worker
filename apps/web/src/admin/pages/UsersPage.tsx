import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { AdminUser, InviteUserRequest } from '../../api/types';
import { useConfirm, useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';

/**
 * AdminUser may carry an optional `status` field on some upstream payloads
 * ("ACTIVE" / "FROZEN" / "SUSPENDED"). The shared type doesn't declare it, so
 * we read it defensively to decide whether to show freeze vs. unfreeze.
 */
function userFrozen(u: AdminUser): boolean | null {
  const status = (u as AdminUser & { status?: string; frozen?: boolean }).status;
  const frozen = (u as AdminUser & { status?: string; frozen?: boolean }).frozen;
  if (typeof frozen === 'boolean') return frozen;
  if (typeof status === 'string') {
    const s = status.toUpperCase();
    if (s === 'FROZEN' || s === 'SUSPENDED' || s === 'DISABLED') return true;
    if (s === 'ACTIVE' || s === 'ENABLED') return false;
  }
  return null; // unknown — show both actions
}

// ─── Invite User Dialog ───────────────────────────────────────────────────────

function InviteUserDialog({
  tenantId,
  onClose,
  onInvited,
}: {
  tenantId: string | null;
  onClose: () => void;
  onInvited: () => void;
}) {
  const [email, setEmail] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [roles, setRoles] = useState('');
  const [sendEmail, setSendEmail] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const body: InviteUserRequest = {
        email: email.trim(),
        send_invite_email: sendEmail,
      };
      if (displayName.trim()) body.display_name = displayName.trim();
      const roleList = roles
        .split(',')
        .map((r) => r.trim())
        .filter(Boolean);
      if (roleList.length > 0) body.roles = roleList;
      // tenant_id is a query param, not a body field (cross-tenant invite).
      await api.admin.users.invite(body, tenantId ?? undefined);
      onInvited();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '邀请失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[12vh] bg-black/30" onClick={onClose}>
      <form
        onSubmit={handleSubmit}
        className="bg-paper border border-line rounded-lg shadow-xl w-[440px] flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 pt-5 pb-3 border-b border-line">
          <h2 className="text-[15px] font-display text-ink tracking-[-0.02em]">邀请用户</h2>
          <p className="text-[11px] text-dim mt-0.5">
            {tenantId ? '邀请将加入当前筛选的租户' : '邀请将加入你所属的租户'}
          </p>
        </div>

        <div className="px-5 py-4 flex flex-col gap-3">
          {error && (
            <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
          )}
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">邮箱</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="user@acme.co"
              required
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">显示名 (可选)</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="张三"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-[11px] text-dim font-mono">角色 (逗号分隔, 可选)</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
              value={roles}
              onChange={(e) => setRoles(e.target.value)}
              placeholder="member, viewer"
            />
          </div>
          <label className="flex items-center gap-2 cursor-pointer mt-1">
            <input type="checkbox" checked={sendEmail} onChange={(e) => setSendEmail(e.target.checked)} />
            <span className="text-[12px] text-ink-2">发送邀请邮件</span>
          </label>
        </div>

        <div className="px-5 py-3 border-t border-line flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
          <button
            type="submit"
            disabled={loading || !email.trim()}
            className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? '邀请中…' : '发送邀请'}
          </button>
        </div>
      </form>
    </div>
  );
}

// ─── User Action Cell ─────────────────────────────────────────────────────────

function UserActions({
  user,
  tenantId,
  onChanged,
  onError,
}: {
  user: AdminUser;
  tenantId: string | null;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const frozen = userFrozen(user);
  const confirm = useConfirm();
  const toast = useToast();

  const run = async (label: string, fn: () => Promise<void>) => {
    setBusy(label);
    onError('');
    try {
      await fn();
      onChanged();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusy(null);
    }
  };

  const handleFreeze = () =>
    run('freeze', () => api.admin.users.freeze(user.id, tenantId ?? undefined));
  const handleUnfreeze = () =>
    run('unfreeze', () => api.admin.users.unfreeze(user.id, tenantId ?? undefined));
  const handleReset = async () => {
    if (
      !(await confirm({
        title: '重置密码',
        message: `确定要重置用户「${user.email}」的密码吗？`,
        confirmText: '重置',
      }))
    )
      return;
    run('reset', async () => {
      await api.admin.users.resetPassword(user.id, tenantId ?? undefined);
      toast.success('已发起密码重置');
    });
  };

  return (
    <div className="flex items-center gap-2">
      {/* When status is unknown (null) show both freeze and unfreeze */}
      {frozen !== false && (
        <button
          onClick={handleUnfreeze}
          disabled={!!busy}
          className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50 disabled:opacity-50"
        >
          {busy === 'unfreeze' ? '…' : '解冻'}
        </button>
      )}
      {frozen !== true && (
        <button
          onClick={handleFreeze}
          disabled={!!busy}
          className="text-[11px] font-mono text-amber-700 border border-amber-200 rounded-sm px-2 py-0.5 hover:bg-amber-50 disabled:opacity-50"
        >
          {busy === 'freeze' ? '…' : '冻结'}
        </button>
      )}
      <button
        onClick={handleReset}
        disabled={!!busy}
        className="text-[11px] font-mono text-dim border border-line rounded-sm px-2 py-0.5 hover:text-ink hover:bg-paper-2 disabled:opacity-50"
      >
        {busy === 'reset' ? '…' : '重置密码'}
      </button>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function UsersPage() {
  const {
    tenants,
    tenantsLoading,
    setTenants,
    setTenantsLoading,
    users,
    usersLoading,
    usersError,
    setUsers,
    setUsersLoading,
    setUsersError,
    selectedTenantId,
    setSelectedTenantId,
  } = useAdminStore();

  const [showInvite, setShowInvite] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  // Bootstrap tenants list for the selector (may already be loaded)
  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* silently ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  // Load users whenever selection changes (or after an action via reloadKey)
  useEffect(() => {
    let cancelled = false;
    setUsersLoading(true);
    setUsersError(null);
    api.admin.users
      .list(selectedTenantId ?? undefined)
      .then((data) => { if (!cancelled) setUsers(data.items); })
      .catch((err) => { if (!cancelled) setUsersError(err instanceof ApiError ? err.message : '加载失败'); })
      .finally(() => { if (!cancelled) setUsersLoading(false); });
    return () => { cancelled = true; };
  }, [selectedTenantId, reloadKey, setUsers, setUsersLoading, setUsersError]);

  const refresh = () => setReloadKey((k) => k + 1);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">用户管理</h1>
          <p className="text-[12px] text-dim mt-0.5">跨租户用户查询与生命周期操作</p>
        </div>
        <button
          onClick={() => setShowInvite(true)}
          className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity"
        >
          + 邀请用户
        </button>
      </div>

      {/* Tenant filter */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">筛选租户</label>
        <CustomSelect
          options={[{ value: '', label: '全部租户' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {usersError && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {usersError}
        </div>
      )}

      {usersLoading ? (
        <div className="font-mono text-[12px] text-dim">加载中…</div>
      ) : users.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">暂无用户</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">邮箱</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">显示名</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">角色</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">租户 ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 text-[13px] text-ink">{u.email}</td>
                  <td className="px-3 py-2 text-[13px] text-ink-2">{u.display_name || '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {u.roles && u.roles.length > 0 ? u.roles.join(', ') : '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]">{u.tenant_id}</td>
                  <td className="px-3 py-2">
                    <UserActions
                      user={u}
                      tenantId={selectedTenantId}
                      onChanged={refresh}
                      onError={(msg) => setUsersError(msg || null)}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showInvite && (
        <InviteUserDialog
          tenantId={selectedTenantId}
          onClose={() => setShowInvite(false)}
          onInvited={refresh}
        />
      )}
    </div>
  );
}
