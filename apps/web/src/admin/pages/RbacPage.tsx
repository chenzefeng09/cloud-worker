import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
import { formatDateTimeAuto } from '../../i18n/dates';
import { useConfirm } from '../components/dialogs';
import { Dialog } from '../components/Dialog';
import { CustomSelect } from '../components/CustomSelect';
import type {
  AdminRole,
  BindingSubjectType,
  CreateBindingRequest,
  CreateRoleRequest,
  PermissionDefinition,
  RoleBinding,
} from '../../api/types';

const SUBJECT_TYPES: BindingSubjectType[] = ['USER', 'TEAM', 'ORG_UNIT'];
// Upstream authz requires lowercase role names: ^[a-z][a-z0-9_]{1,99}$.
const ROLE_NAME_RE = /^[a-z][a-z0-9_]{1,99}$/;

type Tab = 'roles' | 'permissions' | 'bindings';

/**
 * The permission catalogue payload may carry richer governance fields than the
 * shared `PermissionDefinition` type declares. Read them defensively.
 */
interface PermissionExtra extends PermissionDefinition {
  /** Upstream (authz) keys the permission as `id` (e.g. "app.create"); `name` is absent. */
  id?: string;
  service?: string;
  risk_level?: string;
  requires_mfa?: boolean;
  requires_4eyes?: boolean;
}

/** The permission identifier, tolerant of `id` (authz) vs `name` shapes. */
function permKey(p: PermissionExtra): string {
  return p.id || p.name || '';
}

function permService(p: PermissionExtra): string {
  return p.service || p.owner_service || 'Other';
}

function riskColor(level?: string): string {
  switch ((level || '').toUpperCase()) {
    case 'CRITICAL':
      return 'text-red-700 bg-red-50 border-red-200';
    case 'HIGH':
      return 'text-amber-700 bg-amber-50 border-amber-200';
    case 'MEDIUM':
      return 'text-blue-700 bg-blue-50 border-blue-200';
    case 'LOW':
      return 'text-green-700 bg-green-50 border-green-200';
    default:
      return 'text-dim bg-paper-2 border-line';
  }
}

// ─── Permission multiselect (used in create-role) ─────────────────────────────

function PermissionPicker({
  permissions,
  selected,
  onToggle,
}: {
  permissions: PermissionExtra[];
  selected: Set<string>;
  onToggle: (name: string) => void;
}) {
  const [filter, setFilter] = useState('');
  const filtered = useMemo(() => {
    const f = filter.trim().toLowerCase();
    if (!f) return permissions;
    return permissions.filter(
      (p) => permKey(p).toLowerCase().includes(f) || permService(p).toLowerCase().includes(f),
    );
  }, [permissions, filter]);

  return (
    <div className="flex flex-col gap-1">
      <input
        className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono"
        placeholder="Search permissions…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="border border-line rounded-sm max-h-[220px] overflow-y-auto bg-paper">
        {filtered.length === 0 ? (
          <div className="px-2 py-2 text-[11px] text-dim italic">No matching permissions</div>
        ) : (
          filtered.map((p) => (
            <label
              key={permKey(p)}
              className="flex items-center gap-2 px-2 py-1 hover:bg-paper-2 cursor-pointer border-b border-line-2 last:border-0"
            >
              <input
                type="checkbox"
                checked={selected.has(permKey(p))}
                onChange={() => onToggle(permKey(p))}
              />
              <span className="font-mono text-[11px] text-ink-2 flex-1 truncate">{permKey(p)}</span>
              <span className="font-mono text-[10px] text-dim">{permService(p)}</span>
            </label>
          ))
        )}
      </div>
      <div className="text-[10.5px] text-dim font-mono">Selected: {selected.size}</div>
    </div>
  );
}

// ─── Create Role Form ─────────────────────────────────────────────────────────

function CreateRoleForm({
  tenantId,
  permissions,
  onCreated,
}: {
  tenantId: string | null;
  permissions: PermissionExtra[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [description, setDescription] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameValid = ROLE_NAME_RE.test(name);
  const displayNameValid = displayName.trim().length > 0;

  const reset = () => {
    setName('');
    setDisplayName('');
    setDescription('');
    setSelected(new Set());
    setError(null);
  };

  const toggle = (n: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!nameValid) {
      setError('Role name must match ^[a-z][a-z0-9_]{1,99}$');
      return;
    }
    if (!displayNameValid) {
      setError('Enter a display name');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const body: CreateRoleRequest = {
        name: name.trim(),
        display_name: displayName.trim(),
        permissions: Array.from(selected),
      };
      if (description.trim()) body.description = description.trim();
      await api.admin.authz.roles.create(body, tenantId ?? undefined);
      reset();
      setOpen(false);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create');
    } finally {
      setLoading(false);
    }
  };

  const close = () => { reset(); setOpen(false); };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 self-start"
      >
        + New role
      </button>
      {open && (
        <Dialog title="New role" onClose={close} dismissable={!loading} className="w-full max-w-lg">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Role name (lowercase identifier)</label>
              <input
                className={`border rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none font-mono ${
                  name && !nameValid ? 'border-accent focus:border-accent' : 'border-line focus:border-accent'
                }`}
                value={name}
                onChange={(e) => setName(e.target.value.toLowerCase())}
                placeholder="kb_editor"
                required
              />
              {name && !nameValid && (
                <span className="text-[10.5px] text-accent">Must start with a lowercase letter and contain only lowercase letters, digits or underscores</span>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Display name</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Knowledge base editor"
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Description (optional)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Permissions (select from catalog)</label>
              <PermissionPicker permissions={permissions} selected={selected} onToggle={toggle} />
            </div>
            <div className="flex items-center justify-end gap-2 pt-1">
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
                disabled={loading || !nameValid || !displayNameValid}
                className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
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

// ─── Edit Role Permissions ────────────────────────────────────────────────────

function EditRolePermissions({
  role,
  tenantId,
  permissions,
  onClose,
  onSaved,
}: {
  role: AdminRole;
  tenantId: string | null;
  permissions: PermissionExtra[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set(role.permissions ?? []));
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggle = (n: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  const handleSave = async () => {
    setLoading(true);
    setError(null);
    try {
      await api.admin.authz.roles.patch(
        role.id,
        { permissions: Array.from(selected) },
        tenantId ?? undefined,
      );
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to save');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      title={`Edit permissions · ${role.display_name || role.name}`}
      onClose={onClose}
      dismissable={!loading}
      className="w-full max-w-lg"
    >
      <p className="text-[11px] text-dim -mt-1">Adjust this role’s permission set</p>
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      <PermissionPicker permissions={permissions} selected={selected} onToggle={toggle} />
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          onClick={onClose}
          disabled={loading}
          className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          onClick={handleSave}
          disabled={loading}
          className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          {loading ? 'Saving…' : 'Save'}
        </button>
      </div>
    </Dialog>
  );
}

// ─── Roles Tab ────────────────────────────────────────────────────────────────

function RolesTab({
  tenantId,
  roles,
  permissions,
  onReload,
  onError,
}: {
  tenantId: string | null;
  roles: AdminRole[];
  permissions: PermissionExtra[];
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [editing, setEditing] = useState<AdminRole | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  const confirm = useConfirm();

  const handleDelete = async (role: AdminRole) => {
    if (role.is_system) return;
    if (!(await confirm({ title: 'Delete role', message: `Delete role "${role.display_name || role.name}"?`, tone: 'danger', confirmText: 'Delete' }))) return;
    setDeleting(role.id);
    onError('');
    try {
      await api.admin.authz.roles.delete(role.id, tenantId ?? undefined);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : 'Failed to delete');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <CreateRoleForm tenantId={tenantId} permissions={permissions} onCreated={onReload} />
      {roles.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No roles</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Permissions</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Type</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Actions</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((r) => (
                <tr key={r.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2">
                    <div className="text-[13px] text-ink">{r.display_name || r.name}</div>
                    {r.display_name && (
                      <div className="font-mono text-[10.5px] text-dim mt-0.5">{r.name}</div>
                    )}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px] text-dim">{r.permissions?.length ?? 0}</td>
                  <td className="px-3 py-2">
                    {r.is_system ? (
                      <span className="font-mono text-[10px] text-dim border border-line rounded-sm px-1.5 py-0.5">
                        System
                      </span>
                    ) : (
                      <span className="font-mono text-[10px] text-ink-2 border border-line rounded-sm px-1.5 py-0.5">
                        Custom
                      </span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center gap-2">
                      <button
                        onClick={() => setEditing(r)}
                        disabled={r.is_system}
                        className="text-[11px] font-mono text-dim border border-line rounded-sm px-2 py-0.5 hover:text-ink hover:bg-paper-2 disabled:opacity-40 disabled:cursor-not-allowed"
                      >
                        Edit permissions
                      </button>
                      <button
                        onClick={() => handleDelete(r)}
                        disabled={r.is_system || deleting === r.id}
                        className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
                        title={r.is_system ? 'System roles can’t be deleted' : undefined}
                      >
                        {deleting === r.id ? '…' : 'Delete'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {editing && (
        <EditRolePermissions
          role={editing}
          tenantId={tenantId}
          permissions={permissions}
          onClose={() => setEditing(null)}
          onSaved={onReload}
        />
      )}
    </div>
  );
}

// ─── Permissions Tab ──────────────────────────────────────────────────────────

const PERM_DESC_ZH: Record<string, string> = {
  'app.create': 'Create app records',
  'app.publish': 'Publish approved app versions (admins/reviewers only)',
  'app.read': 'View apps and version details',
  'app.review': 'Review submitted apps (approve/reject; admins/reviewers only)',
  'app.run': 'Start runs from an app definition',
  'app.update': 'Update app metadata or publish new versions',
  'document.create': 'Upload documents to the knowledge base',
  'document.delete': 'Delete knowledge base documents and their chunks',
  'document.read': 'View knowledge base documents and details',
  'document.search': 'Search knowledge base documents',
  'document.update': 'Update knowledge base documents',
  'mcp_server.create': 'Register new MCP servers',
  'mcp_server.delete': 'Delete MCP servers',
  'mcp_server.read': 'View MCP servers and details',
  'mcp_server.update': 'Update MCP server configuration',
  'model.read': 'View available models',
  'model.admin': 'Manage model configuration and defaults',
  'quota.read': 'View quotas and usage',
  'quota.admin': 'Manage quota limits',
  'run.cancel': 'Cancel in-progress runs',
  'run.create': 'Create and start new runs',
  'run.read': 'View runs and details',
  'sandbox.read': 'View sandbox status',
  'sandbox.stop': 'Stop running sandboxes',
  'session.attachment.create': 'Upload session attachments',
  'session.attachment.read': 'View session attachments',
  'session.create': 'Create sessions',
  'session.delete': 'Delete sessions',
  'session.message.create': 'Send session messages',
  'session.message.delete': 'Delete session messages',
  'session.message.read': 'View session messages',
  'session.read': 'View sessions and details',
  'session.update': 'Update session settings',
  'skill.create': 'Create skills',
  'skill.delete': 'Delete skills',
  'skill.read': 'View skills and details',
  'skill.update': 'Update skill configuration',
};

function PermissionsTab({ permissions }: { permissions: PermissionExtra[] }) {
  const [serviceFilter, setServiceFilter] = useState('');
  const [search, setSearch] = useState('');

  const services = useMemo(() => {
    const set = new Set<string>();
    permissions.forEach((p) => set.add(permService(p)));
    return Array.from(set).sort();
  }, [permissions]);

  const grouped = useMemo(() => {
    const s = search.trim().toLowerCase();
    const filtered = permissions.filter((p) => {
      if (serviceFilter && permService(p) !== serviceFilter) return false;
      if (
        s &&
        !permKey(p).toLowerCase().includes(s) &&
        !(p.description ?? '').toLowerCase().includes(s) &&
        !(PERM_DESC_ZH[permKey(p)] ?? '').includes(s)
      )
        return false;
      return true;
    });
    const map = new Map<string, PermissionExtra[]>();
    for (const p of filtered) {
      const svc = permService(p);
      if (!map.has(svc)) map.set(svc, []);
      map.get(svc)!.push(p);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [permissions, serviceFilter, search]);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3 flex-wrap">
        <CustomSelect
          options={[{ value: '', label: 'All services' }, ...services.map((s) => ({ value: s, label: s }))]}
          value={serviceFilter}
          onChange={(v) => setServiceFilter(v)}
        />
        <input
          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono flex-1 min-w-[180px]"
          placeholder="Search permission name/description…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {grouped.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No matching permissions</div>
      ) : (
        grouped.map(([svc, perms]) => (
          <div key={svc} className="border border-line rounded-md overflow-hidden">
            <div className="bg-paper-2 px-3 py-2 border-b border-line font-mono text-[11px] text-ink-2 tracking-[0.05em]">
              {svc} <span className="text-dim">· {perms.length}</span>
            </div>
            <div className="divide-y divide-line-2">
              {perms.map((p) => (
                <div key={permKey(p)} className="px-3 py-2 flex items-start gap-3">
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-[12px] text-ink truncate">{permKey(p)}</div>
                    {(PERM_DESC_ZH[permKey(p)] || p.description) && (
                      <div className="text-[11px] text-dim mt-0.5">{PERM_DESC_ZH[permKey(p)] ?? p.description}</div>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {p.risk_level && (
                      <span
                        className={`font-mono text-[9.5px] border rounded-sm px-1.5 py-0.5 ${riskColor(p.risk_level)}`}
                      >
                        {p.risk_level}
                      </span>
                    )}
                    {p.requires_mfa && (
                      <span className="font-mono text-[9.5px] text-violet-700 bg-violet-50 border border-violet-200 rounded-sm px-1.5 py-0.5">
                        MFA
                      </span>
                    )}
                    {p.requires_4eyes && (
                      <span className="font-mono text-[9.5px] text-amber-700 bg-amber-50 border border-amber-200 rounded-sm px-1.5 py-0.5">
                        Dual control
                      </span>
                    )}
                    {p.requires_policy && (
                      <span className="font-mono text-[9.5px] text-blue-700 bg-blue-50 border border-blue-200 rounded-sm px-1.5 py-0.5">
                        OPA
                      </span>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        ))
      )}
    </div>
  );
}

// ─── Bindings Tab ─────────────────────────────────────────────────────────────

function CreateBindingForm({
  tenantId,
  roles,
  onCreated,
}: {
  tenantId: string | null;
  roles: AdminRole[];
  onCreated: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [subjectType, setSubjectType] = useState<BindingSubjectType>('USER');
  const [subjectId, setSubjectId] = useState('');
  const [roleId, setRoleId] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setSubjectType('USER');
    setSubjectId('');
    setRoleId('');
    setExpiresAt('');
    setReason('');
    setError(null);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!subjectId.trim() || !roleId) return;
    setLoading(true);
    setError(null);
    try {
      const body: CreateBindingRequest = {
        subject_type: subjectType,
        subject_id: subjectId.trim(),
        role_id: roleId,
      };
      if (expiresAt) body.expires_at = new Date(expiresAt).toISOString();
      if (reason.trim()) body.reason = reason.trim();
      await api.admin.authz.bindings.create(body, tenantId ?? undefined);
      reset();
      setOpen(false);
      onCreated();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create');
    } finally {
      setLoading(false);
    }
  };

  const close = () => { reset(); setOpen(false); };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 self-start"
      >
        + New binding
      </button>
      {open && (
        <Dialog title="New role binding" onClose={close} dismissable={!loading} className="w-full max-w-md">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Subject type</label>
                <CustomSelect
                  options={SUBJECT_TYPES.map((s) => ({ value: s, label: s }))}
                  value={subjectType}
                  onChange={(v) => setSubjectType(v as BindingSubjectType)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">Role</label>
                <CustomSelect
                  options={[{ value: '', label: '— Select role —' }, ...roles.map((r) => ({ value: r.id, label: r.display_name || r.name }))]}
                  value={roleId}
                  onChange={(v) => setRoleId(v)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Subject ID</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={subjectId}
                onChange={(e) => setSubjectId(e.target.value)}
                placeholder="user-id / team-id / org-unit-id"
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Expires at (optional · temporary grant)</label>
              <input
                type="datetime-local"
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">Reason (optional)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </div>
            <div className="flex items-center justify-end gap-2 pt-1">
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
                disabled={loading || !subjectId.trim() || !roleId}
                className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
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

function BindingsTab({
  tenantId,
  bindings,
  roles,
  onReload,
  onError,
}: {
  tenantId: string | null;
  bindings: RoleBinding[];
  roles: AdminRole[];
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [deleting, setDeleting] = useState<string | null>(null);
  const roleName = (id?: string) => {
    const r = roles.find((x) => x.id === id);
    return r?.display_name || r?.name || id || '—';
  };
  const confirm = useConfirm();

  const handleDelete = async (b: RoleBinding) => {
    if (!(await confirm({ title: 'Delete role binding', message: 'Delete this role binding?', tone: 'danger', confirmText: 'Delete' }))) return;
    setDeleting(b.id);
    onError('');
    try {
      await api.admin.authz.bindings.delete(b.id, tenantId ?? undefined);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : 'Failed to delete');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <CreateBindingForm tenantId={tenantId} roles={roles} onCreated={onReload} />
      {bindings.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No bindings</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Subject type</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Subject ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Role</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Expires at</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Actions</th>
              </tr>
            </thead>
            <tbody>
              {bindings.map((b) => (
                <tr key={b.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{b.subject_type ?? '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim truncate max-w-[160px]">
                    {b.subject_id ?? '—'}
                  </td>
                  <td className="px-3 py-2 font-mono text-[12px] text-ink">{roleName(b.role_id)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-dim">
                    {b.expires_at ? (
                      <span className="text-amber-700">{formatDateTimeAuto(new Date(b.expires_at))}</span>
                    ) : (
                      'Never'
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => handleDelete(b)}
                      disabled={deleting === b.id}
                      className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-50"
                    >
                      {deleting === b.id ? '…' : 'Delete'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function RbacPage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();
  const userProfile = useChatStore((s) => s.userProfile);
  // Platform admins may pick any tenant; tenant admins are locked to their own
  // tenant (selectedTenantId stays null → BFF omits ?tenant_id= → own tenant).
  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);
  const ownTenantName =
    tenants.find((t) => t.id === userProfile?.tenant_id)?.name ?? userProfile?.tenant_id ?? 'Current tenant';

  const [tab, setTab] = useState<Tab>('roles');
  const [roles, setRoles] = useState<AdminRole[]>([]);
  const [permissions, setPermissions] = useState<PermissionExtra[]>([]);
  const [bindings, setBindings] = useState<RoleBinding[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);

  // Bootstrap tenant list
  useEffect(() => {
    if (tenants.length > 0) return;
    setTenantsLoading(true);
    api.admin.tenants
      .list()
      .then((data) => setTenants(data.items))
      .catch(() => {/* ignore */})
      .finally(() => setTenantsLoading(false));
  }, [tenants.length, setTenants, setTenantsLoading]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    const tid = selectedTenantId ?? undefined;
    try {
      const [roleList, permList, bindingList] = await Promise.all([
        api.admin.authz.roles.list(tid),
        api.admin.authz.permissions.list(),
        api.admin.authz.bindings.list(tid),
      ]);
      setRoles(roleList);
      setPermissions(permList as PermissionExtra[]);
      setBindings(bindingList);
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
        setIsTodoEndpoint(true);
      } else {
        setError(err instanceof ApiError ? err.message : 'Failed to load');
      }
    } finally {
      setLoading(false);
    }
  }, [selectedTenantId]);

  // Tenant admins must never carry a cross-tenant selection (the adminStore is
  // shared and could hold a stale id from a prior platform-admin session).
  useEffect(() => {
    if (!canSwitchTenant && selectedTenantId !== null) {
      setSelectedTenantId(null);
    }
  }, [canSwitchTenant, selectedTenantId, setSelectedTenantId]);

  useEffect(() => {
    load();
  }, [load]);

  const TABS: { key: Tab; label: string }[] = [
    { key: 'roles', label: `Roles (${roles.length})` },
    { key: 'permissions', label: `Permission catalog (${permissions.length})` },
    { key: 'bindings', label: `Role bindings (${bindings.length})` },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Roles & permissions</h1>
        <p className="text-[12px] text-dim mt-0.5">Manage RBAC roles, the permission catalog and role bindings</p>
      </div>

      {/* Tenant selector — only platform admins may switch; tenant admins are
          locked to their own tenant (shown read-only). */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? 'Tenant' : 'Current tenant'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            options={[{ value: '', label: '(Default / current tenant)' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
            value={selectedTenantId ?? ''}
            onChange={(v) => setSelectedTenantId(v || null)}
            disabled={tenantsLoading}
            className="min-w-[200px]"
          />
        ) : (
          <span className="min-w-[200px] text-[13px] text-ink font-mono px-3 py-1.5 rounded-sm border border-line bg-surface">
            {ownTenantName}
          </span>
        )}
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-line">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-3 py-2 text-[12px] font-mono -mb-px border-b-2 transition-colors ${
              tab === t.key
                ? 'border-accent text-ink'
                : 'border-transparent text-dim hover:text-ink-2'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          This service isn’t enabled yet (the RBAC proxy endpoint isn’t ready). Data will appear here once it’s connected.
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : (
        !isTodoEndpoint && (
          <>
            {tab === 'roles' && (
              <RolesTab
                tenantId={selectedTenantId}
                roles={roles}
                permissions={permissions}
                onReload={load}
                onError={(m) => setError(m || null)}
              />
            )}
            {tab === 'permissions' && <PermissionsTab permissions={permissions} />}
            {tab === 'bindings' && (
              <BindingsTab
                tenantId={selectedTenantId}
                bindings={bindings}
                roles={roles}
                onReload={load}
                onError={(m) => setError(m || null)}
              />
            )}
          </>
        )
      )}
    </div>
  );
}
