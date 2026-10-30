import { useEffect, useRef, useState, useCallback } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusLabel } from '../lib/format';
import { FilePreviewPanel } from '../components/FilePreviewPanel';
import { TaskBar } from '../components/tasks/TaskBar';
import type { FileContentSource } from '../components/filePreview';
import { FilePreviewProvider } from '../lib/filePreviewContext';
import { useWorkspace } from '../lib/workspaceContext';
import { api } from '../api/client';
import { LevelBadge } from '../lib/sensitivity';
import type { MessageAttachment, Workspace, WorkspaceFile } from '../api/types';

/**
 * Single-task agent view. The URL `/tasks/:id` drives `activeSessionId` in the
 * chat store so the existing `ChatWindow` / `useChat` SSE pipeline (B-M1) keeps
 * working unchanged — this page is essentially a Chinese-labelled shell around
 * those primitives plus a thin context rail.
 *
 * Canvas is entirely model-triggered (design §7.1): the worker's `create_canvas`
 * tool materializes a server canvas, the `canvas_created` SSE handler stamps the
 * assistant message's `canvas_id`, and ChatWindow renders the anchored
 * CanvasMessageCard off that id. This page has NO manual canvas entry points.
 *
 * Friendly mode: add `?mode=friendly` to the URL.
 */
export function AgentPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const friendly = searchParams.get('mode') === 'friendly';

  const { sessions } = useSessions();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const setActiveSession = useChatStore((s) => s.setActiveSession);
  // This session's own live-run flag (another session's run never blocks it).
  const sessionBusy = useChatStore((s) => (id ? Boolean(s.streamingSessions[id]) : false));
  const { sendMessage } = useChat();

  const [previewFile, setPreviewFile] = useState<{ id: string; filename: string; mime?: string } | null>(null);
  // Resizable preview panel width (px). Persisted across opens within a session.
  const [previewWidth, setPreviewWidth] = useState(360);
  const PREVIEW_MIN = 280;
  const PREVIEW_MAX = 760;
  const splitContainerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  const startPreviewDrag = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev: MouseEvent) => {
      if (!draggingRef.current || !splitContainerRef.current) return;
      const rect = splitContainerRef.current.getBoundingClientRect();
      // Preview is on the right; width = distance from cursor to right edge.
      const next = rect.right - ev.clientX;
      setPreviewWidth(Math.min(PREVIEW_MAX, Math.max(PREVIEW_MIN, next)));
    };
    const onUp = () => {
      draggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  // Keep the chat store's active session in sync with the URL. This is what
  // lets useChat() pick up the right transcript without changes to the hook.
  useEffect(() => {
    if (id && id !== activeSessionId) setActiveSession(id);
  }, [id, activeSessionId, setActiveSession]);

  useEffect(() => {
    if (!id) return;
    setPreviewFile(null);
  }, [id]);

  const session = sessions.find((s) => s.id === id);
  // The sidebar list holds only the pages loaded so far, so a session opened
  // any other way — a task started from the board, an older link — is fetched
  // on its own.  Kept in the list, its title follows renames and auto-titles.
  const upsertSession = useChatStore((s) => s.upsertSession);
  const sessionLoaded = session !== undefined;
  useEffect(() => {
    if (!id || sessionLoaded) return;
    let stale = false;
    api.sessions
      .get(id)
      .then((fetched) => {
        if (!stale) upsertSession(fetched);
      })
      .catch(() => {
        /* gone or not ours: the transcript area reports it */
      });
    return () => {
      stale = true;
    };
  }, [id, sessionLoaded, upsertSession]);
  // A task's lifecycle is shown by its TaskBar; a chat's status only when it
  // is not simply open (ACTIVE says nothing about whether a run is going).
  const isTaskSession = session?.metadata?.kind === 'task';
  const statusLabel =
    session && !isTaskSession && session.status !== 'ACTIVE' ? sessionStatusLabel(session.status) : null;
  // Workspace bound to THIS session (not the global active workspace) — sources
  // the "@" mention workspace files and the wsf_ preview content path.
  const workspaceId = session?.workspace_id ?? undefined;

  const openPreviewFile = useCallback((file: { id: string; filename: string; mime?: string }) => {
    setPreviewFile(file);
  }, []);

  // Content-source override for the preview panel: a `wsf_…` id is a workspace
  // base file, which lives under /workspaces/{id}/files/{fileId}/content rather
  // than the session path. Only built when a workspace is known; otherwise the
  // panel keeps its session-scoped behaviour (attachments, `att_…`).
  const previewGetSource =
    previewFile && previewFile.id.startsWith('wsf_') && workspaceId
      ? (fileId: string): FileContentSource => ({
          url: api.workspaces.fileContentUrl(workspaceId, fileId),
          getContent: () => api.workspaces.getContent(workspaceId, fileId),
        })
      : undefined;

  // If HomePage routed us here with an `initialMessage` in state, dispatch it
  // once the chat store has bound to this session. Guard with a ref so React
  // Strict Mode's double-effect doesn't double-send.  The router state is
  // cleared only once the prompt really went out (or failed visibly): a send
  // refused because the session is busy is retried when it frees up, and one
  // the server refused otherwise goes back into the composer, instead of
  // silently dropping the user's first prompt.
  const sentInitialRef = useRef<string | null>(null);
  const [initialDraft, setInitialDraft] = useState<string | null>(null);
  useEffect(() => {
    if (!id || activeSessionId !== id || sessionBusy) return;
    const state = location.state as
      | { initialMessage?: string; attachmentIds?: string[] }
      | null;
    const initial = state?.initialMessage?.trim();
    const attachmentIds = state?.attachmentIds ?? [];
    // Allow an attachment-only first message (no text) to still dispatch.
    if (!initial && attachmentIds.length === 0) return;
    if (sentInitialRef.current === id) return;
    sentInitialRef.current = id;
    void sendMessage(initial ?? '', attachmentIds).then((result) => {
      if (result === 'busy') {
        sentInitialRef.current = null; // try again once this session is free
        return;
      }
      if (result === 'rejected' && initial) setInitialDraft(initial);
      // Clear router state so a refresh won't re-send.
      navigate(location.pathname, { replace: true, state: null });
    });
  }, [id, activeSessionId, sessionBusy, location.pathname, location.state, navigate, sendMessage]);

  const handleSendMessage = useCallback(
    (
      text: string,
      attachmentIds?: string[],
      attachmentsMeta?: MessageAttachment[],
      workspaceFileIds?: string[],
    ) => sendMessage(text, attachmentIds, attachmentsMeta, workspaceFileIds),
    [sendMessage],
  );

  const hasSidePanel = previewFile !== null;

  if (!id) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={[{ label: 'Home', to: '/home' }, { label: 'Sessions' }]} />
        <div className="flex-1 flex items-center justify-center text-dim">No task specified.</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: 'Home', to: '/home' },
          { label: friendly ? 'Chat' : 'Sessions', to: '/home' },
          { label: session?.title || 'Untitled task' },
        ]}
        status={
          <span className="flex items-center gap-2">
            {/* Read-only badge: the workspace bound to THIS session at creation
                time. It is purely informational — there is no way to re-bind an
                existing session's workspace, so the badge never switches. */}
            {workspaceId && (
              <WorkspaceBadge workspaceId={workspaceId} onPreviewFile={openPreviewFile} />
            )}
            {statusLabel && (
              <span className="font-mono text-[11px] text-ink-2">
                · {statusLabel}
              </span>
            )}
          </span>
        }
        actions={
          <>
            {!friendly && (
              <button
                onClick={() => navigate('/files')}
                className="cw-btn cw-btn-ghost cw-btn-sm"
              >
                <span className="font-mono">⌗</span> Files
              </button>
            )}
          </>
        }
      />
      {/* Background-task lifecycle (status, attempts, Retry / Cancel); renders
          nothing for ordinary chats. */}
      <TaskBar sessionId={id} />

      <div
        className="min-h-0 flex-1"
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr',
        }}
      >
        {/* transcript area */}
        <section className="flex flex-col min-w-0 min-h-0 bg-surface">
          {/* Friendly mode banner */}
          {friendly && (
            <div className="px-8 pt-5">
              <div
                className="flex items-center gap-2.5 px-3.5 py-2.5 text-[12.5px] text-ink-2"
                style={{
                  border: '1px dashed var(--line)',
                  borderRadius: 'var(--r-md)',
                  background: 'var(--paper)',
                  maxWidth: 780,
                  margin: '0 auto',
                }}
              >
                <span
                  className="cw-avatar accent shrink-0 font-mono"
                  style={{ width: 22, height: 22 }}
                >
                  ◆
                </span>
                <div className="flex-1 leading-[1.45]">
                  <b className="text-ink font-semibold">Friendly mode.</b> I'll keep the chat warm and tuck technical work (tool calls, file edits, terminal output) into collapsed rows you can open if you're curious.
                </div>
              </div>
            </div>
          )}

          {/* delegated to the existing ChatWindow + useChat SSE pipeline */}
          <div
            ref={splitContainerRef}
            className="flex-1 min-h-0"
            style={
              hasSidePanel
                ? { display: 'grid', gridTemplateColumns: `1fr 6px ${previewWidth}px`, minHeight: 0 }
                : { display: 'flex', flexDirection: 'column', minHeight: 0 }
            }
          >
            <FilePreviewProvider value={openPreviewFile}>
              <ChatWindow
                sessionId={id}
                workspaceId={workspaceId}
                friendly={friendly}
                onPreviewFile={openPreviewFile}
                onSendMessage={handleSendMessage}
                externalPrefill={initialDraft}
                onExternalPrefillConsumed={() => setInitialDraft(null)}
              />
            </FilePreviewProvider>
            {hasSidePanel && (
              <>
                {/* Draggable divider */}
                <div
                  onMouseDown={startPreviewDrag}
                  role="separator"
                  aria-orientation="vertical"
                  aria-label="Drag to resize the preview panel"
                  title="Drag to resize the preview panel"
                  className="relative flex items-center justify-center"
                  style={{ cursor: 'col-resize', userSelect: 'none' }}
                  onMouseEnter={(e) => {
                    const bar = e.currentTarget.querySelector('[data-bar]') as HTMLElement | null;
                    if (bar) { bar.style.width = '3px'; bar.style.background = 'var(--accent)'; }
                  }}
                  onMouseLeave={(e) => {
                    if (draggingRef.current) return;
                    const bar = e.currentTarget.querySelector('[data-bar]') as HTMLElement | null;
                    if (bar) { bar.style.width = '1px'; bar.style.background = 'var(--line)'; }
                  }}
                >
                  <span
                    data-bar
                    className="h-full transition-all"
                    style={{ width: 1, background: 'var(--line)' }}
                  />
                </div>
                {previewFile && (
                  <FilePreviewPanel
                    sessionId={id}
                    file={previewFile}
                    onClose={() => setPreviewFile(null)}
                    getSource={previewGetSource}
                  />
                )}
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

/**
 * Read-only workspace badge for the session header. Shows `◇ <workspace name>` and, on
 * click, a small downward popover describing the workspace the session was bound
 * to at creation time: a truncated instructions preview and the list of base
 * files (lazy-loaded once). Clicking a file opens it in the session's preview
 * panel. There is deliberately NO switching UI here — an existing session's
 * workspace binding is immutable, so the badge only explains the binding.
 *
 * Popover mechanics mirror InputBar's "+" popover (container ref + absolute +
 * document-mousedown outside-close), but opens DOWNWARD (`top-full`) because the
 * badge lives in the page header at the top of the viewport.
 */
function WorkspaceBadge({
  workspaceId,
  onPreviewFile,
}: {
  workspaceId: string;
  onPreviewFile: (file: { id: string; filename: string; mime?: string }) => void;
}) {
  const { workspaces } = useWorkspace();
  // Prefer the already-loaded list; fall back to a direct GET only when the
  // session's workspace isn't among the user's own (e.g. shared/legacy).
  const known = workspaces.find((w) => w.id === workspaceId) ?? null;
  const [fetched, setFetched] = useState<Workspace | null>(null);
  const ws = known ?? fetched;

  useEffect(() => {
    if (known) return;
    let alive = true;
    api.workspaces
      .get(workspaceId)
      .then((w) => {
        if (alive) setFetched(w);
      })
      .catch(() => {
        /* silent — badge falls back to the generic “Workspace” label */
      });
    return () => {
      alive = false;
    };
  }, [known, workspaceId]);

  const name = ws?.name ?? 'Workspace';
  const instructions = ws?.instructions?.trim() || null;
  const instructionsPreview =
    instructions && instructions.length > 200
      ? `${instructions.slice(0, 200)}…`
      : instructions;

  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const [files, setFiles] = useState<WorkspaceFile[] | null>(null);
  const [filesLoading, setFilesLoading] = useState(false);

  // Close on outside click while open.
  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  // Lazy-load base files the first time the popover opens.
  useEffect(() => {
    if (!open || files !== null || filesLoading) return;
    setFilesLoading(true);
    api.workspaces
      .listFiles(workspaceId)
      .then((items) => setFiles(items))
      .catch(() => setFiles([]))
      .finally(() => setFilesLoading(false));
  }, [open, files, filesLoading, workspaceId]);

  return (
    <div ref={ref} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title="Workspace bound to this session (read-only)"
        className="inline-flex items-center gap-1.5 px-2 py-0.5 border border-line rounded-full bg-surface font-mono text-[11px] text-ink-2 hover:border-ink-2 hover:text-ink hover:bg-paper-2 transition-colors cursor-pointer max-w-[220px]"
      >
        <span aria-hidden className="text-accent">◇</span>
        <span className="truncate">{name}</span>
        <LevelBadge level={ws?.sensitivity_level} small />
      </button>

      {open && (
        <div
          role="dialog"
          className="absolute top-full left-0 mt-1.5 z-40 w-[300px] bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-2.5"
        >
          <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-1.5">
            Workspace · read-only
          </div>
          <div className="flex items-center gap-1.5 mb-2">
            <span className="text-[13px] font-semibold tracking-[-0.01em] text-ink truncate">
              {name}
            </span>
            <LevelBadge level={ws?.sensitivity_level} />
          </div>

          {instructionsPreview && (
            <div className="mb-2.5 pb-2.5 border-b border-line-2">
              <div className="cw-label mb-1">Workspace instructions</div>
              <div className="text-[12px] leading-[1.5] text-ink-2 whitespace-pre-wrap break-words">
                {instructionsPreview}
              </div>
            </div>
          )}

          <div className="cw-label mb-1">My workspace</div>
          <div className="max-h-[220px] overflow-auto cw-scroll -mx-0.5">
            {filesLoading ? (
              <div className="px-1 py-1.5 font-mono text-[11px] text-dim animate-pulse">
                Loading…
              </div>
            ) : files && files.length > 0 ? (
              files.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  onClick={() => {
                    onPreviewFile({ id: f.id, filename: f.filename, mime: f.mime });
                    setOpen(false);
                  }}
                  className="w-full flex items-center gap-1.5 px-1.5 py-1.5 rounded-md text-left hover:bg-paper-2 transition-colors"
                  title={f.filename}
                >
                  <span aria-hidden className="font-mono text-[11px] text-dim shrink-0">
                    ·
                  </span>
                  <span className="font-mono text-[12px] text-ink truncate">{f.filename}</span>
                </button>
              ))
            ) : (
              <div className="px-1 py-1.5 font-mono text-[11px] italic text-dim">
                No files yet
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
