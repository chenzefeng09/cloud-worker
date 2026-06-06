import { useEffect } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';

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

  // Load users whenever selection changes
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
  }, [selectedTenantId, setUsers, setUsersLoading, setUsersError]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">用户管理</h1>
        <p className="text-[12px] text-dim mt-0.5">跨租户用户查询</p>
      </div>

      {/* Tenant filter */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">筛选租户</label>
        <select
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]"
          value={selectedTenantId ?? ''}
          onChange={(e) => setSelectedTenantId(e.target.value || null)}
          disabled={tenantsLoading}
        >
          <option value="">全部租户</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
