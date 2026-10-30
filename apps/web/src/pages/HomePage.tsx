import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { useTranslation } from '../i18n';
import { formatDate } from '../i18n/dates';
import { useWorkspace } from '../lib/workspaceContext';
import { SENSITIVITY_LABELS, DEFAULT_SENSITIVITY } from '../lib/sensitivity';
import { useSessions } from '../hooks/useSessions';
import { useTasks } from '../hooks/useTasks';
import { api } from '../api/client';
import type { Attachment, Session, Task } from '../api/types';
import { formatRelativeTime } from '../lib/format';
import { StatusDot, taskToDesignStatus } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { Composer } from '../components/composer/Composer';
import { DRAFT_SESSION_ID, useChatStore } from '../stores/chatStore';
import { useApps } from '../hooks/useApps';
import { useInstalledSkills } from '../hooks/useInstalledSkills';
import { useMcpServers } from '../hooks/useMcpServers';
import { CreateAppModal } from '../components/app/CreateAppModal';
import { CreateTaskDialog } from '../components/tasks/CreateTaskDialog';
import { GridBackdrop } from '../components/GridBackdrop';
import { SYSTEM_FALLBACK_MODEL, DEFAULT_EFFORT } from '../lib/modelOptions';
import { useDefaultModel } from '../hooks/useDefaultModel';
import { useFeatureFlag } from '../hooks/useFeatureFlags';

const PLACEHOLDERS = [
  'Plan tomorrow’s team stand-up…',
  'Summarize this PDF in 5 bullet points?',
  'Draft a reply to the supplier’s email…',
  'What’s the best path to migrate to passkeys?',
];

const SUGGESTIONS = ['Plan my day tomorrow', 'Summarize this PDF', 'Reply to a supplier email', 'Brainstorm Q3 launch names'];

/** Default glyph for an App card when the App has no icon set. */
const DEFAULT_APP_GLYPH = '▤';
// The four most recently touched conversations, tasks and app runs.
const RECENT_PARAMS = { limit: 4 } as const;

export function HomePage() {
  const { lang } = useTranslation();
  const {
    activeWorkspace,
    workspaces,
    loading: workspacesLoading,
    setActiveWorkspaceId,
  } = useWorkspace();
  const { createSession } = useSessions();
  // The rail shows each session's real state, derived from its latest run;
  // the session status alone says only whether it is open.
  const { items: recent } = useTasks(RECENT_PARAMS);
  const { apps, loading: appsLoading } = useApps('published');
  const { apps: myApps, loading: myAppsLoading } = useApps('mine');
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showCreateTask, setShowCreateTask] = useState(false);
  const [phIdx, setPhIdx] = useState(0);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // Reasoning-effort control is gated behind the same flag as the chat page
  // (ChatWindow). Default false → the "Off▾" dropdown is hidden until enabled.
  const showThinkingUi = useFeatureFlag('ai.thinking_ui');
  const { modelId: defaultModelId, isLoading: defaultModelLoading } = useDefaultModel('llm');
  const [model, setModelRaw] = useState<string>(SYSTEM_FALLBACK_MODEL);
  const [effort, setEffort] = useState<string>(DEFAULT_EFFORT);
  // Track whether the user has manually picked a model in the composer.
  // If so, don't overwrite it when the hook resolves later.
  const userModelChosen = useRef(false);
  const setModel = (m: string) => {
    userModelChosen.current = true;
    setModelRaw(m);
  };
  // When the hook finishes loading, auto-fill the resolved default — but only
  // if the user hasn't already made a manual selection.
  useEffect(() => {
    if (!defaultModelLoading && defaultModelId && !userModelChosen.current) {
      setModelRaw(defaultModelId);
    }
  }, [defaultModelLoading, defaultModelId]);
  const setSessionModel = useChatStore((s) => s.setSessionModel);
  const setSessionEffort = useChatStore((s) => s.setSessionEffort);
  // Skill / MCP toggles for a task that has no session yet — they live in the
  // store's draft slot and are handed to the real session id in startChat.
  const skills = useInstalledSkills();
  const { servers: mcpServers } = useMcpServers();
  const draftSkills = useChatStore((s) => s.selectedSkills[DRAFT_SESSION_ID]);
  const draftMcpServerIds = useChatStore((s) => s.selectedMcpServerIds[DRAFT_SESSION_ID]);
  const toggleSkill = useChatStore((s) => s.toggleSkill);
  const seedSkills = useChatStore((s) => s.seedSkills);
  const toggleMcpServer = useChatStore((s) => s.toggleMcpServer);
  const seedMcpServerIds = useChatStore((s) => s.seedMcpServerIds);
  const commitDraftSelections = useChatStore((s) => s.commitDraftSelections);
  // Attachments require a session_id (NOT NULL FK), so the session must exist
  // before we can upload.  We create it lazily on the first file pick and
  // reuse it on send so the session isn't created twice.
  const sessionRef = useRef<Session | null>(null);

  useEffect(() => {
    if (draft) return;
    const t = setInterval(() => setPhIdx((p) => (p + 1) % PLACEHOLDERS.length), 3200);
    return () => clearInterval(t);
  }, [draft]);

  // Create the session up front (or return the one already created for an
  // earlier upload). The first persisted message triggers server-side
  // automatic naming; this placeholder keeps creation independent of prompt
  // length and is replaced through the session SSE stream.
  const ensureSession = async (): Promise<Session | null> => {
    if (sessionRef.current) return sessionRef.current;
    // Started inside a workspace → tag the session with it so the backend
    // injects the workspace's instructions / base files / skills.
    const session = await createSession('New task', activeWorkspace?.id);
    if (session) sessionRef.current = session;
    return session ?? null;
  };

  // The shared Composer owns the attachment list + the large-attachment gate;
  // all it needs from us is "upload this file and give me the Attachment".
  // Attachments are FK-bound to a session, hence ensureSession() first.
  const uploadAttachment = async (file: File): Promise<Attachment> => {
    const session = await ensureSession();
    if (!session) throw new Error('Couldn’t create the task. Please try again later.');
    return api.attachments.upload(session.id, file);
  };

  const startChat = async (text: string, attachmentIds?: string[]): Promise<boolean> => {
    setSubmitError(null);
    setSubmitting(true);
    try {
      // Reuse the session created during an earlier file upload, if any.
      const session = await ensureSession();
      if (!session) throw new Error('Couldn’t create the task. Please try again later.');

      // Persist the composer's model + effort choice onto the new session so
      // useChat picks them up at send time (same store the chat page reads).
      setSessionModel(session.id, model);
      setSessionEffort(session.id, effort);
      // Hand the "+" menu's pre-session skill / MCP choices to the session
      // that was just created, and clear the draft so the next new task
      // starts from the defaults instead of inheriting this one.
      commitDraftSelections(session.id);
      // Pass the first user message and any pre-uploaded attachments through
      // router state so AgentPage can dispatch them once the chat store is
      // bound to the new session.  Without this the draft was being thrown
      // away.
      navigate(`/tasks/${session.id}?mode=friendly`, {
        state: {
          initialMessage: text,
          attachmentIds: attachmentIds ?? [],
        },
      });
      return true;
    } catch (err) {
      const detail = err instanceof Error ? err.message : '';
      setSubmitError(detail || 'Couldn’t create the task. Please try again later.');
      console.warn('create session failed', err);
      return false;
    } finally {
      setSubmitting(false);
    }
  };


  const openRecent = (task: Task) => {
    if (task.kind === 'app_run' && task.latest_run) {
      navigate(`/apps/runs/${task.id}/${task.latest_run.id}`);
      return;
    }
    navigate(`/tasks/${task.id}`);
  };

  const dateLabel = formatDate(new Date(), lang, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: 'Home' }]}
        actions={
          <>
            <button className="cw-btn cw-btn-ghost cw-btn-sm">⌘K Launch</button>
          </>
        }
      />

      <div className="relative flex-1 min-h-0 flex flex-col">
        <GridBackdrop />

        <div className="cw-scroll flex-1 px-[5vw] pt-8 pb-8 flex flex-col items-center gap-7 relative">
          {/* greeting */}
          <div className="text-center shrink-0">
            <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-2">
              {dateLabel}
            </div>
            <h1 className="m-0 font-display font-normal tracking-[-0.02em] leading-[1.15] text-[clamp(24px,3.4vw,40px)]">
              Hello.
              <br />
              <span className="text-dim">What can I do for you?</span>
            </h1>
          </div>

          {/* composer — shared with the chat page (components/composer/Composer) */}
          <div className="w-[min(720px,95%)] shrink-0">
            <Composer
              variant="home"
              text={draft}
              onTextChange={setDraft}
              placeholder={PLACEHOLDERS[phIdx]}
              onSend={startChat}
              onUpload={uploadAttachment}
              busy={submitting}
              skills={{
                skills,
                selected: draftSkills,
                // Seed-then-toggle against the DRAFT slot: no session exists
                // yet and toggling a skill must never create one.
                onToggle: (name) => {
                  seedSkills(
                    DRAFT_SESSION_ID,
                    skills.filter((s) => s.enabled_by_default).map((s) => s.name),
                  );
                  toggleSkill(DRAFT_SESSION_ID, name);
                },
              }}
              mcp={{
                servers: mcpServers,
                selected: draftMcpServerIds,
                onToggle: (id) => {
                  seedMcpServerIds(
                    DRAFT_SESSION_ID,
                    mcpServers.filter((s) => s.enabled).map((s) => s.id),
                  );
                  toggleMcpServer(DRAFT_SESSION_ID, id);
                },
              }}
              workspace={{
                value: activeWorkspace?.id ?? '',
                options: workspaces.map((w) => ({
                  id: w.id,
                  label: `${w.name} (${SENSITIVITY_LABELS[w.sensitivity_level ?? DEFAULT_SENSITIVITY]})`,
                })),
                onChange: setActiveWorkspaceId,
                disabled: workspacesLoading || workspaces.length === 0,
                emptyLabel: workspacesLoading ? 'Loading workspaces…' : 'No workspaces',
              }}
              model={model}
              onModelChange={setModel}
              effort={effort}
              onEffortChange={showThinkingUi ? setEffort : undefined}
            />
          </div>

          {submitError && (
            <div role="alert" className="w-[min(720px,95%)] px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {submitError}
            </div>
          )}

          {/* suggestions */}
          <div className="flex gap-2 flex-wrap justify-center max-w-[720px]">
            <span className="cw-label self-center">Try:</span>
            {SUGGESTIONS.map((s) => (
              <Chip key={s} dashed onClick={() => setDraft(s)}>{s}</Chip>
            ))}
          </div>

          {/* divider */}
          <div className="w-[min(960px,95%)] flex items-center gap-3.5 mt-1">
            <div className="flex-1 h-px bg-line" />
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              Or pick an app
            </span>
            <button
              type="button"
              onClick={() => setShowCreateModal(true)}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              title="Create an app from scratch"
            >
              <span className="font-mono">+</span> Create app
            </button>
            <div className="flex-1 h-px bg-line" />
          </div>

          {/* 应用中心 — published apps */}
          <div className="w-[min(960px,95%)] grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            {/* 空白任务 — a structured background task (instructions, files, tools) */}
            <div
              role="button"
              tabIndex={0}
              onClick={() => setShowCreateTask(true)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  setShowCreateTask(true);
                }
              }}
              className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
            >
              <div className="flex items-center gap-2.5 mb-0.5">
                <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
                  +
                </div>
                <div className="text-[14px] font-semibold tracking-[-0.01em]">Blank task</div>
                <div className="flex-1" />
                <span className="font-mono text-[10.5px] text-dim">→</span>
              </div>
              <div className="text-[12px] text-dim leading-[1.45]">
                Describe a goal, attach files, and let it run in the background.
              </div>
              <div className="flex-1" />
              <div className="flex gap-1 flex-wrap">
                <Chip>Free-form</Chip>
                <Chip>Background</Chip>
              </div>
            </div>

            {/* published apps */}
            {apps.map((app) => (
              <div
                key={app.id}
                onClick={() => navigate(`/apps/${app.id}`)}
                className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
              >
                <div className="flex items-center gap-2.5 mb-0.5">
                  <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
                    {app.icon || DEFAULT_APP_GLYPH}
                  </div>
                  <div className="text-[14px] font-semibold tracking-[-0.01em]">{app.name}</div>
                  <div className="flex-1" />
                  <span className="font-mono text-[10.5px] text-dim">→</span>
                </div>
                <div className="text-[12px] text-dim leading-[1.45]">
                  {app.description || 'Run this app'}
                </div>
                <div className="flex-1" />
                <div className="flex gap-1 flex-wrap">
                  <Chip>App</Chip>
                </div>
              </div>
            ))}

            {!appsLoading && apps.length === 0 && (
              <div className="border border-line border-dashed bg-surface rounded-md p-3.5 flex flex-col gap-2 items-start justify-center min-h-[120px]">
                <div className="font-mono italic text-dim text-[12px]">No published apps yet</div>
              </div>
            )}
          </div>

          <div className="w-[min(960px,95%)] flex justify-end -mt-2">
            <button
              type="button"
              onClick={() => navigate('/apps?tab=published')}
              className="font-mono text-[11px] text-dim hover:text-ink transition-colors"
            >
              Open App Center →
            </button>
          </div>

          {/* 我的应用 divider */}
          <div className="w-[min(960px,95%)] flex items-center gap-3.5 mt-1">
            <div className="flex-1 h-px bg-line" />
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              My apps
            </span>
            <div className="flex-1 h-px bg-line" />
          </div>

          {/* 我的应用 — my apps */}
          <div className="w-[min(960px,95%)] grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            {myApps.map((app) => {
              const latestVersion = app.versions?.length
                ? app.versions[app.versions.length - 1]
                : null;
              const statusLabel =
                latestVersion?.status === 'draft' ? 'Draft'
                : latestVersion?.status === 'pending_review' ? 'In review'
                : latestVersion?.status === 'published' ? 'Published'
                : latestVersion?.status === 'rejected' ? 'Rejected'
                : 'Draft';
              return (
                <div
                  key={app.id}
                  onClick={() => navigate(`/apps/${app.id}/edit`)}
                  className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
                >
                  <div className="flex items-center gap-2.5 mb-0.5">
                    <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
                      {app.icon || DEFAULT_APP_GLYPH}
                    </div>
                    <div className="text-[14px] font-semibold tracking-[-0.01em]">{app.name}</div>
                    <div className="flex-1" />
                    <span className="font-mono text-[10.5px] text-dim">→</span>
                  </div>
                  <div className="text-[12px] text-dim leading-[1.45]">
                    {app.description || 'Click to edit this app'}
                  </div>
                  <div className="flex-1" />
                  <div className="flex gap-1 flex-wrap">
                    <Chip>{statusLabel}</Chip>
                  </div>
                </div>
              );
            })}

            {!myAppsLoading && myApps.length === 0 && (
              <div className="border border-line border-dashed bg-surface rounded-md p-3.5 flex flex-col gap-2 items-start justify-center min-h-[120px]">
                <div className="font-mono italic text-dim text-[12px]">You haven’t created any apps yet</div>
                <button
                  type="button"
                  onClick={() => setShowCreateModal(true)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                >
                  <span className="font-mono">+</span> Create app
                </button>
              </div>
            )}
          </div>

          <div className="w-[min(960px,95%)] flex justify-end -mt-2">
            <button
              type="button"
              onClick={() => navigate('/apps')}
              className="font-mono text-[11px] text-dim hover:text-ink transition-colors"
            >
              View all →
            </button>
          </div>
        </div>

        {/* recent activity rail */}
        <div className="border-t border-line bg-paper px-6 py-3 shrink-0 relative">
          <div className="flex items-baseline justify-between mb-2.5">
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              Recent
            </span>
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-2.5">
            {recent.map((s) => (
              <div
                key={s.id}
                onClick={() => openRecent(s)}
                className="border border-line rounded-md p-3 bg-surface cursor-pointer flex flex-col gap-2 hover:border-ink-2 hover:-translate-y-px transition-all"
              >
                <div className="flex items-center gap-2">
                  <StatusDot status={taskToDesignStatus(s.status)} />
                  <div className="flex-1" />
                  <span className="font-mono text-[10.5px] text-dim">
                    {formatRelativeTime(s.updated_at)}
                  </span>
                </div>
                <div className="text-[13px] font-medium tracking-[-0.01em] leading-[1.3] truncate">
                  {s.title || 'Untitled task'}
                </div>
              </div>
            ))}
            {recent.length === 0 && (
              <div className="font-mono italic text-dim text-[11px] px-2">No tasks yet</div>
            )}
          </div>
        </div>
      </div>

      <CreateAppModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
      />
      <CreateTaskDialog
        open={showCreateTask}
        onClose={() => setShowCreateTask(false)}
        onCreated={(task) => {
          // Started → watch it run; saved but not started → the board, where
          // its card offers Start.
          navigate(task ? `/tasks/${task.id}` : '/tasks');
        }}
      />
    </div>
  );
}
