import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useConfirm, useToast } from '../components/dialogs';
import { CustomSelect, SearchableSelect } from '../components/CustomSelect';
import type {
  AdminUser,
  LargeAttachmentTenantSetting,
  LargeAttachmentSubjectAllow,
  LargeAttachmentSubjectType,
  OrgUnit,
  Team,
} from '../../api/types';

const isTodo = (err: unknown) =>
  err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502);

function fmt(iso?: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : formatDateTimeAuto(d);
}

const SUBJECT_TYPE_LABELS: Record<LargeAttachmentSubjectType, string> = {
  USER: 'User',
  TEAM: 'Group',
  ORG_UNIT: 'Org unit',
};

export function LargeAttachmentPage() {
  const {
    tenants,
    tenantsLoading,
    setTenants,
    setTenantsLoading,
    selectedTenantId,
    setSelectedTenantId,
  } = useAdminStore();
  const userProfile = useChatStore((s) => s.userProfile);
  const toast = useToast();
  const confirm = useConfirm();

  // Platform admins may pick any tenant; tenant admins are locked to their own
  // tenant. The shared adminStore may hold a stale cross-tenant id from a prior
  // platform-admin session, so we never trust selectedTenantId for a tenant_admin.
  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);
  const ownTenantId = userProfile?.tenant_id ?? null;
  const ownTenantName = tenants.find((t) => t.id === ownTenantId)?.name ?? ownTenantId ?? 'Current tenant';

  // Which tenant we manage: platform-admins follow the selector; tenant-admins
  // are pinned to their own tenant (so the page loads immediately).
  const effectiveTenantId = canSwitchTenant ? selectedTenantId : ownTenantId;
  // What we actually send to the BFF: only a platform-admin acting cross-tenant
  // sends an override. A tenant_admin sends undefined → BFF uses the caller's
  // own tenant and the cross-tenant gate stays in the same-tenant lane.
  const apiTenantId = canSwitchTenant ? (selectedTenantId ?? undefined) : undefined;
  // ``tid`` gates "is a tenant chosen / data loadable"; ``apiTid`` is the value
  // forwarded to the API client.
  const tid = effectiveTenantId ?? undefined;
  const apiTid = apiTenantId;

  const [setting, setSetting] = useState<LargeAttachmentTenantSetting | null>(null);
  const [allows, setAllows] = useState<LargeAttachmentSubjectAllow[]>([]);
  const [loading, setLoading] = useState(false);
  const [todo, setTodo] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [toggleBusy, setToggleBusy] = useState(false);

  // Tenant rosters — back the searchable pickers.
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [orgUnits, setOrgUnits] = useState<OrgUnit[]>([]);

  // Add-subject form.
  const [newSubjectType, setNewSubjectType] = useState<LargeAttachmentSubjectType>('USER');
  const [newSubjectId, setNewSubjectId] = useState('');
  const [addBusy, setAddBusy] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const enabled = setting?.large_attachment_enabled ?? false;

  // Bootstrap tenant list.
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
    if (!tid) {
      setSetting(null);
      setAllows([]);
      return;
    }
    setLoading(true);
    setError(null);
    setTodo(false);
    Promise.all([
      api.admin.largeAttachment.tenantSetting(apiTid),
      api.admin.largeAttachment.subjectAllows(apiTid),
    ])
      .then(([s, a]) => {
        setSetting(s);
        setAllows(a);
      })
      .catch((err) => {
        if (isTodo(err)) setTodo(true);
        else setError(err instanceof ApiError ? err.message : 'Failed to load');
      })
      .finally(() => setLoading(false));
  }, [tid, apiTid]);

  useEffect(() => {
    load();
  }, [load]);

  // Tenant admins must never carry a cross-tenant selection in the shared store.
  useEffect(() => {
    if (!canSwitchTenant && selectedTenantId !== null) {
      setSelectedTenantId(null);
    }
  }, [canSwitchTenant, selectedTenantId, setSelectedTenantId]);

  // Load the tenant's rosters for the searchable pickers. Best-effort:
  // failures just leave pickers empty (the toggle/whitelist still work).
  useEffect(() => {
    if (!tid) {
      setUsers([]);
      setTeams([]);
      setOrgUnits([]);
      return;
    }
    let cancelled = false;
    api.admin.users
      .list(apiTid)
      .then((data) => { if (!cancelled) setUsers(data.items); })
      .catch(() => { if (!cancelled) setUsers([]); });
    api.admin.org.teams
      .list(apiTid)
      .then((data) => { if (!cancelled) setTeams(data); })
      .catch(() => { if (!cancelled) setTeams([]); });
    api.admin.org.orgUnits
      .list(apiTid)
      .then((data) => { if (!cancelled) setOrgUnits(data); })
      .catch(() => { if (!cancelled) setOrgUnits([]); });
    return () => { cancelled = true; };
  }, [tid, apiTid]);

  // Picker options per subject type — exclude already-listed subjects.
  const takenUsers = useMemo(
    () => new Set(allows.filter((a) => a.subject_type === 'USER').map((a) => a.subject_id)),
    [allows],
  );
  const takenTeams = useMemo(
    () => new Set(allows.filter((a) => a.subject_type === 'TEAM').map((a) => a.subject_id)),
    [allows],
  );
  const takenOrgUnits = useMemo(
    () => new Set(allows.filter((a) => a.subject_type === 'ORG_UNIT').map((a) => a.subject_id)),
    [allows],
  );

  const userOptions = useMemo(
    () =>
      users
        .filter((u) => !takenUsers.has(u.id))
        .map((u) => ({
          value: u.id,
          label: u.display_name ? `${u.display_name} · ${u.email} · ${u.id}` : `${u.email} · ${u.id}`,
        })),
    [users, takenUsers],
  );
  const teamOptions = useMemo(
    () =>
      teams
        .filter((t) => !takenTeams.has(t.id))
        .map((t) => ({ value: t.id, label: `${t.name} · ${t.id}` })),
    [teams, takenTeams],
  );
  const orgUnitOptions = useMemo(
    () =>
      orgUnits
        .filter((o) => !takenOrgUnits.has(o.id))
        .map((o) => ({ value: o.id, label: o.path ? `${o.path} · ${o.id}` : `${o.name} · ${o.id}` })),
    [orgUnits, takenOrgUnits],
  );

  // id → display name maps for the table.
  const userMap = useMemo(
    () => new Map(users.map((u) => [u.id, u.display_name || u.email || u.id])),
    [users],
  );
  const teamMap = useMemo(() => new Map(teams.map((t) => [t.id, t.name])), [teams]);
  const orgUnitMap = useMemo(
    () => new Map(orgUnits.map((o) => [o.id, o.path || o.name || o.id])),
    [orgUnits],
  );

  function resolveSubjectName(row: LargeAttachmentSubjectAllow): string {
    switch (row.subject_type) {
      case 'USER': return userMap.get(row.subject_id) ?? row.subject_id;
      case 'TEAM': return teamMap.get(row.subject_id) ?? row.subject_id;
      case 'ORG_UNIT': return orgUnitMap.get(row.subject_id) ?? row.subject_id;
    }
  }

  const handleToggle = async () => {
    if (!tid || toggleBusy) return;
    const next = !enabled;
    setToggleBusy(true);
    try {
      const updated = await api.admin.largeAttachment.setTenantSetting(next, apiTid);
      setSetting(updated);
      toast.success(next ? 'Large attachments enabled for tenant' : 'Large attachments disabled for tenant');
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Operation failed');
    } finally {
      setToggleBusy(false);
    }
  };

  const handleAdd = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!tid || !newSubjectId.trim()) return;
    setAddBusy(true);
    setAddError(null);
    try {
      await api.admin.largeAttachment.setSubjectAllow(newSubjectType, newSubjectId.trim(), true, apiTid);
      toast.success('Added to allowlist');
      setNewSubjectId('');
      load();
    } catch (err) {
      setAddError(err instanceof ApiError ? err.message : 'Failed to add');
    } finally {
      setAddBusy(false);
    }
  };

  const handleToggleAllow = async (row: LargeAttachmentSubjectAllow) => {
    if (!tid) return;
    try {
      await api.admin.largeAttachment.setSubjectAllow(row.subject_type, row.subject_id, !row.allowed, apiTid);
      toast.success('Updated');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Operation failed');
    }
  };

  const handleRemove = async (row: LargeAttachmentSubjectAllow) => {
    if (!tid) return;
    const label = SUBJECT_TYPE_LABELS[row.subject_type];
    if (
      !(await confirm({
        title: 'Remove from allowlist',
        message: `Remove ${label.toLowerCase()} ${resolveSubjectName(row)} from the large-attachment allowlist?`,
        tone: 'danger',
        confirmText: 'Remove',
      }))
    )
      return;
    try {
      await api.admin.largeAttachment.removeSubjectAllow(row.subject_type, row.subject_id, apiTid);
      toast.success('Removed');
      load();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : 'Failed to remove');
    }
  };

  // Picker component for the currently selected subject type.
  function renderPicker() {
    const sharedProps = {
      value: newSubjectId,
      onChange: setNewSubjectId,
      disabled: !enabled,
      className: 'min-w-[300px]',
    };
    switch (newSubjectType) {
      case 'USER':
        return (
          <SearchableSelect
            {...sharedProps}
            options={userOptions}
            placeholder="Select a user…"
            searchPlaceholder="Search by name / email / ID…"
            emptyText={users.length === 0 ? 'No users in this tenant' : 'No matching users'}
          />
        );
      case 'TEAM':
        return (
          <SearchableSelect
            {...sharedProps}
            options={teamOptions}
            placeholder="Select a group…"
            searchPlaceholder="Search by name / ID…"
            emptyText={teams.length === 0 ? 'No groups in this tenant' : 'No matching groups'}
          />
        );
      case 'ORG_UNIT':
        return (
          <SearchableSelect
            {...sharedProps}
            options={orgUnitOptions}
            placeholder="Select an org unit…"
            searchPlaceholder="Search by path / name / ID…"
            emptyText={orgUnits.length === 0 ? 'No org units in this tenant' : 'No matching org units'}
          />
        );
    }
  }

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Large attachments</h1>
        <p className="text-[12px] text-dim mt-0.5">
          Two-layer access: a tenant-level switch plus a subject allowlist. When enabled, attachments above the threshold run in the high-memory sandbox tier.
        </p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {/* ─── 租户选择 ─────────────────────────────────────────── */}
      {/* Platform admins may switch tenants; tenant admins are locked to their
          own tenant (shown read-only). */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? 'Select tenant' : 'Current tenant'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            options={[{ value: '', label: '— Select —' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
            value={selectedTenantId ?? ''}
            onChange={(v) => {
              setSelectedTenantId(v || null);
              setSetting(null);
              setAllows([]);
            }}
            disabled={tenantsLoading}
            className="min-w-[200px]"
          />
        ) : (
          <span className="min-w-[200px] text-[13px] text-ink font-mono px-3 py-1.5 rounded-sm border border-line bg-surface">
            {ownTenantName}
          </span>
        )}
      </div>

      {canSwitchTenant && !tid && (
        <div className="font-mono text-[12px] text-dim italic">Select a tenant to manage its large-attachment access</div>
      )}

      {tid && todo && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          This service isn’t enabled (the large-attachment proxy endpoint is unreachable).
        </div>
      )}

      {tid && !todo && (
        <>
          {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}

          {/* ─── Section 1: 租户大附件开关 ─────────────────────── */}
          {!loading && (
            <section className="flex flex-col gap-2">
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Tenant switch</div>
              <div className="border border-line rounded-md p-4 bg-surface flex items-center justify-between gap-4">
                <div className="flex flex-col gap-1">
                  <div className="text-[14px] text-ink">
                    {enabled ? 'Enabled' : 'Disabled'}
                  </div>
                  <div className="font-mono text-[11px] text-dim">
                    Updated {fmt(setting?.updated_at)} · by {setting?.updated_by || '—'}
                  </div>
                </div>
                <button
                  onClick={handleToggle}
                  disabled={toggleBusy}
                  role="switch"
                  aria-checked={enabled}
                  className={[
                    'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors disabled:opacity-50',
                    enabled ? 'bg-ink' : 'bg-line-2',
                  ].join(' ')}
                >
                  <span
                    className={[
                      'inline-block h-4 w-4 transform rounded-full bg-surface transition-transform',
                      enabled ? 'translate-x-6' : 'translate-x-1',
                    ].join(' ')}
                  />
                </button>
              </div>
            </section>
          )}

          {/* ─── Section 2: 准入白名单 ────────────────────────── */}
          {!loading && (
            <section className={`flex flex-col gap-2 ${enabled ? '' : 'opacity-50 pointer-events-none select-none'}`}>
              <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim flex items-center gap-2">
                Allowlist
                {!enabled && <span className="text-dim normal-case tracking-normal">(enable the tenant switch first)</span>}
              </div>

              <form onSubmit={handleAdd} className="flex items-end gap-2 flex-wrap">
                {addError && (
                  <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm w-full">{addError}</div>
                )}
                <label className="flex flex-col gap-1">
                  <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">Subject type</span>
                  <CustomSelect
                    options={[
                      { value: 'USER', label: 'User' },
                      { value: 'TEAM', label: 'Group' },
                      { value: 'ORG_UNIT', label: 'Org unit' },
                    ]}
                    value={newSubjectType}
                    onChange={(v) => {
                      setNewSubjectType(v as LargeAttachmentSubjectType);
                      setNewSubjectId('');
                    }}
                    disabled={!enabled}
                    className="min-w-[120px]"
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="font-mono text-[10px] text-dim uppercase tracking-[0.08em]">
                    {SUBJECT_TYPE_LABELS[newSubjectType]}
                  </span>
                  {renderPicker()}
                </label>
                <button
                  type="submit"
                  disabled={addBusy || !enabled || !newSubjectId.trim()}
                  className="px-3 py-1.5 text-[11px] font-mono bg-ink text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
                >
                  {addBusy ? 'Adding…' : 'Add to allowlist'}
                </button>
              </form>

              <div className="border border-line rounded-md overflow-hidden">
                <div className="bg-paper-2 px-3 py-2 border-b border-line font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
                  Allowlist ({allows.length})
                </div>
                {allows.length > 0 ? (
                  <table className="w-full text-left border-collapse">
                    <thead className="border-b border-line-2">
                      <tr>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim">Type</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim">Subject</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim">Status</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim">Updated</th>
                        <th className="px-3 py-2 font-mono text-[10.5px] text-dim text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {allows.map((row) => (
                        <tr key={`${row.subject_type}:${row.subject_id}`} className="border-b border-line-2 hover:bg-paper-2">
                          <td className="px-3 py-2">
                            <span className="inline-flex items-center font-mono text-[10px] border rounded-sm px-1.5 py-0.5 text-ink-2 bg-paper-2 border-line">
                              {SUBJECT_TYPE_LABELS[row.subject_type]}
                            </span>
                          </td>
                          <td className="px-3 py-2 font-mono text-[12px] text-ink">
                            {resolveSubjectName(row)}
                            <span className="text-dim ml-1.5 text-[10px]">{row.subject_id}</span>
                          </td>
                          <td className="px-3 py-2">
                            <span
                              className={`inline-flex items-center font-mono text-[10px] border rounded-sm px-1.5 py-0.5 ${
                                row.allowed
                                  ? 'text-green-700 bg-green-50 border-green-200'
                                  : 'text-gray-600 bg-gray-50 border-gray-200'
                              }`}
                            >
                              {row.allowed ? 'Allowed' : 'Blocked'}
                            </span>
                          </td>
                          <td className="px-3 py-2 font-mono text-[11px] text-dim">{fmt(row.updated_at)}</td>
                          <td className="px-3 py-2 text-right whitespace-nowrap">
                            <button
                              onClick={() => handleToggleAllow(row)}
                              className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 mr-2"
                            >
                              {row.allowed ? 'Block' : 'Allow'}
                            </button>
                            <button
                              onClick={() => handleRemove(row)}
                              className="px-2 py-1 text-[11px] font-mono border border-red-200 text-red-600 rounded-sm hover:bg-red-50"
                            >
                              Remove
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : (
                  <div className="px-3 py-4 font-mono text-[12px] text-dim italic">No subjects on the allowlist</div>
                )}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}

