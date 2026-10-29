import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
import { api } from '../api/client';
import { ApiError } from '../api/client';
import type {
  McpServer,
  RegisterMcpServerRequest,
  Skill,
  SkillVersion,
} from '../api/types';
import { formatRelativeTime } from '../lib/format';
import { sha256Hex } from '../lib/sha256';
import { useInstalledSkills } from '../hooks/useInstalledSkills';
import { KnowledgeSourceTab } from '../components/KnowledgeSourceTab';
import { McpServerFormDialog } from '../components/McpServerFormDialog';

// Skill tarball upload limits — kept in lockstep with the BFF (20 MiB) and the
// skill-store backend, which also enforces the SKILL.md / gzip checks.
const SKILL_MAX_BYTES = 20 * 1024 * 1024;
const SKILL_ACCEPT = '.tar.gz,.tgz,application/gzip,application/x-gzip';

// Local preflight: reject obviously-bad packages before hitting the network.
// Returns a Chinese error message, or null when the file looks acceptable.
function preflightSkillFile(file: File): string | null {
  const name = file.name.toLowerCase();
  const okExt = name.endsWith('.tar.gz') || name.endsWith('.tgz');
  const okMime =
    file.type === 'application/gzip' || file.type === 'application/x-gzip';
  if (!okExt && !okMime) return 'Please upload a skill package in .tar.gz format';
  if (file.size > SKILL_MAX_BYTES) return 'The skill package exceeds the 20 MB size limit';
  return null;
}

type Tab = 'mcp' | 'skills' | 'docs';

const TAB_LABELS: Record<Tab, string> = {
  mcp: 'MCP tools',
  skills: 'Skills',
  docs: 'Knowledge sources',
};

export function LibraryPage() {
  const [tab, setTab] = useState<Tab>('mcp');

  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  // Knowledge-source docs are owned by the self-contained KnowledgeSourceTab
  // (folder navigation + admin mutations); it reports its document count up so
  // the stat tile / tab badge stay in sync.
  const [docsCount, setDocsCount] = useState<number | null>(null);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [installOpen, setInstallOpen] = useState(false);
  // Skill the user is installing-to-toolbox via the InstallToToolbox modal.
  const [toolboxTarget, setToolboxTarget] = useState<Skill | null>(null);

  // The user's already-installed skills — drives the default/on-demand state
  // shown on each shelf row. refresh() is called after a successful install.
  const installedSkills = useInstalledSkills();
  const installedSkillById = useMemo(
    () => new Map(installedSkills.map((s) => [s.id, s])),
    [installedSkills],
  );

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [mcpRes, skillRes] = await Promise.all([
        api.library.listMcpServers(),
        api.library.listSkills(),
      ]);
      setMcpServers(mcpRes.items);
      setSkills(skillRes.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load the library. Please try again later.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  // Count shown on tiles / tab badges. Docs come from KnowledgeSourceTab and
  // may still be loading (null → "…"); mcp/skills follow the fetchAll loader.
  const countLabel = (t: Tab): string => {
    if (t === 'docs') return docsCount == null ? '…' : String(docsCount);
    if (loading) return '…';
    return String(t === 'mcp' ? mcpServers.length : skills.length);
  };

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: 'Home', to: '/home' }, { label: 'Library' }]}
      />

      <div className="cw-scroll flex-1 px-8 py-5">
        <div className="w-full max-w-[960px] min-w-0 flex flex-col gap-4">

          {/* stat tiles */}
          <div className="grid grid-cols-3 gap-px bg-line border border-line rounded-md overflow-hidden">
            {(['mcp', 'skills', 'docs'] as Tab[]).map((t) => (
              <div
                key={t}
                onClick={() => setTab(t)}
                className={`bg-surface px-4 py-3 cursor-pointer hover:bg-paper-2 transition-colors ${tab === t ? 'border-b-2 border-accent' : ''}`}
              >
                <div className="cw-label mb-1">
                  {TAB_LABELS[t]}
                </div>
                <div className="font-mono text-[24px] font-semibold tracking-[-0.01em]">
                  {countLabel(t)}
                </div>
              </div>
            ))}
          </div>

          {/* error */}
          {error && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {error}
            </div>
          )}

          {/* tab nav */}
          <div className="flex items-center gap-0.5 border-b border-line">
            {(['mcp', 'skills', 'docs'] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 font-mono text-[11px] rounded-t-md border-b-2 -mb-px transition-colors ${
                  tab === t
                    ? 'border-accent text-accent bg-paper'
                    : 'border-transparent text-ink-2 hover:text-ink hover:bg-paper-2'
                }`}
              >
                {TAB_LABELS[t]}
                <span className="ml-1.5 text-[10px] text-dim">
                  {countLabel(t)}
                </span>
              </button>
            ))}
            {/* Docs manage their own toolbar (folder-aware) inside
                KnowledgeSourceTab; the generic "+" only drives mcp/skills. */}
            {tab !== 'docs' && (
              <button
                onClick={() => setInstallOpen(true)}
                title={`Add to ${TAB_LABELS[tab]}`}
                aria-label={`Add to ${TAB_LABELS[tab]}`}
                className="ml-auto shrink-0 px-2.5 py-1.5 font-mono text-[15px] leading-none text-ink-2 hover:text-accent transition-colors"
              >
                +
              </button>
            )}
          </div>

          {/* loading (mcp/skills only; docs tab self-loads) */}
          {loading && tab !== 'docs' && (
            <div className="font-mono text-[11px] text-dim">Loading…</div>
          )}

          {/* MCP servers */}
          {!loading && tab === 'mcp' && (
            <>
              {mcpServers.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">No MCP tools yet</div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {mcpServers.map((s) => (
                    <div
                      key={s.id}
                      className="grid grid-cols-[minmax(0,1fr)_80px_80px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all"
                    >
                      <div className="min-w-0">
                        <div className="text-[13.5px] font-medium">{s.name}</div>
                        <div className="font-mono text-[11px] text-dim truncate">
                          {s.transport === 'http' ? s.endpoint : s.command}
                        </div>
                      </div>
                      <span className="font-mono text-[11px] text-ink-2 uppercase">{s.transport}</span>
                      <Chip accent={s.enabled} dashed={!s.enabled}>
                        {s.enabled ? 'Enabled' : 'Disabled'}
                      </Chip>
                      <span className="font-mono text-[11px] text-dim text-right">
                        {formatRelativeTime(s.updated_at)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* Skills */}
          {!loading && tab === 'skills' && (
            <>
              {skills.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">No skills yet</div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {skills.map((sk) => {
                    const installed = installedSkillById.get(sk.id);
                    return (
                      <div
                        key={sk.id}
                        data-testid={`skill-row-${sk.id}`}
                        className="grid grid-cols-[minmax(0,1fr)_80px_100px_72px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all"
                      >
                        <div className="min-w-0">
                          <div className="text-[13.5px] font-medium">{sk.name}</div>
                          {sk.description && (
                            <div
                              className="font-mono text-[11px] text-dim truncate"
                              title={sk.description}
                            >
                              {sk.description}
                            </div>
                          )}
                        </div>
                        <span className="font-mono text-[11px] text-ink-2 truncate">
                          {sk.owner ?? '—'}
                        </span>
                        <span className="font-mono text-[11px] text-dim">
                          {formatRelativeTime(sk.created_at)}
                        </span>
                        {sk.is_platform_default ? (
                          <span
                            className="font-mono text-[11px] text-center px-2 py-1 border rounded-md cursor-default select-none text-accent border-accent-line bg-accent-soft"
                            title="Built-in platform skill: ships with the deployment and is enabled by default for every organization and user, no install needed (can be turned off per session)"
                          >
                            Built-in
                          </span>
                        ) : installed ? (
                          <span
                            className={`font-mono text-[11px] text-center px-2 py-1 border rounded-md cursor-default select-none ${
                              installed.enabled_by_default
                                ? 'text-accent border-accent-line bg-accent-soft'
                                : 'text-dim border-dashed border-line'
                            }`}
                            title={
                              installed.enabled_by_default
                                ? 'Installed; enabled by default in new sessions, can be turned off per session'
                                : 'Installed; not enabled automatically in new sessions, can be turned on per session'
                            }
                          >
                            {installed.enabled_by_default ? 'On by default' : 'On demand'}
                          </span>
                        ) : (
                          <button
                            className="cw-btn cw-btn-sm"
                            onClick={() => setToolboxTarget(sk)}
                          >
                            Install
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          )}

          {/* Knowledge source — folder-navigable, admin-managed */}
          {tab === 'docs' && <KnowledgeSourceTab onCount={setDocsCount} />}

        </div>
      </div>

      {installOpen && tab === 'mcp' && (
        <McpServerFormDialog
          mode="create"
          title="Install MCP tool"
          submitLabel="Install"
          // The library install path has no id-less probe endpoint (that is an
          // admin-governance surface); disable the tool-list fetch so the
          // filter editor falls back to a manual tag input.
          probe={{
            fetchTools: async () => ({ ok: false }),
            canFetch: false,
            disabledTitle: 'Tool discovery isn’t supported for library installs yet — enter tool names manually',
          }}
          onSubmit={(body) => api.library.installMcpServer(body as RegisterMcpServerRequest)}
          onClose={() => setInstallOpen(false)}
          onSaved={() => {
            setInstallOpen(false);
            void fetchAll();
          }}
        />
      )}

      {installOpen && tab === 'skills' && (
        <InstallModal
          onClose={() => setInstallOpen(false)}
          onInstalled={() => {
            setInstallOpen(false);
            void fetchAll();
          }}
        />
      )}

      {toolboxTarget && (
        <InstallToToolboxModal
          skill={toolboxTarget}
          onClose={() => setToolboxTarget(null)}
          onInstalled={() => {
            setToolboxTarget(null);
            installedSkills.refresh();
          }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Install modal (skills only). MCP install is handled by the shared
// McpServerFormDialog, rendered directly at the call site so its own Dialog
// frame is not nested inside this modal's overlay.
// ─────────────────────────────────────────────────────────────────────────────

const inputCls =
  'w-full px-2.5 py-1.5 text-[12.5px] border border-line bg-paper rounded-md ' +
  'focus:outline-none focus:border-accent transition-colors';
const labelCls = 'font-mono text-[11px] text-dim';

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className={labelCls}>{label}</span>
      {children}
    </label>
  );
}

function InstallModal({
  onClose,
  onInstalled,
}: {
  onClose: () => void;
  onInstalled: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Skill fields
  const [skName, setSkName] = useState('');
  const [skDesc, setSkDesc] = useState('');
  const [skVisibility, setSkVisibility] = useState<Skill['visibility']>('private');
  const [skVersion, setSkVersion] = useState('0.1.0');
  const [skFile, setSkFile] = useState<File | null>(null);

  const errMsg = (e: unknown) =>
    e instanceof ApiError ? e.message : 'Install failed. Please try again later.';

  const submit = async () => {
    setErr(null);
    setSubmitting(true);
    try {
      if (!skName.trim()) throw new ApiError(400, 'VALIDATION', 'Please enter a name');
      if (!skVersion.trim()) throw new ApiError(400, 'VALIDATION', 'Please enter a version');
      if (!skFile) throw new ApiError(400, 'VALIDATION', 'Please choose a skill package file (tarball)');
      // Local preflight: catch wrong format / oversized packages before any
      // network round-trip (and before we create the skill record).
      const preflightErr = preflightSkillFile(skFile);
      if (preflightErr) throw new ApiError(400, 'VALIDATION', preflightErr);
      // sha256 of the compressed bytes — the backend re-verifies integrity
      // against this. Optional: if Web Crypto is unavailable we skip it.
      const sha = await sha256Hex(skFile);
      const skill = await api.library.createSkill({
        name: skName.trim(),
        description: skDesc.trim() || null,
        visibility: skVisibility,
      });
      const fd = new FormData();
      fd.append('version', skVersion.trim());
      fd.append('file', skFile, skFile.name);
      if (sha) fd.append('expected_sha256', sha);
      await api.library.uploadSkillVersion(skill.id, fd);
      onInstalled();
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[420px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">Install skill</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          <Field label="Name">
            <input
              className={inputCls}
              value={skName}
              onChange={(e) => setSkName(e.target.value)}
              placeholder="e.g. pdf-summarizer"
            />
          </Field>
          <Field label="Description (optional)">
            <input
              className={inputCls}
              value={skDesc}
              maxLength={2000}
              onChange={(e) => setSkDesc(e.target.value)}
              placeholder="Briefly describe what this skill does"
            />
          </Field>
          <Field label="Visibility">
            <select
              className={inputCls}
              value={skVisibility}
              onChange={(e) => setSkVisibility(e.target.value as Skill['visibility'])}
            >
              <option value="private">Private</option>
              <option value="tenant">Tenant</option>
              <option value="public">Public</option>
            </select>
          </Field>
          <Field label="Version">
            <input
              className={inputCls}
              value={skVersion}
              onChange={(e) => setSkVersion(e.target.value)}
              placeholder="0.1.0"
            />
          </Field>
          <Field label="Skill package (tarball)">
            <input
              type="file"
              className="text-[12px] text-ink-2 file:mr-2 file:px-2 file:py-1 file:text-[11px] file:font-mono file:border file:border-line file:rounded-md file:bg-paper file:cursor-pointer"
              accept={SKILL_ACCEPT}
              onChange={(e) => setSkFile(e.target.files?.[0] ?? null)}
            />
          </Field>

          {err && (
            <div className="px-2.5 py-1.5 border border-accent-line bg-accent-soft rounded-md text-[12px] text-accent">
              {err}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={submit}
            disabled={submitting}
          >
            {submitting ? 'Installing…' : 'Install'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Install-to-toolbox modal — adds an existing shelf skill to the calling
// user's personal toolbox. Lets the user lock a specific version, or leave
// it on "follow latest" (the backend default → pinned_version_id = null).
// ─────────────────────────────────────────────────────────────────────────────

function InstallToToolboxModal({
  skill,
  onClose,
  onInstalled,
}: {
  skill: Skill;
  onClose: () => void;
  onInstalled: () => void;
}) {
  const [versions, setVersions] = useState<SkillVersion[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(true);
  // Empty string ⇒ "follow latest" (we omit the field in the request body).
  const [pickedVersion, setPickedVersion] = useState<string>('');
  const [enabledByDefault, setEnabledByDefault] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setVersionsLoading(true);
    api.library.listSkillVersions(skill.id)
      .then((vs) => {
        if (cancelled) return;
        setVersions(vs);
      })
      .catch((e) => {
        if (cancelled) return;
        setErr(e instanceof ApiError ? e.message : 'Failed to load versions');
      })
      .finally(() => {
        if (cancelled) return;
        setVersionsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [skill.id]);

  // Dropdown options: "follow latest" first (id=''), then each concrete version.
  const versionOptions = useMemo(
    () => [
      { id: '', label: 'Track latest' },
      ...versions.map((v) => ({ id: v.version, label: v.version })),
    ],
    [versions],
  );

  const submit = async () => {
    setErr(null);
    setSubmitting(true);
    try {
      await api.library.installSkill(
        skill.id,
        enabledByDefault,
        pickedVersion || undefined,
      );
      onInstalled();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : 'Install failed. Please try again later.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[420px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">Install skill</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          <Field label="Skill">
            <div className={`${inputCls} bg-paper-2 text-ink-2 cursor-default`}>
              {skill.name}
            </div>
          </Field>

          <Field label="Version">
            {versionsLoading ? (
              <div className="font-mono text-[11px] text-dim">Loading…</div>
            ) : (
              <Dropdown
                value={pickedVersion}
                options={versionOptions}
                onChange={setPickedVersion}
                fullWidth
              />
            )}
          </Field>

          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={enabledByDefault}
              onChange={(e) => setEnabledByDefault(e.target.checked)}
            />
            <span className="flex flex-col gap-0.5">
              <span className={labelCls}>Enable by default in new sessions</span>
              <span className="font-mono text-[10.5px] leading-4 text-dim">
                You can still turn it on or off per session from the composer’s ⊕ menu
              </span>
            </span>
          </label>

          {err && (
            <div className="px-2.5 py-1.5 border border-accent-line bg-accent-soft rounded-md text-[12px] text-accent">
              {err}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={submitting}>
            Cancel
          </button>
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={submit}
            disabled={submitting || versionsLoading}
          >
            {submitting ? 'Installing…' : 'Install'}
          </button>
        </div>
      </div>
    </div>
  );
}
