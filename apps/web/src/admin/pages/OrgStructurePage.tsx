import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useConfirm, usePrompt } from '../components/dialogs';
import type {
  AddTeamMemberRequest,
  AdminUser,
  CreateOrgUnitRequest,
  CreateTeamRequest,
  OrgUnit,
  Team,
  TeamMember,
} from '../../api/types';

// ─── Org-unit tree helpers ────────────────────────────────────────────────────

interface OrgNode extends OrgUnit {
  children: OrgNode[];
}

function buildTree(units: OrgUnit[]): OrgNode[] {
  const byId = new Map<string, OrgNode>();
  units.forEach((u) => byId.set(u.id, { ...u, children: [] }));
  const roots: OrgNode[] = [];
  byId.forEach((node) => {
    const pid = node.parent_id;
    if (pid && byId.has(pid)) {
      byId.get(pid)!.children.push(node);
    } else {
      roots.push(node);
    }
  });
  return roots;
}

// ─── Org-unit tree node ───────────────────────────────────────────────────────

function OrgUnitNode({
  node,
  depth,
  units,
  tenantId,
  onReload,
  onError,
}: {
  node: OrgNode;
  depth: number;
  units: OrgUnit[];
  tenantId: string | null;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();
  const prompt = usePrompt();

  const wrap = async (fn: () => Promise<void>) => {
    setBusy(true);
    onError('');
    try {
      await fn();
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '操作失败');
    } finally {
      setBusy(false);
    }
  };

  const handleAddChild = async () => {
    const name = await prompt({ title: '新建子部门', label: '部门名称', required: true });
    if (!name || !name.trim()) return;
    const body: CreateOrgUnitRequest = { name: name.trim(), parent_id: node.id };
    wrap(() => api.admin.org.orgUnits.create(body, tenantId ?? undefined).then(() => undefined));
  };

  const handleRename = async () => {
    const name = await prompt({
      title: '重命名部门',
      label: '新名称',
      defaultValue: node.name,
      required: true,
    });
    if (!name || !name.trim() || name.trim() === node.name) return;
    wrap(() => api.admin.org.orgUnits.patch(node.id, { name: name.trim() }, tenantId ?? undefined).then(() => undefined));
  };

  const handleMove = async () => {
    const candidates = units.filter((u) => u.id !== node.id);
    const list = candidates.map((u) => `${u.id}  ${u.path ?? u.name}`).join('\n');
    const target = await prompt({
      title: '移动部门',
      label: '新父部门 ID',
      description: `留空移动到根。\n\n可选父节点：\n${list}`,
      defaultValue: node.parent_id ?? '',
    });
    if (target === null) return;
    const parent_id = target.trim() || undefined;
    if (parent_id === node.id) {
      onError('不能移动到自身');
      return;
    }
    wrap(() => api.admin.org.orgUnits.move(node.id, { parent_id }, tenantId ?? undefined).then(() => undefined));
  };

  const handleDelete = async () => {
    if (node.children.length > 0) {
      onError('请先删除子部门');
      return;
    }
    if (
      !(await confirm({
        title: '删除部门',
        message: `确定删除部门「${node.name}」吗？`,
        tone: 'danger',
        confirmText: '删除',
      }))
    )
      return;
    wrap(() => api.admin.org.orgUnits.delete(node.id, tenantId ?? undefined));
  };

  return (
    <div>
      <div
        className="group flex items-center gap-1.5 py-1.5 px-2 rounded-sm hover:bg-paper-2"
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {node.children.length > 0 ? (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-[10px] text-dim font-mono w-3 shrink-0"
          >
            {expanded ? '▼' : '▶'}
          </button>
        ) : (
          <span className="w-3 shrink-0 text-[10px] text-line text-center">·</span>
        )}
        <span className="text-[13px] text-ink truncate flex-1">{node.name}</span>
        {typeof node.members_count === 'number' && (
          <span className="font-mono text-[10px] text-dim">{node.members_count} 人</span>
        )}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button onClick={handleAddChild} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">＋子</button>
          <button onClick={handleRename} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">改名</button>
          <button onClick={handleMove} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">移动</button>
          <button onClick={handleDelete} disabled={busy} className="text-[10.5px] font-mono text-red-500 hover:text-red-700 px-1">删除</button>
        </div>
      </div>
      {expanded &&
        node.children.map((c) => (
          <OrgUnitNode
            key={c.id}
            node={c}
            depth={depth + 1}
            units={units}
            tenantId={tenantId}
            onReload={onReload}
            onError={onError}
          />
        ))}
    </div>
  );
}

// ─── Org-unit panel (left) ────────────────────────────────────────────────────

function OrgUnitPanel({
  units,
  tenantId,
  onReload,
  onError,
}: {
  units: OrgUnit[];
  tenantId: string | null;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const tree = useMemo(() => buildTree(units), [units]);
  const [busy, setBusy] = useState(false);
  const prompt = usePrompt();

  const handleAddRoot = async () => {
    const name = await prompt({ title: '新建顶级部门', label: '部门名称', required: true });
    if (!name || !name.trim()) return;
    setBusy(true);
    onError('');
    api.admin.org.orgUnits
      .create({ name: name.trim() }, tenantId ?? undefined)
      .then(() => onReload())
      .catch((err) => onError(err instanceof ApiError ? err.message : '创建失败'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">组织架构</h2>
        <button
          onClick={handleAddRoot}
          disabled={busy}
          className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
        >
          + 顶级部门
        </button>
      </div>
      <div className="border border-line rounded-md p-2 bg-paper min-h-[200px]">
        {tree.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic p-2">暂无部门</div>
        ) : (
          tree.map((n) => (
            <OrgUnitNode
              key={n.id}
              node={n}
              depth={0}
              units={units}
              tenantId={tenantId}
              onReload={onReload}
              onError={onError}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ─── Team members (selected team) ─────────────────────────────────────────────

function TeamMembersPanel({
  team,
  tenantId,
  users,
  onError,
}: {
  team: Team;
  tenantId: string | null;
  users: AdminUser[];
  onError: (m: string) => void;
}) {
  const [members, setMembers] = useState<TeamMember[]>([]);
  const [loading, setLoading] = useState(false);
  const [newUserId, setNewUserId] = useState('');
  const [busy, setBusy] = useState(false);
  const confirm = useConfirm();

  const loadMembers = useCallback(() => {
    setLoading(true);
    api.admin.org.teams.members
      .list(team.id, tenantId ?? undefined)
      .then((data) => setMembers(data))
      .catch((err) => onError(err instanceof ApiError ? err.message : '加载成员失败'))
      .finally(() => setLoading(false));
  }, [team.id, tenantId, onError]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  const handleAdd = async () => {
    const uid = newUserId.trim();
    if (!uid) return;
    setBusy(true);
    onError('');
    try {
      const body: AddTeamMemberRequest = { user_id: uid };
      await api.admin.org.teams.members.add(team.id, body, tenantId ?? undefined);
      setNewUserId('');
      loadMembers();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '添加成员失败');
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (userId: string) => {
    if (!(await confirm({ title: '移除成员', message: '确定移除该成员吗？', tone: 'danger', confirmText: '移除' }))) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.teams.members.remove(team.id, userId, tenantId ?? undefined);
      loadMembers();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '移除成员失败');
    } finally {
      setBusy(false);
    }
  };

  const userEmail = (uid: string) => users.find((u) => u.id === uid)?.email;

  return (
    <div className="border border-line rounded-md bg-surface flex flex-col">
      <div className="px-3 py-2 border-b border-line bg-paper-2">
        <div className="text-[13px] text-ink font-semibold">{team.name} · 成员</div>
      </div>

      {/* Add member */}
      <div className="px-3 py-2 border-b border-line-2 flex items-center gap-2">
        <input
          list="org-users-list"
          className="flex-1 border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono"
          placeholder="输入 user_id 或从用户选择…"
          value={newUserId}
          onChange={(e) => setNewUserId(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              handleAdd();
            }
          }}
        />
        <datalist id="org-users-list">
          {users.map((u) => (
            <option key={u.id} value={u.id}>{u.email}</option>
          ))}
        </datalist>
        <button
          onClick={handleAdd}
          disabled={busy || !newUserId.trim()}
          className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          添加
        </button>
      </div>

      {loading ? (
        <div className="px-3 py-3 font-mono text-[12px] text-dim">加载中…</div>
      ) : members.length === 0 ? (
        <div className="px-3 py-3 font-mono text-[12px] text-dim italic">暂无成员</div>
      ) : (
        <div className="divide-y divide-line-2">
          {members.map((m) => (
            <div key={m.user_id} className="px-3 py-2 flex items-center gap-2">
              <div className="flex-1 min-w-0">
                <div className="font-mono text-[12px] text-ink truncate">
                  {userEmail(m.user_id) ?? m.user_id}
                </div>
                {m.role && <div className="font-mono text-[10px] text-dim">{m.role}</div>}
              </div>
              <button
                onClick={() => handleRemove(m.user_id)}
                disabled={busy}
                className="text-[11px] font-mono text-red-600 border border-red-200 rounded-sm px-2 py-0.5 hover:bg-red-50 disabled:opacity-50"
              >
                移除
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Teams panel (right) ──────────────────────────────────────────────────────

function TeamsPanel({
  teams,
  tenantId,
  users,
  selectedTeamId,
  onSelectTeam,
  onReload,
  onError,
}: {
  teams: Team[];
  tenantId: string | null;
  users: AdminUser[];
  selectedTeamId: string | null;
  onSelectTeam: (id: string | null) => void;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);

  const selectedTeam = teams.find((t) => t.id === selectedTeamId) ?? null;

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    onError('');
    try {
      const body: CreateTeamRequest = { name: name.trim() };
      await api.admin.org.teams.create(body, tenantId ?? undefined);
      setName('');
      setCreating(false);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '创建团队失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">团队</h2>
        {!creating && (
          <button
            onClick={() => setCreating(true)}
            className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
          >
            + 新建团队
          </button>
        )}
      </div>

      {creating && (
        <form onSubmit={handleCreate} className="flex items-center gap-2">
          <input
            className="flex-1 border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            placeholder="团队名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
          <button
            type="submit"
            disabled={busy || !name.trim()}
            className="px-2.5 py-1.5 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
          >
            创建
          </button>
          <button
            type="button"
            onClick={() => { setCreating(false); setName(''); }}
            className="px-2.5 py-1.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
          >
            取消
          </button>
        </form>
      )}

      {/* Team list */}
      <div className="border border-line rounded-md bg-paper overflow-hidden">
        {teams.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic p-3">暂无团队</div>
        ) : (
          teams.map((t) => (
            <button
              key={t.id}
              onClick={() => onSelectTeam(selectedTeamId === t.id ? null : t.id)}
              className={`w-full text-left px-3 py-2 border-b border-line-2 last:border-0 flex items-center gap-2 transition-colors ${
                selectedTeamId === t.id ? 'bg-accent-soft' : 'hover:bg-paper-2'
              }`}
            >
              <span className="text-[13px] text-ink flex-1 truncate">{t.name}</span>
              {typeof t.members_count === 'number' && (
                <span className="font-mono text-[10px] text-dim">{t.members_count} 人</span>
              )}
            </button>
          ))
        )}
      </div>

      {/* Selected team members */}
      {selectedTeam && (
        <TeamMembersPanel team={selectedTeam} tenantId={tenantId} users={users} onError={onError} />
      )}
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function OrgStructurePage() {
  const { tenants, tenantsLoading, setTenants, setTenantsLoading, selectedTenantId, setSelectedTenantId } =
    useAdminStore();

  const [units, setUnits] = useState<OrgUnit[]>([]);
  const [teams, setTeams] = useState<Team[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selectedTeamId, setSelectedTeamId] = useState<string | null>(null);
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
      const [unitList, teamList] = await Promise.all([
        api.admin.org.orgUnits.list(tid),
        api.admin.org.teams.list(tid),
      ]);
      setUnits(unitList);
      setTeams(teamList);
      // Users power the member picker — best-effort, ignore failures.
      api.admin.users
        .list(tid)
        .then((data) => setUsers(data.items))
        .catch(() => {/* ignore */});
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

  useEffect(() => {
    setSelectedTeamId(null);
    load();
  }, [load]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">组织架构</h1>
        <p className="text-[12px] text-dim mt-0.5">管理部门层级与团队成员</p>
      </div>

      {/* Tenant selector */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">选择租户</label>
        <select
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent min-w-[200px]"
          value={selectedTenantId ?? ''}
          onChange={(e) => setSelectedTenantId(e.target.value || null)}
          disabled={tenantsLoading}
        >
          <option value="">（默认 / 当前租户）</option>
          {tenants.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          该能力服务未启用（组织架构代理端点尚未就绪）。接线后将自动展示。
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">加载中…</div>
      ) : (
        !isTodoEndpoint && (
          <div className="flex gap-6 items-start">
            <OrgUnitPanel units={units} tenantId={selectedTenantId} onReload={load} onError={(m) => setError(m || null)} />
            <TeamsPanel
              teams={teams}
              tenantId={selectedTenantId}
              users={users}
              selectedTeamId={selectedTeamId}
              onSelectTeam={setSelectedTeamId}
              onReload={load}
              onError={(m) => setError(m || null)}
            />
          </div>
        )
      )}
    </div>
  );
}
