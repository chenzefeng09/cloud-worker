import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { CustomSelect } from '../components/CustomSelect';
import { Dialog } from '../components/Dialog';
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

// foundation refuses to delete a non-empty org unit (NOT_EMPTY) and reports the
// precise cause via `reason`. The delete endpoint archives first, so the only
// reasons that reach the user are genuine occupants; map each to a concrete
// hint, falling back to the localised `user_message` carried on the error.
const ORG_DELETE_REASON_HINT: Record<string, string> = {
  has_children: '该部门下仍有子部门，请先删除子部门。',
  has_teams: '该部门下仍有团队，请先移除或删除团队。',
  has_members: '该部门下仍有成员，请先把成员移出该部门。',
};

function deleteErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.reason && ORG_DELETE_REASON_HINT[err.reason]) {
      return ORG_DELETE_REASON_HINT[err.reason];
    }
    return err.message;
  }
  return '操作失败';
}

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

/** Collect a node and all its descendant IDs (to exclude from move targets). */
function collectDescendantIds(nodeId: string, units: OrgUnit[]): Set<string> {
  const ids = new Set<string>([nodeId]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const u of units) {
      if (u.parent_id && ids.has(u.parent_id) && !ids.has(u.id)) {
        ids.add(u.id);
        changed = true;
      }
    }
  }
  return ids;
}

// ─── Move org-unit dialog (tree picker) ─────────────────────────────────────

function MoveTreeNode({
  node,
  depth,
  excludeIds,
  selectedId,
  currentParentId,
  onSelect,
}: {
  node: OrgNode;
  depth: number;
  excludeIds: Set<string>;
  selectedId: string | undefined;
  currentParentId: string | undefined;
  onSelect: (id: string) => void;
}) {
  const disabled = excludeIds.has(node.id);
  const selected = selectedId === node.id;
  const isCurrent = currentParentId === node.id;
  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onSelect(node.id)}
        className={`w-full text-left py-1.5 px-2 rounded-sm flex items-center gap-1.5 transition-colors ${
          disabled
            ? 'opacity-40 cursor-not-allowed'
            : selected
              ? 'bg-accent-soft'
              : 'hover:bg-paper-2'
        }`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        <span className="text-[13px] text-ink truncate">{node.name}</span>
        {isCurrent && <span className="text-[10px] font-mono text-dim ml-1">（当前）</span>}
      </button>
      {node.children.map((c) => (
        <MoveTreeNode
          key={c.id}
          node={c}
          depth={depth + 1}
          excludeIds={excludeIds}
          selectedId={selectedId}
          currentParentId={currentParentId}
          onSelect={onSelect}
        />
      ))}
    </>
  );
}

function MoveOrgUnitDialog({
  node,
  units,
  tenantId,
  onDone,
  onCancel,
  onError,
}: {
  node: OrgNode;
  units: OrgUnit[];
  tenantId: string | null;
  onDone: () => void;
  onCancel: () => void;
  onError: (m: string) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | undefined>(node.parent_id ?? undefined);
  const [busy, setBusy] = useState(false);
  const tree = useMemo(() => buildTree(units), [units]);
  const excludeIds = useMemo(() => collectDescendantIds(node.id, units), [node.id, units]);

  const isRoot = selectedId === undefined;
  const unchanged = (selectedId ?? undefined) === (node.parent_id ?? undefined);

  const handleConfirm = async () => {
    if (unchanged) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.orgUnits.move(node.id, { new_parent_id: selectedId ?? null }, tenantId ?? undefined);
      onDone();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '移动失败');
      onCancel();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={`移动「${node.name}」`} onClose={onCancel} className="w-full max-w-sm">
      <div className="text-[12px] text-dim mb-1">选择目标父部门：</div>
      <div className="border border-line rounded-md bg-paper max-h-[320px] overflow-y-auto p-1">
        {/* Move to root option */}
        <button
          type="button"
          onClick={() => setSelectedId(undefined)}
          className={`w-full text-left py-1.5 px-2 rounded-sm flex items-center gap-1.5 transition-colors ${
            isRoot ? 'bg-accent-soft' : 'hover:bg-paper-2'
          }`}
        >
          <span className="text-[13px] text-ink">（根级 — 顶级部门）</span>
          {!node.parent_id && <span className="text-[10px] font-mono text-dim ml-1">（当前）</span>}
        </button>
        <div className="border-b border-line-2 my-1" />
        {tree.map((n) => (
          <MoveTreeNode
            key={n.id}
            node={n}
            depth={0}
            excludeIds={excludeIds}
            selectedId={selectedId}
            currentParentId={node.parent_id ?? undefined}
            onSelect={setSelectedId}
          />
        ))}
      </div>
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 text-[13px] rounded-sm border border-line text-dim hover:bg-paper-2"
        >
          取消
        </button>
        <button
          type="button"
          disabled={unchanged || busy}
          onClick={handleConfirm}
          className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
        >
          {busy ? '移动中…' : '确定'}
        </button>
      </div>
    </Dialog>
  );
}

function CreateOrgUnitForm({
  title,
  busy,
  onSubmit,
  onCancel,
}: {
  title: string;
  busy: boolean;
  onSubmit: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const n = name.trim();
    if (!n) { setErr('请填写部门名称'); return; }
    setErr(null);
    onSubmit(n);
  };

  return (
    <form onSubmit={submit} className="border border-line rounded-md p-3 bg-surface flex flex-col gap-2">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">{title}</div>
      {err && <div className="text-[11px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
      <div className="flex flex-col gap-1">
        <label className="text-[11px] text-dim font-mono">部门名称</label>
        <input
          autoFocus
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
          value={name}
          onChange={(e) => { setName(e.target.value); if (err) setErr(null); }}
          placeholder="研发部"
        />
      </div>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          {busy ? '创建中…' : '创建'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
        >
          取消
        </button>
      </div>
    </form>
  );
}

// ─── Org-unit tree node ───────────────────────────────────────────────────────

function OrgUnitNode({
  node,
  depth,
  units,
  tenantId,
  selectedOuId,
  onSelectOu,
  onReload,
  onError,
}: {
  node: OrgNode;
  depth: number;
  units: OrgUnit[];
  tenantId: string | null;
  selectedOuId: string | null;
  onSelectOu: (id: string | null) => void;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [busy, setBusy] = useState(false);
  const [addingChild, setAddingChild] = useState(false);
  const [showMoveDialog, setShowMoveDialog] = useState(false);
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

  const handleAddChild = (name: string) => {
    const body: CreateOrgUnitRequest = { name, parent_id: node.id };
    wrap(() =>
      api.admin.org.orgUnits
        .create(body, tenantId ?? undefined)
        .then(() => { setAddingChild(false); }),
    );
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

  const handleMove = () => setShowMoveDialog(true);

  const handleDelete = async () => {
    if (node.children.length > 0) {
      onError('该部门下仍有子部门，请先删除子部门。');
      return;
    }
    const input = await prompt({
      title: '删除部门',
      description: `此操作会先归档再删除。请输入部门名称「${node.name}」以确认删除。`,
      label: '部门名称',
      placeholder: node.name,
      confirmText: '删除',
      required: true,
      validate: (value) =>
        value.trim() === node.name ? null : '输入的部门名称不一致，请重新输入。',
    });
    if (input === null) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.orgUnits.delete(node.id, tenantId ?? undefined);
      onReload();
    } catch (err) {
      onError(deleteErrorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <div
        className={`group flex items-center gap-1.5 py-1.5 px-2 rounded-sm cursor-pointer transition-colors ${
          selectedOuId === node.id ? 'bg-accent-soft' : 'hover:bg-paper-2'
        }`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={() => onSelectOu(selectedOuId === node.id ? null : node.id)}
      >
        {node.children.length > 0 ? (
          <button
            onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
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
          <button onClick={() => setAddingChild((v) => !v)} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">＋子</button>
          <button onClick={handleRename} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">改名</button>
          <button onClick={handleMove} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">移动</button>
          <button onClick={handleDelete} disabled={busy} className="text-[10.5px] font-mono text-red-500 hover:text-red-700 px-1">删除</button>
        </div>
      </div>
      {showMoveDialog && (
        <MoveOrgUnitDialog
          node={node}
          units={units}
          tenantId={tenantId}
          onDone={() => { setShowMoveDialog(false); onReload(); }}
          onCancel={() => setShowMoveDialog(false)}
          onError={onError}
        />
      )}
      {addingChild && (
        <div style={{ paddingLeft: `${(depth + 1) * 16 + 8}px` }} className="py-1.5 pr-2">
          <CreateOrgUnitForm
            title={`在「${node.name}」下新建子部门`}
            busy={busy}
            onSubmit={handleAddChild}
            onCancel={() => setAddingChild(false)}
          />
        </div>
      )}
      {expanded &&
        node.children.map((c) => (
          <OrgUnitNode
            key={c.id}
            node={c}
            depth={depth + 1}
            units={units}
            tenantId={tenantId}
            selectedOuId={selectedOuId}
            onSelectOu={onSelectOu}
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
  selectedOuId,
  onSelectOu,
  onReload,
  onError,
}: {
  units: OrgUnit[];
  tenantId: string | null;
  selectedOuId: string | null;
  onSelectOu: (id: string | null) => void;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const tree = useMemo(() => buildTree(units), [units]);
  const [busy, setBusy] = useState(false);
  const [addingRoot, setAddingRoot] = useState(false);

  const handleAddRoot = (name: string) => {
    setBusy(true);
    onError('');
    api.admin.org.orgUnits
      .create({ name }, tenantId ?? undefined)
      .then(() => { setAddingRoot(false); onReload(); })
      .catch((err) => onError(err instanceof ApiError ? err.message : '创建失败'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">组织架构</h2>
        <button
          onClick={() => setAddingRoot((v) => !v)}
          disabled={busy}
          className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
        >
          + 顶级部门
        </button>
      </div>

      {addingRoot && (
        <CreateOrgUnitForm
          title="新建顶级部门"
          busy={busy}
          onSubmit={handleAddRoot}
          onCancel={() => setAddingRoot(false)}
        />
      )}
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
              selectedOuId={selectedOuId}
              onSelectOu={onSelectOu}
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
  const [busy, setBusy] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  const [search, setSearch] = useState('');
  const confirm = useConfirm();

  const loadMembers = useCallback(() => {
    setLoading(true);
    api.admin.org.teams.members
      .list(team.id, tenantId ?? undefined)
      .then((data) => setMembers(Array.isArray(data) ? data : []))
      .catch((err) => onError(err instanceof ApiError ? err.message : '加载成员失败'))
      .finally(() => setLoading(false));
  }, [team.id, tenantId, onError]);

  useEffect(() => {
    loadMembers();
  }, [loadMembers]);

  const handleAdd = async (userId: string) => {
    setBusy(true);
    onError('');
    try {
      const body: AddTeamMemberRequest = { user_id: userId };
      await api.admin.org.teams.members.add(team.id, body, tenantId ?? undefined);
      setShowPicker(false);
      setSearch('');
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

  const userLabel = (uid: string) => {
    const u = users.find((u) => u.id === uid);
    return u ? (u.display_name || u.email) : uid;
  };

  const availableUsers = users.filter((u) => !members.some((m) => m.user_id === u.id));
  const filteredUsers = search.trim()
    ? availableUsers.filter((u) => {
        const q = search.toLowerCase();
        return u.email.toLowerCase().includes(q) || (u.display_name?.toLowerCase().includes(q));
      })
    : availableUsers;

  return (
    <div className="border border-line rounded-md bg-surface flex flex-col">
      <div className="px-3 py-2 border-b border-line bg-paper-2 flex items-center justify-between">
        <div className="text-[13px] text-ink font-semibold">{team.name} · 成员</div>
        <button
          onClick={() => { setShowPicker(true); setSearch(''); }}
          disabled={busy}
          className="px-2 py-0.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper disabled:opacity-50"
        >
          + 添加成员
        </button>
      </div>

      {showPicker && (
        <Dialog title="添加成员" onClose={() => { setShowPicker(false); setSearch(''); }} dismissable={!busy}>
          <input
            autoFocus
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent w-full"
            placeholder="搜索邮箱或姓名…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="max-h-[300px] overflow-auto -mx-1">
            {filteredUsers.length === 0 ? (
              <div className="px-3 py-3 text-[12px] text-dim italic">无匹配用户</div>
            ) : (
              filteredUsers.map((u) => (
                <button
                  key={u.id}
                  onClick={() => handleAdd(u.id)}
                  disabled={busy}
                  className="w-full text-left px-3 py-2 rounded-sm hover:bg-paper-2 transition-colors flex flex-col gap-0.5 disabled:opacity-50"
                >
                  <span className="text-[13px] text-ink">{u.display_name || u.email}</span>
                  {u.display_name && <span className="text-[11px] text-dim">{u.email}</span>}
                </button>
              ))
            )}
          </div>
        </Dialog>
      )}

      {loading ? (
        <div className="px-3 py-3 font-mono text-[12px] text-dim">加载中…</div>
      ) : members.length === 0 ? (
        <div className="px-3 py-3 font-mono text-[12px] text-dim italic">暂无成员</div>
      ) : (
        <div className="divide-y divide-line-2">
          {members.map((m) => (
            <div key={m.user_id} className="px-3 py-2 flex items-center gap-2">
              <div className="flex-1 min-w-0">
                <div className="text-[12px] text-ink truncate">{userLabel(m.user_id)}</div>
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
  selectedOuId,
  selectedOuName,
  selectedTeamId,
  onSelectTeam,
  onReload,
  onError,
}: {
  teams: Team[];
  tenantId: string | null;
  users: AdminUser[];
  selectedOuId: string | null;
  selectedOuName: string | null;
  selectedTeamId: string | null;
  onSelectTeam: (id: string | null) => void;
  onReload: () => void;
  onError: (m: string) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const prompt = usePrompt();

  const filteredTeams = selectedOuId ? teams.filter((t) => t.org_unit_id === selectedOuId) : teams;
  const selectedTeam = filteredTeams.find((t) => t.id === selectedTeamId) ?? null;

  const autoCode = (n: string): string => {
    const slug = n.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    const base = slug || `t-${Date.now().toString(36)}`;
    return base.slice(0, 32);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim() || !selectedOuId) return;
    setBusy(true);
    onError('');
    try {
      let code = autoCode(name);
      if (!/^[a-z]/.test(code)) code = `t-${code}`;
      const body: CreateTeamRequest = { name: name.trim(), code, org_unit_id: selectedOuId };
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

  const handleDelete = async (team: Team) => {
    const input = await prompt({
      title: '删除团队',
      description: `此操作不可撤销。请输入团队名称「${team.name}」以确认删除。`,
      label: '团队名称',
      placeholder: team.name,
      confirmText: '删除',
      required: true,
      validate: (value) =>
        value.trim() === team.name ? null : '输入的团队名称不一致，请重新输入。',
    });
    if (input === null) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.teams.delete(team.id, tenantId ?? undefined);
      if (selectedTeamId === team.id) onSelectTeam(null);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : '删除团队失败');
    } finally {
      setBusy(false);
    }
  };

  if (!selectedOuId) {
    return (
      <div className="flex flex-col gap-3 flex-1 min-w-0">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">团队</h2>
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          请先在左侧选择一个部门，查看该部门牵头的团队
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">
          团队
          {selectedOuName && <span className="text-dim ml-1.5 normal-case tracking-normal">— {selectedOuName}</span>}
        </h2>
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
            autoFocus
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
            {busy ? '创建中…' : '创建'}
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
        {filteredTeams.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic p-3">暂无团队</div>
        ) : (
          filteredTeams.map((t) => (
            <div
              key={t.id}
              onClick={() => onSelectTeam(selectedTeamId === t.id ? null : t.id)}
              className={`group w-full text-left px-3 py-2 border-b border-line-2 last:border-0 flex items-center gap-2 cursor-pointer transition-colors ${
                selectedTeamId === t.id ? 'bg-accent-soft' : 'hover:bg-paper-2'
              }`}
            >
              <span className="text-[13px] text-ink flex-1 truncate">{t.name}</span>
              {typeof t.members_count === 'number' && (
                <span className="font-mono text-[10px] text-dim">{t.members_count} 人</span>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(t); }}
                disabled={busy}
                className="text-[10.5px] font-mono text-red-500 hover:text-red-700 px-1 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                删除
              </button>
            </div>
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
  const [selectedOuId, setSelectedOuId] = useState<string | null>(null);
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
        <CustomSelect
          className="min-w-[200px]"
          options={[
            { value: '', label: '（默认 / 当前租户）' },
            ...tenants.map((t) => ({ value: t.id, label: t.name })),
          ]}
          value={selectedTenantId ?? ''}
          onChange={(v) => setSelectedTenantId(v || null)}
          disabled={tenantsLoading}
        />
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
            <OrgUnitPanel
              units={units}
              tenantId={selectedTenantId}
              selectedOuId={selectedOuId}
              onSelectOu={setSelectedOuId}
              onReload={load}
              onError={(m) => setError(m || null)}
            />
            <TeamsPanel
              teams={teams}
              tenantId={selectedTenantId}
              users={users}
              selectedOuId={selectedOuId}
              selectedOuName={units.find((u) => u.id === selectedOuId)?.name ?? null}
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
