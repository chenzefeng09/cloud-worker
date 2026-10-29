import { useEffect, useState } from 'react';
import { api, ApiError } from '../../api/client';
import { useConfirm, useToast } from '../components/dialogs';
import { formatDateAuto, formatDateTimeAuto } from '../../i18n/dates';
import { Dialog } from '../components/Dialog';
import type { AdminSkill, AdminSkillVersion } from '../../api/types';

// ─── Skill detail / versions panel ───────────────────────────────────────────

function SkillDetailPanel({
  skill,
  onClose,
  onDeleted,
}: {
  skill: AdminSkill;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const confirm = useConfirm();
  const toast = useToast();
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async () => {
    if (
      !(await confirm({
        title: 'Delete skill',
        message: `Delete skill "${skill.name}"? This cannot be undone.`,
        tone: 'danger',
        confirmText: 'Delete',
      }))
    )
      return;
    setDeleting(true);
    try {
      await api.admin.skills.delete(skill.id);
      toast.success('Skill deleted');
      onDeleted();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : 'Delete failed';
      toast.error(msg);
    } finally {
      setDeleting(false);
    }
  };

  const [detail, setDetail] = useState<AdminSkill | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [versions, setVersions] = useState<AdminSkillVersion[] | null>(null);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [versionsError, setVersionsError] = useState<string | null>(null);
  const [versionsTodo, setVersionsTodo] = useState(false);

  // 标识符用 skill 的 id 字段而非 name
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.admin.skills
      .get(skill.id)
      .then((data) => { if (!cancelled) setDetail(data); })
      .catch((err) => { if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [skill.id]);

  const loadVersions = () => {
    setVersionsLoading(true);
    setVersionsError(null);
    setVersionsTodo(false);
    api.admin.skills
      .versions(skill.id)
      .then((data) => setVersions(data))
      .catch((err) => {
        if (err instanceof ApiError && (err.status === 404 || err.status === 501)) {
          setVersionsTodo(true);
        } else {
          setVersionsError(err instanceof ApiError ? err.message : 'Failed to load');
        }
      })
      .finally(() => setVersionsLoading(false));
  };

  const d = detail ?? skill;

  return (
    <Dialog title="Skill details" onClose={onClose} className="w-full max-w-2xl">
      {loading && <div className="font-mono text-[12px] text-dim">Loading…</div>}
      {error && (
        <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{error}</div>
      )}

      <div className="grid grid-cols-[120px_1fr] gap-y-2 gap-x-3 text-[12px]">
        <span className="font-mono text-dim">ID</span>
        <span className="font-mono text-ink-2 break-all">{d.id}</span>
        <span className="font-mono text-dim">Name</span>
        <span className="text-ink">{d.name}</span>
        <span className="font-mono text-dim">Description</span>
        <span className="text-ink-2">{d.description || '—'}</span>
        <span className="font-mono text-dim">Owner</span>
        <span className="text-ink-2">{d.owner || '—'}</span>
        <span className="font-mono text-dim">Visibility</span>
        <span className="font-mono text-ink-2">{d.visibility || '—'}</span>
        <span className="font-mono text-dim">Created</span>
        <span className="font-mono text-ink-2">
          {d.created_at ? formatDateTimeAuto(new Date(d.created_at)) : '—'}
        </span>
      </div>

      {/* 版本 */}
      <div className="border-t border-line-2 pt-3 flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <button
            onClick={loadVersions}
            disabled={versionsLoading}
            className="px-2.5 py-1 border border-line text-[11px] font-mono rounded-sm text-ink-2 hover:text-ink disabled:opacity-50"
          >
            {versionsLoading ? 'Loading versions…' : 'View versions'}
          </button>
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="px-2.5 py-1 border border-red-200 text-[11px] font-mono rounded-sm text-red-600 hover:bg-red-50 disabled:opacity-50"
          >
            {deleting ? 'Deleting…' : 'Delete'}
          </button>
        </div>

        {versionsTodo && (
          <div className="border border-line rounded-md p-3 text-[12px] text-dim italic">
            Couldn't load this skill's versions.
          </div>
        )}
        {versionsError && (
          <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">{versionsError}</div>
        )}
        {versions && versions.length === 0 && !versionsTodo && (
          <div className="font-mono text-[12px] text-dim italic">No versions</div>
        )}
        {versions && versions.length > 0 && (
          <div className="border border-line rounded-md overflow-hidden max-h-[40vh] overflow-y-auto">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Version</th>
                  <th className="px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">SHA256</th>
                  <th className="px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Uploaded by</th>
                  <th className="px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Uploaded</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} className="border-b border-line-2 hover:bg-paper-2">
                    <td className="px-3 py-1.5 font-mono text-[12px] text-ink">{v.version}</td>
                    <td className="px-3 py-1.5 font-mono text-[11px] text-dim truncate max-w-[160px]">
                      {v.sha256 || '—'}
                    </td>
                    <td className="px-3 py-1.5 text-[12px] text-ink-2">{v.uploaded_by || '—'}</td>
                    <td className="px-3 py-1.5 font-mono text-[11px] text-dim">
                      {v.uploaded_at ? formatDateTimeAuto(new Date(v.uploaded_at)) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Dialog>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function SkillsAdminPage() {
  const [skills, setSkills] = useState<AdminSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isTodoEndpoint, setIsTodoEndpoint] = useState(false);
  const [selected, setSelected] = useState<AdminSkill | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setIsTodoEndpoint(false);
    api.admin.skills
      .list()
      .then((data) => { if (!cancelled) setSkills(data); })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && (err.status === 404 || err.status === 501 || err.status === 502)) {
          setIsTodoEndpoint(true);
        } else {
          setError(err instanceof ApiError ? err.message : 'Failed to load');
        }
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [reloadKey]);

  return (
    <div className="flex flex-col gap-5 p-6 overflow-auto h-full">
      <div>
        <h1 className="text-[18px] font-display text-ink tracking-[-0.02em]">Skills</h1>
        <p className="text-[12px] text-dim mt-0.5">Platform skill library governance (→ skill-store)</p>
      </div>

      {error && (
        <div className="border border-accent-line rounded-md px-4 py-3 bg-accent-soft text-[13px] text-accent">
          {error}
        </div>
      )}

      {isTodoEndpoint && (
        <div className="border border-line rounded-md p-4 text-[12px] text-dim italic">
          Not available yet: the skill-store proxy endpoint isn't ready. Skills will appear here once it's connected.
        </div>
      )}

      {loading ? (
        <div className="font-mono text-[12px] text-dim">Loading…</div>
      ) : !isTodoEndpoint && skills.length === 0 ? (
        <div className="font-mono text-[12px] text-dim italic">No skills</div>
      ) : (
        !isTodoEndpoint && (
          <div className="border border-line rounded-md overflow-hidden">
            <table className="w-full text-left border-collapse">
              <thead className="bg-paper-2 border-b border-line">
                <tr>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Name</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Visibility</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Description</th>
                  <th className="px-3 py-2 font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">Created</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((s) => (
                  <tr
                    key={s.id}
                    onClick={() => setSelected(s)}
                    className={`border-b border-line-2 hover:bg-paper-2 transition-colors cursor-pointer ${
                      selected?.id === s.id ? 'bg-paper-2' : ''
                    }`}
                  >
                    <td className="px-3 py-2 text-[13px] text-ink">{s.name}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-ink-2">{s.visibility || '—'}</td>
                    <td className="px-3 py-2 text-[12px] text-dim truncate max-w-[280px]">
                      {s.description || '—'}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-dim">
                      {s.created_at ? formatDateAuto(new Date(s.created_at)) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}

      {selected && (
        <SkillDetailPanel
          skill={selected}
          onClose={() => setSelected(null)}
          onDeleted={() => { setSelected(null); setReloadKey((k) => k + 1); }}
        />
      )}
    </div>
  );
}
