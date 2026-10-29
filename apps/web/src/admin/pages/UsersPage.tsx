import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import type { AdminUser, InviteUserRequest } from '../../api/types';
import { useConfirm, useToast } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
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
      setError(err instanceof ApiError ? err.message : 'Invite failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog title="Invite user" onClose={onClose} dismissable={!loading} className="w-full max-w-[440px]">
      <p className="text-[11px] text-dim -mt-1">
        {tenantId ? 'The user will join the currently filtered tenant' : 'The user will join your tenant'}
      </p>

      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        {error && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
        )}
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Email</label>
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
          <label className="text-[11px] text-dim font-mono">Display name (optional)</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Jane Doe"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-[11px] text-dim font-mono">Roles (comma-separated, optional)</label>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
            value={roles}
            onChange={(e) => setRoles(e.target.value)}
            placeholder="member, viewer"
          />
        </div>
        <label className="flex items-center gap-2 cursor-pointer mt-1">
          <input type="checkbox" checked={sendEmail} onChange={(e) => setSendEmail(e.target.checked)} />
          <span className="text-[12px] text-ink-2">Send invitation email</span>
        </label>

        <div className="flex items-center justify-end gap-2 pt-1">
          <button
            type="button"
            onClick={onClose}
            disabled={loading}
            className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={loading || !email.trim()}
            className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            {loading ? 'Inviting…' : 'Send invite'}
          </button>
        </div>
      </form>
    </Dialog>
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
      onError(err instanceof ApiError ? err.message : 'Action failed');
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
        title: 'Reset password',
        message: `Reset the password for "${user.email}"?`,
        confirmText: 'Reset',
      }))
    )
      return;
    run('reset', async () => {
      await api.admin.users.resetPassword(user.id, tenantId ?? undefined);
      toast.success('Password reset initiated');
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
          {busy === 'unfreeze' ? '…' : 'Unfreeze'}
        </button>
      )}
      {frozen !== true && (
        <button
          onClick={handleFreeze}
          disabled={!!busy}
          className="text-[11px] font-mono text-amber-700 border border-amber-200 rounded-sm px-2 py-0.5 hover:bg-amber-50 disabled:opacity-50"
        >
          {busy === 'freeze' ? '…' : 'Freeze'}
        </button>
      )}
      <button
        onClick={handleReset}
        disabled={!!busy}
        className="text-[11px] font-mono text-dim border border-line rounded-sm px-2 py-0.5 hover:text-ink hover:bg-paper-2 disabled:opacity-50"
      >
        {busy === 'reset' ? '…' : 'Reset password'}
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
      .catch((err) => { if (!cancelled) setUsersError(err instanceof ApiError ? err.message : 'Failed to load'); })
      .finally(() => { if (!cancelled) setUsersLoading(false); });
    return () => { cancelled = true; };
  }, [selectedTenantId, reloadKey, setUsers, setUsersLoading, setUsersError]);

  const refresh = () => setReloadKey((k) => k + 1);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Users</h1>
          <p className="text-[12px] text-dim mt-0.5">Cross-tenant user lookup and lifecycle actions</p>
        </div>
        <button
          onClick={() => setShowInvite(true)}
          className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity"
        >
          + Invite user
        </button>
      </div>

      {/* Tenant filter */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Filter by tenant</label>
        <CustomSelect
          options={[{ value: '', label: 'All tenants' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
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
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : users.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No users</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Email</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Display name</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Roles</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Tenant ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Actions</th>
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
