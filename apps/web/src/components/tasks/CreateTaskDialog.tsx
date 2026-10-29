import { useEffect, useRef, useState } from 'react';
import { api, ApiError } from '../../api/client';
import type { Attachment, CreateTaskRequest, Task } from '../../api/types';
import { useInstalledSkills } from '../../hooks/useInstalledSkills';
import { useMcpServers } from '../../hooks/useMcpServers';
import { useModelOptions } from '../../hooks/useModelOptions';
import { useDefaultModel } from '../../hooks/useDefaultModel';
import { useWorkspace } from '../../lib/workspaceContext';
import { SENSITIVITY_LABELS, DEFAULT_SENSITIVITY } from '../../lib/sensitivity';
import { SYSTEM_FALLBACK_MODEL } from '../../lib/modelOptions';
import { appInputCls as inputCls } from '../app/AppMetaFields';

interface CreateTaskDialogProps {
  open: boolean;
  onClose: () => void;
  /** Called once the task exists (also when it exists but its run failed to start). */
  onCreated: (task: Task | null, startError?: string) => void;
}

const MAX_FILES = 20;

/**
 * Structured "New task" form.  Unlike the home composer it does not open a live
 * chat: the task is created and its first sandbox run starts in the background;
 * the board tracks it.
 *
 * Attachments are FK-bound to a session, so the first file pick creates a draft
 * `kind: 'task'` session that the submit then adopts (same pattern the App run
 * form uses).  A draft abandoned by closing the dialog is deleted.
 */
export function CreateTaskDialog({ open, onClose, onCreated }: CreateTaskDialogProps) {
  const { activeWorkspace, workspaces } = useWorkspace();
  const skills = useInstalledSkills();
  const { servers: mcpServers } = useMcpServers();
  const { options: modelOptions } = useModelOptions();
  const { modelId: defaultModelId } = useDefaultModel('llm');

  const [title, setTitle] = useState('');
  const [instructions, setInstructions] = useState('');
  const [workspaceId, setWorkspaceId] = useState('');
  const [model, setModel] = useState('');
  const [hitl, setHitl] = useState(true);
  const [showTools, setShowTools] = useState(false);
  // null = untouched → the server applies the user's defaults.
  const [selectedSkills, setSelectedSkills] = useState<string[] | null>(null);
  const [selectedMcp, setSelectedMcp] = useState<string[] | null>(null);
  const [files, setFiles] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const draftIdRef = useRef<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setTitle('');
    setInstructions('');
    setWorkspaceId(activeWorkspace?.id ?? '');
    setModel('');
    setHitl(true);
    setShowTools(false);
    setSelectedSkills(null);
    setSelectedMcp(null);
    setFiles([]);
    setError(null);
    draftIdRef.current = null;
    // Only re-seed when the dialog opens, not when the workspace list refreshes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const close = (submitted: boolean) => {
    const draft = draftIdRef.current;
    draftIdRef.current = null;
    if (!submitted && draft) {
      // Best-effort: an abandoned draft would otherwise linger as an idle task.
      api.sessions.delete(draft).catch(() => undefined);
    }
    onClose();
  };

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting && uploading === 0) close(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const ensureDraft = async (): Promise<string> => {
    if (draftIdRef.current) return draftIdRef.current;
    const session = await api.sessions.create('New task', { kind: 'task' }, workspaceId || undefined);
    draftIdRef.current = session.id;
    return session.id;
  };

  const pickFiles = async (list: FileList | null) => {
    if (!list || list.length === 0) return;
    const picked = Array.from(list).slice(0, Math.max(0, MAX_FILES - files.length));
    setError(null);
    setUploading((n) => n + picked.length);
    try {
      const sid = await ensureDraft();
      for (const file of picked) {
        try {
          // Same pre-upload gate as the composer: large files need the
          // large-attachment entitlement.  Fail-open if the check itself errors.
          const gate = await api.attachments.largeCheck(file.size).catch(() => null);
          if (gate && !gate.allowed) {
            setError(gate.reason || `“${file.name}” is too large to attach.`);
            continue;
          }
          const att = await api.attachments.upload(sid, file);
          setFiles((prev) => [...prev, att]);
        } catch (err) {
          setError(err instanceof ApiError ? `“${file.name}”: ${err.message}` : `Couldn’t upload “${file.name}”.`);
        } finally {
          setUploading((n) => n - 1);
        }
      }
    } catch (err) {
      setUploading(0);
      setError(err instanceof ApiError ? err.message : 'Couldn’t prepare the upload. Please try again.');
    }
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const removeFile = (att: Attachment) => {
    setFiles((prev) => prev.filter((f) => f.id !== att.id));
    const sid = draftIdRef.current;
    if (sid) api.artifacts.deleteFile(sid, att.id).catch(() => undefined);
  };

  const effectiveSkills = selectedSkills ?? skills.filter((s) => s.enabled_by_default).map((s) => s.name);
  const effectiveMcp = selectedMcp ?? mcpServers.filter((s) => s.enabled).map((s) => s.id);
  const toggle = (list: string[], value: string) =>
    list.includes(value) ? list.filter((v) => v !== value) : [...list, value];

  const submit = async () => {
    if (!instructions.trim()) {
      setError('Describe what the task should do.');
      return;
    }
    setSubmitting(true);
    setError(null);
    const body: CreateTaskRequest = {
      instructions: instructions.trim(),
      ...(title.trim() ? { title: title.trim() } : {}),
      ...(workspaceId ? { workspace_id: workspaceId } : {}),
      ...(draftIdRef.current ? { session_id: draftIdRef.current } : {}),
      ...(files.length ? { attachment_ids: files.map((f) => f.id) } : {}),
      ...(selectedSkills !== null ? { skills: selectedSkills } : {}),
      ...(selectedMcp !== null ? { mcp_server_ids: selectedMcp } : {}),
      model: model || defaultModelId || SYSTEM_FALLBACK_MODEL,
      hitl,
    };
    try {
      const { task } = await api.tasks.create(body);
      draftIdRef.current = null;
      onCreated(task);
      close(true);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : 'Couldn’t start the task.';
      // The task was saved but its run didn't start: it exists (not started)
      // and can be started again from the board.  Any other failure created
      // nothing, so keep the dialog open with the error.
      if (err instanceof ApiError && err.reason === 'task_not_started') {
        draftIdRef.current = null;
        onCreated(null, message);
        close(true);
      } else {
        setError(message);
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;
  const busy = submitting || uploading > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => { if (!busy) close(false); }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-task-title"
        className="w-full max-w-[620px] max-h-[90vh] flex flex-col border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line shrink-0">
          <div id="create-task-title" className="text-[13.5px] font-medium">New task</div>
          <button
            type="button"
            aria-label="Close"
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors disabled:opacity-40"
            onClick={() => close(false)}
            disabled={busy}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-4 px-4 py-4 overflow-y-auto cw-scroll">
          {error && (
            <div role="alert" className="border border-accent-line bg-accent-soft rounded-md px-3 py-2 text-[13px] text-accent">
              {error}
            </div>
          )}

          <div className="flex flex-col gap-1">
            <label className="cw-label" htmlFor="task-instructions">What should the agent do? *</label>
            <textarea
              id="task-instructions"
              className={`${inputCls} min-h-[140px] resize-y leading-[1.5]`}
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
              placeholder="e.g. Read the attached contracts, list every renewal date and notice period, and produce a spreadsheet sorted by deadline."
              autoFocus
              maxLength={20000}
            />
            <div className="text-[11.5px] text-dim">
              The task runs in its own sandbox in the background — you can close this page and come back to the board.
            </div>
          </div>

          <div className="flex flex-col gap-1">
            <label className="cw-label" htmlFor="task-title">Title</label>
            <input
              id="task-title"
              className={inputCls}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Optional — generated from the instructions when empty"
              maxLength={200}
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            <div className="flex flex-col gap-1">
              <label className="cw-label" htmlFor="task-workspace">Workspace</label>
              <select
                id="task-workspace"
                className={inputCls}
                value={workspaceId}
                onChange={(e) => setWorkspaceId(e.target.value)}
                // The draft session is already bound to the workspace it was
                // created in once a file has been attached.
                disabled={files.length > 0 || uploading > 0}
              >
                <option value="">No workspace</option>
                {workspaces.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name} ({SENSITIVITY_LABELS[w.sensitivity_level ?? DEFAULT_SENSITIVITY]})
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="cw-label" htmlFor="task-model">Model</label>
              <select
                id="task-model"
                className={inputCls}
                value={model}
                onChange={(e) => setModel(e.target.value)}
              >
                <option value="">Default{defaultModelId ? ` (${defaultModelId})` : ''}</option>
                {modelOptions.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <span className="cw-label">Files</span>
              <button
                type="button"
                className="cw-btn cw-btn-ghost cw-btn-sm"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy || files.length >= MAX_FILES}
              >
                <span className="font-mono">+</span> Attach
              </button>
              {uploading > 0 && <span className="font-mono text-[11px] text-dim">Uploading {uploading}…</span>}
              <input
                ref={fileInputRef}
                type="file"
                multiple
                className="hidden"
                aria-label="Attach files"
                onChange={(e) => void pickFiles(e.target.files)}
              />
            </div>
            {files.length > 0 && (
              <ul className="flex flex-col gap-1">
                {files.map((f) => (
                  <li key={f.id} className="flex items-center gap-2 text-[12.5px]">
                    <span className="truncate flex-1">{f.filename}</span>
                    <span className="font-mono text-[10.5px] text-dim">{Math.max(1, Math.round(f.size_bytes / 1024))} KB</span>
                    <button
                      type="button"
                      className="font-mono text-[11px] text-dim hover:text-ink"
                      aria-label={`Remove ${f.filename}`}
                      onClick={() => removeFile(f)}
                      disabled={submitting}
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="flex flex-col gap-2">
            <button
              type="button"
              className="self-start font-mono text-[11px] text-dim hover:text-ink"
              aria-expanded={showTools}
              onClick={() => setShowTools((v) => !v)}
            >
              {showTools ? '▾' : '▸'} Tools ({effectiveSkills.length} skills, {effectiveMcp.length} connectors)
            </button>
            {showTools && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 border border-line rounded-md p-3 bg-paper">
                <fieldset className="flex flex-col gap-1 min-w-0">
                  <legend className="cw-label mb-1">Skills</legend>
                  {skills.length === 0 && <span className="text-[12px] text-dim">No skills installed</span>}
                  {skills.map((s) => (
                    <label key={s.name} className="flex items-center gap-2 text-[12.5px] min-w-0">
                      <input
                        type="checkbox"
                        checked={effectiveSkills.includes(s.name)}
                        onChange={() => setSelectedSkills(toggle(effectiveSkills, s.name))}
                      />
                      <span className="truncate" title={s.description ?? undefined}>{s.name}</span>
                    </label>
                  ))}
                </fieldset>
                <fieldset className="flex flex-col gap-1 min-w-0">
                  <legend className="cw-label mb-1">Connectors (MCP)</legend>
                  {mcpServers.length === 0 && <span className="text-[12px] text-dim">No connectors configured</span>}
                  {mcpServers.map((s) => (
                    <label key={s.id} className="flex items-center gap-2 text-[12.5px] min-w-0">
                      <input
                        type="checkbox"
                        checked={effectiveMcp.includes(s.id)}
                        onChange={() => setSelectedMcp(toggle(effectiveMcp, s.id))}
                      />
                      <span className="truncate">{s.name}</span>
                    </label>
                  ))}
                </fieldset>
              </div>
            )}
          </div>

          <label className="flex items-start gap-2 text-[12.5px]">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={hitl}
              onChange={(e) => setHitl(e.target.checked)}
            />
            <span>
              Ask me before sensitive actions
              <span className="block text-[11.5px] text-dim">
                When off, the task runs unattended: the agent won’t pause for approvals or questions.
              </span>
            </span>
          </label>
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line shrink-0">
          <button type="button" className="cw-btn cw-btn-ghost cw-btn-sm" onClick={() => close(false)} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="cw-btn cw-btn-primary cw-btn-sm"
            onClick={() => void submit()}
            disabled={busy || !instructions.trim()}
          >
            {submitting ? 'Starting…' : 'Start task'}
          </button>
        </div>
      </div>
    </div>
  );
}
