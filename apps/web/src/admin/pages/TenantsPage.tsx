import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateAuto } from '../../i18n/dates';
import { Dialog } from '../components/Dialog';
import { useToast } from '../components/dialogs';
import { CustomSelect } from '../components/CustomSelect';
import type { CreateTenantRequest, PatchTenantRequest, Tenant, TenantStatus, TenantTier } from '../../api/types';

const TIERS: TenantTier[] = ['FREE', 'PRO', 'BUSINESS', 'ENTERPRISE', 'DEDICATED'];
const STATUSES: TenantStatus[] = ['ACTIVE', 'SUSPENDED', 'TERMINATING', 'TERMINATED'];

const STATUS_COLOR: Record<TenantStatus, string> = {
  ACTIVE: 'text-ok',
  SUSPENDED: 'text-warn',
  TERMINATING: 'text-dim',
  TERMINATED: 'text-dim',
};

// ─── Create Tenant Form ───────────────────────────────────────────────────────

function CreateTenantForm({ onCreated }: { onCreated: (t: Tenant) => void }) {
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [tier, setTier] = useState<TenantTier>('FREE');
  const [adminEmail, setAdminEmail] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setOpen(false);
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !adminEmail.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const body: CreateTenantRequest = { name: name.trim(), tier, admin_email: adminEmail.trim() };
      const created = await api.admin.tenants.create(body);
      onCreated(created);
      toast.success(`Tenant "${created.name ?? name.trim()}" created`);
      setName('');
      setAdminEmail('');
      setTier('FREE');
      setOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Create failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 transition-opacity"
      >
        + New tenant
      </button>
      {open && (
        <Dialog title="New tenant" onClose={close} dismissable={!loading} className="w-full max-w-md">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            {error && (
              <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
            )}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Tenant name</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="acme-corp"
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Plan</label>
              <CustomSelect
                options={TIERS.map((t) => ({ value: t, label: t }))}
                value={tier}
                onChange={(v) => setTier(v as TenantTier)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Admin email</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                type="email"
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
                placeholder="admin@acme.co"
                required
              />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={close}
                disabled={loading}
                className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={loading}
                className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {loading ? 'Creating…' : 'Create'}
              </button>
            </div>
          </form>
        </Dialog>
      )}
    </>
  );
}

// ─── Edit Tenant Inline ───────────────────────────────────────────────────────

function EditTenantRow({ tenant, onPatched }: { tenant: Tenant; onPatched: (t: Tenant) => void }) {
  const [editing, setEditing] = useState(false);
  const [tier, setTier] = useState<TenantTier>(tenant.tier);
  const [status, setStatus] = useState<TenantStatus>(tenant.status);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    const body: PatchTenantRequest = {};
    if (tier !== tenant.tier) body.tier = tier;
    if (status !== tenant.status) body.status = status;
    if (!body.tier && !body.status) { setEditing(false); return; }
    setLoading(true);
    setError(null);
    try {
      const updated = await api.admin.tenants.patch(tenant.id, body);
      onPatched(updated);
      setEditing(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Update failed');
    } finally {
      setLoading(false);
    }
  };

  if (!editing) {
    return (
      <tr className="border-b border-line-2 hover:bg-paper-2 transition-colors group">
        <td className="px-3 py-2 font-mono text-[12px] text-dim truncate max-w-[120px]">{tenant.id?.slice(0, 8) ?? '—'}…</td>
        <td className="px-3 py-2 text-[13px] text-ink">{tenant.name ?? '—'}</td>
        <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{tenant.tier ?? '—'}</td>
        <td className={`px-3 py-2 font-mono text-[11px] ${(tenant.status && STATUS_COLOR[tenant.status]) ?? 'text-dim'}`}>{tenant.status ?? '—'}</td>
        <td className="px-3 py-2 font-mono text-[11px] text-dim">
          {tenant.created_at ? formatDateAuto(new Date(tenant.created_at)) : '—'}
        </td>
        <td className="px-3 py-2">
          <button
            onClick={() => setEditing(true)}
            className="opacity-0 group-hover:opacity-100 text-[11px] font-mono text-dim hover:text-ink transition-opacity"
          >
            Edit
          </button>
        </td>
      </tr>
    );
  }

  return (
    <tr className="border-b border-line bg-surface">
      <td className="px-3 py-2 font-mono text-[12px] text-dim">{tenant.id?.slice(0, 8) ?? '—'}…</td>
      <td className="px-3 py-2 text-[13px] text-ink">{tenant.name ?? '—'}</td>
      <td className="px-3 py-2">
        <CustomSelect
          options={TIERS.map((t) => ({ value: t, label: t }))}
          value={tier}
          onChange={(v) => setTier(v as TenantTier)}
        />
      </td>
      <td className="px-3 py-2">
        <CustomSelect
          options={STATUSES.map((s) => ({ value: s, label: s }))}
          value={status}
          onChange={(v) => setStatus(v as TenantStatus)}
        />
      </td>
      <td className="px-3 py-2 font-mono text-[11px] text-dim">
        {tenant.created_at ? formatDateAuto(new Date(tenant.created_at)) : '—'}
      </td>
      <td className="px-3 py-2 flex items-center gap-2">
        {error && <span className="text-[11px] text-accent">{error}</span>}
        <button
          onClick={handleSave}
          disabled={loading}
          className="text-[11px] font-mono text-ok hover:opacity-80 disabled:opacity-50"
        >
          {loading ? '…' : 'Save'}
        </button>
        <button
          onClick={() => { setEditing(false); setTier(tenant.tier); setStatus(tenant.status); }}
          className="text-[11px] font-mono text-dim hover:text-ink"
        >
          Cancel
        </button>
      </td>
    </tr>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function TenantsPage() {
  const { tenants, tenantsLoading, tenantsError, setTenants, setTenantsLoading, setTenantsError, upsertTenant } =
    useAdminStore();

  useEffect(() => {
    let cancelled = false;
    setTenantsLoading(true);
    setTenantsError(null);
    api.admin.tenants
      .list()
      .then((data) => { if (!cancelled) setTenants(data.items); })
      .catch((err) => { if (!cancelled) setTenantsError(err instanceof ApiError ? err.message : 'Failed to load'); })
      .finally(() => { if (!cancelled) setTenantsLoading(false); });
    return () => { cancelled = true; };
  }, [setTenants, setTenantsLoading, setTenantsError]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Tenants</h1>
          <p className="text-[12px] text-dim mt-0.5">Manage all tenants on the platform</p>
        </div>
        <CreateTenantForm onCreated={upsertTenant} />
      </div>

      {tenantsError && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {tenantsError}
        </div>
      )}

      {tenantsLoading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : tenants.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No tenants</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Plan</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Status</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Created</th>
                <th className="px-3 py-2 w-16"></th>
              </tr>
            </thead>
            <tbody>
              {tenants.map((t) => (
                <EditTenantRow key={t.id} tenant={t} onPatched={upsertTenant} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
