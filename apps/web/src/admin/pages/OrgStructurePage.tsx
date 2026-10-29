import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';
import { isPlatformAdmin } from '../../lib/permissions';
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
  has_children: 'This department still has sub-departments. Delete them first.',
  has_teams: 'This department still has teams. Move or delete them first.',
  has_members: 'This department still has members. Move them out first.',
};

function deleteErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.reason && ORG_DELETE_REASON_HINT[err.reason]) {
      return ORG_DELETE_REASON_HINT[err.reason];
    }
    return err.message;
  }
  return 'Operation failed';
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
        {isCurrent && <span className="text-[10px] font-mono text-dim ml-1"> (current)</span>}
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
      onError(err instanceof ApiError ? err.message : 'Move failed');
      onCancel();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog title={`Move “${node.name}”`} onClose={onCancel} className="w-full max-w-sm">
      <div className="text-[12px] text-dim mb-1">Choose the new parent department:</div>
      <div className="border border-line rounded-md bg-paper max-h-[320px] overflow-y-auto p-1">
        {/* Move to root option */}
        <button
          type="button"
          onClick={() => setSelectedId(undefined)}
          className={`w-full text-left py-1.5 px-2 rounded-sm flex items-center gap-1.5 transition-colors ${
            isRoot ? 'bg-accent-soft' : 'hover:bg-paper-2'
          }`}
        >
          <span className="text-[13px] text-ink">(Root — top-level department)</span>
          {!node.parent_id && <span className="text-[10px] font-mono text-dim ml-1"> (current)</span>}
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
          Cancel
        </button>
        <button
          type="button"
          disabled={unchanged || busy}
          onClick={handleConfirm}
          className="px-3 py-1.5 text-[13px] rounded-sm bg-ink text-paper hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Moving…' : 'Confirm'}
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
    if (!n) { setErr('Enter a department name'); return; }
    setErr(null);
    onSubmit(n);
  };

  return (
    <form onSubmit={submit} className="border border-line rounded-md p-3 bg-surface flex flex-col gap-2">
      <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">{title}</div>
      {err && <div className="text-[11px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{err}</div>}
      <div className="flex flex-col gap-1">
        <label className="text-[11px] text-dim font-mono">Department name</label>
        <input
          autoFocus
          className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
          value={name}
          onChange={(e) => { setName(e.target.value); if (err) setErr(null); }}
          placeholder="Engineering"
        />
      </div>
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy}
          className="px-2.5 py-1 text-[11px] font-mono bg-accent text-surface rounded-sm hover:opacity-90 disabled:opacity-50"
        >
          {busy ? 'Creating…' : 'Create'}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="px-2.5 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink"
        >
          Cancel
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
      onError(err instanceof ApiError ? err.message : 'Operation failed');
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
      title: 'Rename department',
      label: 'New name',
      defaultValue: node.name,
      required: true,
    });
    if (!name || !name.trim() || name.trim() === node.name) return;
    wrap(() => api.admin.org.orgUnits.patch(node.id, { name: name.trim() }, tenantId ?? undefined).then(() => undefined));
  };

  const handleMove = () => setShowMoveDialog(true);

  const handleDelete = async () => {
    if (node.children.length > 0) {
      onError('This department still has sub-departments. Delete them first.');
      return;
    }
    const input = await prompt({
      title: 'Delete department',
      description: `The department will be archived and then deleted. Type the department name “${node.name}” to confirm.`,
      label: 'Department name',
      placeholder: node.name,
      confirmText: 'Delete',
      required: true,
      validate: (value) =>
        value.trim() === node.name ? null : 'The department name doesn’t match. Try again.',
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
          <span className="font-mono text-[10px] text-dim">{node.members_count} members</span>
        )}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <button onClick={() => setAddingChild((v) => !v)} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">+ Sub</button>
          <button onClick={handleRename} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">Rename</button>
          <button onClick={handleMove} disabled={busy} className="text-[10.5px] font-mono text-dim hover:text-ink px-1">Move</button>
          <button onClick={handleDelete} disabled={busy} className="text-[10.5px] font-mono text-red-500 hover:text-red-700 px-1">Delete</button>
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
            title={`New sub-department under “${node.name}”`}
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
      .catch((err) => onError(err instanceof ApiError ? err.message : 'Create failed'))
      .finally(() => setBusy(false));
  };

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">Organization</h2>
        <button
          onClick={() => setAddingRoot((v) => !v)}
          disabled={busy}
          className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2 disabled:opacity-50"
        >
          + Top-level department
        </button>
      </div>

      {addingRoot && (
        <CreateOrgUnitForm
          title="New top-level department"
          busy={busy}
          onSubmit={handleAddRoot}
          onCancel={() => setAddingRoot(false)}
        />
      )}
      <div className="border border-line rounded-md p-2 bg-paper min-h-[200px]">
        {tree.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic p-2">No departments yet</div>
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
      .catch((err) => onError(err instanceof ApiError ? err.message : 'Failed to load members'))
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
      onError(err instanceof ApiError ? err.message : 'Failed to add member');
    } finally {
      setBusy(false);
    }
  };

  const handleRemove = async (userId: string) => {
    if (!(await confirm({ title: 'Remove member', message: 'Remove this member?', tone: 'danger', confirmText: 'Remove' }))) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.teams.members.remove(team.id, userId, tenantId ?? undefined);
      loadMembers();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : 'Failed to remove member');
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
        <div className="text-[13px] text-ink font-semibold">{team.name} · Members</div>
        <button
          onClick={() => { setShowPicker(true); setSearch(''); }}
          disabled={busy}
          className="px-2 py-0.5 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper disabled:opacity-50"
        >
          + Add member
        </button>
      </div>

      {showPicker && (
        <Dialog title="Add member" onClose={() => { setShowPicker(false); setSearch(''); }} dismissable={!busy}>
          <input
            autoFocus
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent w-full"
            placeholder="Search by email or name…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <div className="max-h-[300px] overflow-auto -mx-1">
            {filteredUsers.length === 0 ? (
              <div className="px-3 py-3 text-[12px] text-dim italic">No matching users</div>
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
        <div className="px-3 py-3 font-mono text-[12px] text-dim">Loading…</div>
      ) : members.length === 0 ? (
        <div className="px-3 py-3 font-mono text-[12px] text-dim italic">No members yet</div>
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
                Remove
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
      onError(err instanceof ApiError ? err.message : 'Failed to create team');
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (team: Team) => {
    const input = await prompt({
      title: 'Delete team',
      description: `This cannot be undone. Type the team name “${team.name}” to confirm.`,
      label: 'Team name',
      placeholder: team.name,
      confirmText: 'Delete',
      required: true,
      validate: (value) =>
        value.trim() === team.name ? null : 'The team name doesn’t match. Try again.',
    });
    if (input === null) return;
    setBusy(true);
    onError('');
    try {
      await api.admin.org.teams.delete(team.id, tenantId ?? undefined);
      if (selectedTeamId === team.id) onSelectTeam(null);
      onReload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : 'Failed to delete team');
    } finally {
      setBusy(false);
    }
  };

  if (!selectedOuId) {
    return (
      <div className="flex flex-col gap-3 flex-1 min-w-0">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">Teams</h2>
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Select a department on the left to see the teams it leads
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3 flex-1 min-w-0">
      <div className="flex items-center justify-between">
        <h2 className="text-[13px] font-mono text-ink-2 uppercase tracking-[0.08em]">
          Teams
          {selectedOuName && <span className="text-dim ml-1.5 normal-case tracking-normal">— {selectedOuName}</span>}
        </h2>
        <button
          onClick={() => setCreating(true)}
          className="px-2 py-1 text-[11px] font-mono border border-line rounded-sm text-ink-2 hover:bg-paper-2"
        >
          + New team
        </button>
      </div>

      {creating && (
        <Dialog
          title="New team"
          onClose={() => { setCreating(false); setName(''); }}
          dismissable={!busy}
          className="w-full max-w-[440px]"
        >
          <form onSubmit={handleCreate} className="flex flex-col gap-3">
            <input
              autoFocus
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              placeholder="Team name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
            />
            <div className="flex items-center justify-end gap-2 pt-1">
              <button
                type="button"
                onClick={() => { setCreating(false); setName(''); }}
                disabled={busy}
                className="px-3 py-1.5 text-[12px] font-mono border border-line rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={busy || !name.trim()}
                className="px-4 py-1.5 bg-accent text-surface text-[12px] font-mono rounded-sm hover:opacity-90 disabled:opacity-50"
              >
                {busy ? 'Creating…' : 'Create'}
              </button>
            </div>
          </form>
        </Dialog>
      )}

      {/* Team list */}
      <div className="border border-line rounded-md bg-paper overflow-hidden">
        {filteredTeams.length === 0 ? (
          <div className="font-mono text-[12px] text-dim italic p-3">No teams yet</div>
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
                <span className="font-mono text-[10px] text-dim">{t.members_count} members</span>
              )}
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(t); }}
                disabled={busy}
                className="text-[10.5px] font-mono text-red-500 hover:text-red-700 px-1 opacity-0 group-hover:opacity-100 transition-opacity"
              >
                Delete
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
  const userProfile = useChatStore((s) => s.userProfile);
  // Platform admins may pick any tenant; tenant admins are locked to their own
  // tenant (selectedTenantId stays null → BFF omits ?tenant_id= → own tenant).
  const canSwitchTenant = isPlatformAdmin(userProfile?.permissions);
  const ownTenantName =
    tenants.find((t) => t.id === userProfile?.tenant_id)?.name ?? userProfile?.tenant_id ?? 'Current tenant';

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
    setSelectedTeamId(null);
    load();
  }, [load]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Organization</h1>
        <p className="text-[12px] text-dim mt-0.5">Manage the department hierarchy and team members</p>
      </div>

      {/* Tenant selector — only platform admins may switch; tenant admins are
          locked to their own tenant (shown read-only). */}
      <div className="flex items-center gap-3">
        <label className="text-[11px] font-mono text-dim uppercase tracking-[0.1em]">
          {canSwitchTenant ? 'Tenant' : 'Current tenant'}
        </label>
        {canSwitchTenant ? (
          <CustomSelect
            className="min-w-[200px]"
            options={[
              { value: '', label: '(Default / current tenant)' },
              ...tenants.map((t) => ({ value: t.id, label: t.name })),
            ]}
            value={selectedTenantId ?? ''}
            onChange={(v) => setSelectedTenantId(v || null)}
            disabled={tenantsLoading}
          />
        ) : (
          <span className="min-w-[200px] text-[13px] text-ink font-mono px-3 py-1.5 rounded-sm border border-line bg-surface">
            {ownTenantName}
          </span>
        )}
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          This service isn't enabled yet (the organization proxy endpoint isn't ready). It appears automatically once wired up.
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
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
