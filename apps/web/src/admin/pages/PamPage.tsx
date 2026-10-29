import { useCallback, useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useConfirm, usePrompt, useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type { AdminUser, PamElevation, PamSession } from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

export function PamPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const toast = useToast();
  const confirm = useConfirm();
  const prompt = usePrompt();

  const [pending, setPending] = useState<PamElevation[]>([]);
  const [history, setHistory] = useState<PamElevation[]>([]);
  const [sessions, setSessions] = useState<PamSession[]>([]);
  const [userMap, setUserMap] = useState<Map<string, string>>(new Map());
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);


  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    setTodo(false);
    const tid = selectedTenantId ?? undefined;
    const usersQuery = tid ? `?tenant_id=${tid}` : '';
    Promise.all([
      api.admin.pam.pendingElevations(tid),
      api.admin.pam.elevationHistory(tid),
      api.admin.pam.sessions(tid),
      api.admin.users.list(usersQuery).catch(() => ({ items: [] as AdminUser[] })),
    ])
      .then(([p, h, s, u]) => {
        setPending(p); setHistory(h); setSessions(s);
        const m = new Map<string, string>();
        for (const usr of u.items) m.set(usr.id, usr.display_name || usr.email);
        setUserMap(m);
      })
      .catch((err) => { if (isTodo(err)) setTodo(true); else setError(err instanceof ApiError ? err.message : 'Failed to load'); })
      .finally(() => setLoading(false));
  }, [selectedTenantId]);

  useEffect(() => { load(); }, [load, reloadKey]);

  const refresh = () => setReloadKey((k) => k + 1);
  const userName = (uid?: string) => (uid && userMap.get(uid)) || uid || '—';

  const handleApprove = async (id?: string) => {
    if (!id) return;
    const justification = await prompt({ title: 'Approve elevation', label: 'Approval note (optional)', placeholder: 'Ticket verified' });
    if (justification === null) return;
    try {
      await api.admin.pam.approveElevation(id, { justification: justification.trim() }, selectedTenantId ?? undefined);
      toast.success('Approved');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Approval failed');
    }
  };

  const handleRevoke = async (id?: string) => {
    if (!id) return;
    if (!(await confirm({ title: 'Reject / revoke elevation', message: 'Reject or revoke this elevation request?', tone: 'danger', confirmText: 'Confirm' }))) return;
    try {
      await api.admin.pam.revokeElevation(id, selectedTenantId ?? undefined);
      toast.success('Done');
      refresh();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Action failed');
    }
  };

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Privileged access</h1>
        <p className="text-[12px] text-dim mt-0.5">Elevation approvals, history and active sessions (→ pam)</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">Tenant</label>
        <CustomSelect
          options={[{ value: '', label: '(Default / current tenant)' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
          className="min-w-[200px]"
        />
      </div>

      {error && <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">{error}</div>}
      {todo && <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">Not available yet: the PAM endpoint is unreachable. Data will appear here once it's connected.</div>}
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}

      {!todo && !loading && (
        <>
          {/* Pending */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Pending elevations</div>
            {pending.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">No pending requests</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">User</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Reason</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                      <th className="px-3 py-2 w-32"></th>
                    </tr>
                  </thead>
                  <tbody>
                    {pending.map((p, i) => (
                      <tr key={p.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(p.user_id)}</td>
                        <td className="px-3 py-2 text-[12px] text-ink truncate max-w-[260px]">{p.reason ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-warn">{p.status ?? '—'}</td>
                        <td className="px-3 py-2 flex items-center gap-2">
                          <button onClick={() => handleApprove(p.id)} className="text-[11px] font-mono text-green-700 border border-green-200 rounded-sm px-2 py-0.5 hover:bg-green-50">Approve</button>
                          <button onClick={() => handleRevoke(p.id)} className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50">Reject</button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Active sessions */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Active sessions</div>
            {sessions.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">No active elevated sessions</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Session ID</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">User</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Permission</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Expires</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sessions.map((s, i) => (
                      <tr key={s.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[140px]">{s.id ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(s.user_id)}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.permissions?.join(', ') || '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(s.expires_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* History */}
          <section className="flex flex-col gap-2">
            <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">History</div>
            {history.length === 0 ? (
              <div className="font-mono text-[12px] text-dim italic">No history</div>
            ) : (
              <div className="border border-line rounded-md overflow-hidden">
                <table className="w-full text-left border-collapse">
                  <thead className="bg-paper-2 border-b border-line">
                    <tr>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">User</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Reason</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                      <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Requested</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((h, i) => (
                      <tr key={h.id ?? i} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2 truncate max-w-[140px]">{userName(h.user_id)}</td>
                        <td className="px-3 py-2 text-[12px] text-dim truncate max-w-[260px]">{h.reason ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{h.status ?? '—'}</td>
                        <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(h.requested_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
