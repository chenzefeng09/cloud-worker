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
  return p.service || p.owner_service || '其他';
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
        placeholder="搜索权限…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="border border-line rounded-sm max-h-[220px] overflow-y-auto bg-paper">
        {filtered.length === 0 ? (
          <div className="px-2 py-2 text-[11px] text-dim italic">无匹配权限</div>
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
      <div className="text-[10.5px] text-dim font-mono">已选 {selected.size} 项</div>
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
      setError('角色名须匹配 ^[a-z][a-z0-9_]{1,99}$');
      return;
    }
    if (!displayNameValid) {
      setError('请填写显示名称');
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
      setError(err instanceof ApiError ? err.message : '创建失败');
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
        + 新建角色
      </button>
      {open && (
        <Dialog title="新建角色" onClose={close} dismissable={!loading} className="w-full max-w-lg">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">角色名 (小写技术标识)</label>
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
                <span className="text-[10.5px] text-accent">须以小写字母开头，仅含小写字母/数字/下划线</span>
              )}
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">显示名称</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="知识库编辑员"
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">描述 (可选)</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">权限 (从权限目录多选)</label>
              <PermissionPicker permissions={permissions} selected={selected} onToggle={toggle} />
            </div>
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={close}
                disabled={loading}
                className="px-3 py-1.5 border border-line text-[12px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
              >
                取消
              </button>
              <button
                type="submit"
                disabled={loading || !nameValid || !displayNameValid}
                className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {loading ? '创建中…' : '创建'}
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
      setError(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      title={`编辑权限 · ${role.display_name || role.name}`}
      onClose={onClose}
      dismissable={!loading}
      className="w-full max-w-lg"
    >
      <p className="text-[11px] text-dim -mt-1">调整该角色的权限集合</p>
      {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
      <PermissionPicker permissions={permissions} selected={selected} onToggle={toggle} />
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          onClick={onClose}
          disabled={loading}
          className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
        >
          取消
        </button>
        <button
          onClick={handleSave}
          disabled={loading}
          className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          {loading ? '保存中…' : '保存'}
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
    if (!(await confirm({ title: '删除角色', message: `确定删除角色「${role.display_name || role.name}」吗？`, tone: 'danger', confirmText: '删除' }))) return;
    setDeleting(role.id);
    onError('');
    try {
      await api.admin.authz.roles.delete(role.id, tenantId ?? undefined);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '删除失败');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <CreateRoleForm tenantId={tenantId} permissions={permissions} onCreated={onReload} />
      {roles.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">暂无角色</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">名称</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">权限数</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">类型</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">操作</th>
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
                        系统
                      </span>
                    ) : (
                      <span className="font-mono text-[10px] text-ink-2 border border-line rounded-sm px-1.5 py-0.5">
                        自定义
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
                        编辑权限
                      </button>
                      <button
                        onClick={() => handleDelete(r)}
                        disabled={r.is_system || deleting === r.id}
                        className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-40 disabled:cursor-not-allowed"
                        title={r.is_system ? '系统角色不可删除' : undefined}
                      >
                        {deleting === r.id ? '…' : '删除'}
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
  'app.create': '创建应用记录',
  'app.publish': '发布已审核通过的应用版本（仅管理员/审核员）',
  'app.read': '查看应用列表与版本详情',
  'app.review': '审核提交的应用（批准/驳回，仅管理员/审核员）',
  'app.run': '从应用定义发起运行',
  'app.update': '更新应用元数据或发布新版本',
  'document.create': '上传文档到知识库',
  'document.delete': '删除知识库文档及其分块',
  'document.read': '查看知识库文档列表与详情',
  'document.search': '搜索知识库文档',
  'document.update': '更新知识库文档',
  'mcp_server.create': '注册新的 MCP 服务器',
  'mcp_server.delete': '删除 MCP 服务器',
  'mcp_server.read': '查看 MCP 服务器列表与详情',
  'mcp_server.update': '更新 MCP 服务器配置',
  'model.read': '查看可用模型列表',
  'model.admin': '管理模型配置与默认设置',
  'quota.read': '查看配额与用量',
  'quota.admin': '管理配额限制',
  'run.cancel': '取消正在执行的运行',
  'run.create': '创建并发起新的运行',
  'run.read': '查看运行记录与详情',
  'sandbox.read': '查看沙箱状态',
  'sandbox.stop': '停止运行中的沙箱',
  'session.attachment.create': '上传会话附件',
  'session.attachment.read': '查看会话附件',
  'session.create': '创建新会话',
  'session.delete': '删除会话',
  'session.message.create': '发送会话消息',
  'session.message.delete': '删除会话消息',
  'session.message.read': '查看会话消息',
  'session.read': '查看会话列表与详情',
  'session.update': '更新会话设置',
  'skill.create': '创建新技能',
  'skill.delete': '删除技能',
  'skill.read': '查看技能列表与详情',
  'skill.update': '更新技能配置',
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
          options={[{ value: '', label: '全部服务' }, ...services.map((s) => ({ value: s, label: s }))]}
          value={serviceFilter}
          onChange={(v) => setServiceFilter(v)}
        />
        <input
          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono flex-1 min-w-[180px]"
          placeholder="搜索权限名/描述…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      {grouped.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">无匹配权限</div>
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
                        双人
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
      setError(err instanceof ApiError ? err.message : '创建失败');
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
        + 新建绑定
      </button>
      {open && (
        <Dialog title="新建角色绑定" onClose={close} dismissable={!loading} className="w-full max-w-md">
          <form onSubmit={handleSubmit} className="flex flex-col gap-3">
            {error && <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>}
            <div className="grid grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">主体类型</label>
                <CustomSelect
                  options={SUBJECT_TYPES.map((s) => ({ value: s, label: s }))}
                  value={subjectType}
                  onChange={(v) => setSubjectType(v as BindingSubjectType)}
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[11px] text-dim font-mono">角色</label>
                <CustomSelect
                  options={[{ value: '', label: '— 选择角色 —' }, ...roles.map((r) => ({ value: r.id, label: r.display_name || r.name }))]}
                  value={roleId}
                  onChange={(v) => setRoleId(v)}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">主体 ID</label>
              <input
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={subjectId}
                onChange={(e) => setSubjectId(e.target.value)}
                placeholder="user-id / team-id / org-unit-id"
                required
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">过期时间 (可选 · 临时授权)</label>
              <input
                type="datetime-local"
                className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent font-mono"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[11px] text-dim font-mono">原因 (可选)</label>
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
                取消
              </button>
              <button
                type="submit"
                disabled={loading || !subjectId.trim() || !roleId}
                className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {loading ? '创建中…' : '创建'}
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
    if (!(await confirm({ title: '删除角色绑定', message: '确定删除此角色绑定吗？', tone: 'danger', confirmText: '删除' }))) return;
    setDeleting(b.id);
    onError('');
    try {
      await api.admin.authz.bindings.delete(b.id, tenantId ?? undefined);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '删除失败');
    } finally {
      setDeleting(null);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <CreateBindingForm tenantId={tenantId} roles={roles} onCreated={onReload} />
      {bindings.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">暂无绑定</div>
      ) : (
        <div className="border border-line rounded-md overflow-hidden">
          <table className="w-full text-left border-collapse">
            <thead className="bg-paper-2 border-b border-line">
              <tr>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">主体类型</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">主体 ID</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">角色</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">过期时间</th>
                <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">操作</th>
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
                      '永久'
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <button
                      onClick={() => handleDelete(b)}
                      disabled={deleting === b.id}
                      className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-50"
                    >
                      {deleting === b.id ? '…' : '删除'}
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
    tenants.find((t) => t.id === userProfile?.tenant_id)?.name ?? userProfile?.tenant_id ?? '当前租户';

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
        setError(err instanceof ApiError ? err.message : '加载失败');
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
    { key: 'roles', label: `角色 (${roles.length})` },
    { key: 'permissions', label: `权限目录 (${permissions.length})` },
    { key: 'bindings', label: `角色绑定 (${bindings.length})` },
  ];

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">角色与权限</h1>
        <p className="text-[12px] text-dim mt-0.5">管理 RBAC 角色、权限目录与角色绑定</p>
      </div>

      {/* Tenant selector — only platform admins may switch; tenant admins are
          locked to their own tenant (shown read-only). */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? '选择租户' : '当前租户'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            options={[{ value: '', label: '（默认 / 当前租户）' }, ...tenants.map((t) => ({ value: t.id, label: t.name }))]}
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
          该能力服务未启用（RBAC 代理端点尚未就绪）。接线后将自动展示。
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">加载中…</div>
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
