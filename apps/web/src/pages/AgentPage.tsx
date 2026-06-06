import { useEffect, useRef, useState, useCallback } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { Chip } from '../components/shell/Chip';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusZh } from '../lib/format';
import { useWorkspace } from '../lib/workspaceContext';
import { api } from '../api/client';
import type { Run, RunState, Attachment, InstalledSkill } from '../api/types';
import { FilePreviewPanel } from '../components/FilePreviewPanel';
import { OutputFileCards } from '../components/OutputFileCards';
import { useOutputAttachments } from '../hooks/useOutputAttachments';

// ── helper: relative elapsed string ─────────────────────────────────────────
function elapsedStr(from: string | null, to?: string | null): string {
  if (!from) return '—';
  const startMs = new Date(from).getTime();
  const endMs = to ? new Date(to).getTime() : Date.now();
  const totalSec = Math.max(0, Math.floor((endMs - startMs) / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m ${s}s`;
}

// ── helper: pick the "active" run (RUNNING/PENDING first, else newest) ───────
function pickRun(items: Run[]): Run | null {
  if (!items.length) return null;
  const active = items.find((r) => r.status === 'RUNNING' || r.status === 'PENDING');
  if (active) return active;
  return items.reduce((a, b) => (a.created_at > b.created_at ? a : b));
}

// ── hook: runs for a session (polls while RUNNING) ───────────────────────────
function useRuns(sessionId: string | undefined) {
  const [runs, setRuns] = useState<Run[]>([]);

  const fetch_ = useCallback(async () => {
    if (!sessionId) return;
    try {
      const res = await api.runs.list(sessionId);
      setRuns(Array.isArray(res?.items) ? res.items : []);
    } catch {
      // failure-open: keep whatever was there
    }
  }, [sessionId]);

  useEffect(() => {
    fetch_();
  }, [fetch_]);

  const run = pickRun(runs);
  const isRunning = run?.status === 'RUNNING' || run?.status === 'PENDING';

  // Poll every 4 s while RUNNING/PENDING
  useEffect(() => {
    if (!isRunning) return;
    const timer = setInterval(fetch_, 4000);
    return () => clearInterval(timer);
  }, [isRunning, fetch_]);

  return { run, isRunning };
}

// ── hook: installed skills ───────────────────────────────────────────────────
// The rail shows the user's installed skills (their toolbox), not the browse
// shelf. enabled_by_default seeds the per-session selection (see AgentPage).
function useInstalledSkills() {
  const [skills, setSkills] = useState<InstalledSkill[]>([]);

  useEffect(() => {
    api.library.listInstalledSkills()
      .then((res) => setSkills(Array.isArray(res?.items) ? res.items : []))
      .catch(() => {/* failure-open */});
  }, []);

  return skills;
}

// ── hook: session attachments ─────────────────────────────────────────────────
function useAttachments(sessionId: string | undefined) {
  const [attachments, setAttachments] = useState<Attachment[]>([]);

  useEffect(() => {
    if (!sessionId) return;
    api.attachments.list(sessionId)
      .then((res) => setAttachments(Array.isArray(res?.items) ? res.items : []))
      .catch(() => {/* failure-open */});
  }, [sessionId]);

  return attachments;
}

// ── hook: run state (plan text) ───────────────────────────────────────────────
function useRunState(sessionId: string | undefined, run: Run | null) {
  const [state, setState] = useState<RunState | null>(null);

  const fetch_ = useCallback(async () => {
    if (!sessionId || !run) return;
    try {
      const res = await api.runs.getState(sessionId, run.id);
      setState(res);
    } catch {
      // failure-open: treat as empty plan
    }
  }, [sessionId, run]);

  useEffect(() => {
    fetch_();
  }, [fetch_]);

  const isActive = run?.status === 'RUNNING' || run?.status === 'PENDING';

  // Poll every 3 s while RUNNING/PENDING; stop on terminal status
  useEffect(() => {
    if (!isActive) return;
    const timer = setInterval(fetch_, 3000);
    return () => clearInterval(timer);
  }, [isActive, fetch_]);

  return state;
}

/**
 * Single-task agent view. The URL `/tasks/:id` drives `activeSessionId` in the
 * chat store so the existing `ChatWindow` / `useChat` SSE pipeline (B-M1) keeps
 * working unchanged — this page is essentially a Chinese-labelled shell around
 * those primitives plus a thin context rail.
 *
 * Friendly mode: add `?mode=friendly` to the URL.
 */
export function AgentPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const friendly = searchParams.get('mode') === 'friendly';

  const { ws } = useWorkspace();
  const { sessions } = useSessions();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const setActiveSession = useChatStore((s) => s.setActiveSession);
  const selectedSkills = useChatStore((s) => (id ? s.selectedSkills[id] : undefined));
  const toggleSkill = useChatStore((s) => s.toggleSkill);
  const seedSkills = useChatStore((s) => s.seedSkills);
  const { sendMessage, cancel, isStreaming } = useChat();

  // Real data hooks for context rail
  const { run } = useRuns(id);
  const skills = useInstalledSkills();

  // Seed this session's enabled skills from the user's install defaults, once
  // the installed list loads — only if the user hasn't chosen yet this session.
  useEffect(() => {
    if (!id || skills.length === 0) return;
    seedSkills(id, skills.filter((s) => s.enabled_by_default).map((s) => s.name));
  }, [id, skills, seedSkills]);
  const attachments = useAttachments(id);
  // Read-only input context: files handed to the agent (kind='input').
  const inputAttachments = attachments.filter((a) => a.kind === 'input');
  const runState = useRunState(id, run);
  const { byRunId: outputsByRun, refresh: refreshOutputs } = useOutputAttachments(id);

  // Refresh rail output files when a run finishes streaming (true → false edge),
  // mirroring ChatWindow so newly written deliverables surface promptly.
  const prevStreamingRailRef = useRef(isStreaming);
  useEffect(() => {
    if (prevStreamingRailRef.current && !isStreaming) refreshOutputs();
    prevStreamingRailRef.current = isStreaming;
  }, [isStreaming, refreshOutputs]);

  // Friendly defaults rail hidden; non-friendly defaults rail shown.
  const [showRail, setShowRail] = useState(!friendly);
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

  // If HomePage routed us here with an `initialMessage` in state, dispatch it
  // once the chat store has bound to this session.  Guard with a ref so React
  // Strict Mode's double-effect doesn't double-send.
  const sentInitialRef = useRef<string | null>(null);
  useEffect(() => {
    if (!id || activeSessionId !== id) return;
    const state = location.state as
      | { initialMessage?: string; attachmentIds?: string[] }
      | null;
    const initial = state?.initialMessage?.trim();
    const attachmentIds = state?.attachmentIds ?? [];
    // Allow an attachment-only first message (no text) to still dispatch.
    if (!initial && attachmentIds.length === 0) return;
    if (sentInitialRef.current === id) return;
    sentInitialRef.current = id;
    sendMessage(initial ?? '', attachmentIds);
    // Clear router state so a refresh won't re-send.
    navigate(location.pathname, { replace: true, state: null });
  }, [id, activeSessionId, location.pathname, location.state, navigate, sendMessage]);

  const session = sessions.find((s) => s.id === id);

  if (!id) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={[{ label: '首页', to: '/home' }, { label: '任务' }]} />
        <div className="flex-1 flex items-center justify-center text-dim">未指定任务。</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: '首页', to: '/home' },
          { label: friendly ? 'Chat' : '任务', to: friendly ? '/home' : '/tasks' },
          { label: session?.title || '未命名任务' },
        ]}
        status={
          session && (
            <span className="font-mono text-[11px] text-ink-2">
              · {sessionStatusZh(session.status)}
            </span>
          )
        }
        actions={
          friendly ? (
            <>
              <button
                onClick={() => setShowRail((v) => !v)}
                className="cw-btn cw-btn-ghost cw-btn-sm"
                title="Toggle technical context — plan, files, worker stats"
              >
                <span className="font-mono">{showRail ? '◧' : '◦'}</span> details
              </button>
              <button className="cw-btn cw-btn-sm">pause</button>
            </>
          ) : (
            <>
              <button
                onClick={() => navigate('/files')}
                className="cw-btn cw-btn-ghost cw-btn-sm"
              >
                <span className="font-mono">⌗</span> 文件
              </button>
              {isStreaming && (
                <button onClick={cancel} className="cw-btn cw-btn-sm" style={{ color: '#d23a1f', borderColor: '#f5c2ba', background: '#fef2f0' }}>
                  停止
                </button>
              )}
            </>
          )
        }
      />

      <div
        className="min-h-0 flex-1"
        style={{
          display: 'grid',
          gridTemplateColumns: showRail ? '200px 1fr' : '1fr',
        }}
      >
        {/* context rail */}
        {showRail && (
          <aside
            className="border-r border-line p-3 flex flex-col gap-4 bg-paper cw-scroll"
            style={{ overflow: 'hidden' }}
          >
            {/* ── Input context (read-only) ── */}
            <div>
              <div className="cw-label mb-1.5">输入上下文</div>
              {/* Read-only files the agent was handed for this session. */}
              <div className="flex flex-col gap-0.5 pl-1">
                {inputAttachments.slice(0, 6).map((f) => (
                  <div
                    key={f.id}
                    className="flex items-center gap-1.5 px-1 font-mono text-[10.5px] text-ink-2 min-w-0"
                    title={f.filename}
                  >
                    <span style={{ color: ws.accent, fontSize: 9 }}>●</span>
                    <span className="whitespace-nowrap overflow-hidden text-ellipsis flex-1">
                      {f.filename}
                    </span>
                    <span className="font-mono text-[8.5px] text-dim shrink-0">只读</span>
                  </div>
                ))}
                {inputAttachments.length === 0 && (
                  <span className="font-mono text-[11px] italic text-dim">无输入文件</span>
                )}
              </div>
              {/* Injected memory / persona — backend endpoint not wired yet. */}
              <div className="mt-2 pl-1 flex items-center gap-1.5">
                <span className="font-mono text-[10px] text-dim">记忆 · 人格</span>
                <span className="font-mono text-[8.5px] text-dim italic">待接线</span>
              </div>
            </div>

            {/* ── Worker card — real run data ── */}
            <div>
              <div className="cw-label mb-1.5">Worker</div>
              {run ? (
                <div
                  className="flex items-center gap-2 px-2 py-1.5 bg-surface border border-line"
                  style={{ borderRadius: 'var(--r-sm)' }}
                >
                  <span className="cw-avatar accent lg shrink-0">
                    {(run.sandbox_id ?? run.id).slice(0, 1).toUpperCase()}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-[12px] font-semibold truncate">
                      {run.sandbox_id ?? run.id.slice(0, 8)}
                    </div>
                    <div className="font-mono text-[10px] text-dim">
                      since {elapsedStr(run.started_at)} ago
                    </div>
                  </div>
                </div>
              ) : (
                <span className="font-mono text-[11px] italic text-dim">暂无运行</span>
              )}
            </div>

            {/* ── Skills — click to enable/disable for this session ── */}
            <div>
              <div className="cw-label mb-1.5">
                Skills · {(selectedSkills?.length ?? 0)}/{skills.length}
              </div>
              <div className="flex flex-wrap gap-1">
                {skills.map((s) => {
                  const on = selectedSkills?.includes(s.name) ?? false;
                  return (
                    <Chip
                      key={s.id}
                      accent={on}
                      dashed={!on}
                      onClick={id ? () => toggleSkill(id, s.name) : undefined}
                    >
                      {s.name}
                    </Chip>
                  );
                })}
                {skills.length === 0 && (
                  <span className="font-mono text-[11px] italic text-dim">暂无 skill</span>
                )}
              </div>
            </div>

            {/* ── Plan (real data from run state) ── */}
            <div>
              <div className="cw-label mb-1.5">当前思路</div>
              {runState?.plan ? (
                <div
                  className="font-mono text-[11px] text-ink-2 leading-[1.6]"
                  style={{
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                    maxHeight: 200,
                    overflowY: 'auto',
                  }}
                >
                  {runState.plan}
                </div>
              ) : (
                <span className="font-mono text-[11px] italic text-dim">暂无计划信息</span>
              )}
            </div>

            {/* ── Run outputs — real artifacts written by the current run ── */}
            <div>
              <div className="cw-label mb-1.5">本次产物</div>
              {run && (outputsByRun.get(run.id)?.length ?? 0) > 0 ? (
                <OutputFileCards
                  files={outputsByRun.get(run.id)!}
                  sessionId={id}
                  onPreviewFile={setPreviewFile}
                />
              ) : (
                <span className="font-mono text-[11px] italic text-dim">暂无产物</span>
              )}
            </div>

            <div className="flex-1" />

            {/* ── Live stats: elapsed + real token count ── */}
            <div>
              <div className="cw-label mb-1.5">Live</div>
              {isStreaming && (
                <div className="flex items-center gap-1.5 mb-1.5 px-2 py-1 border border-dashed border-accent-line bg-accent-soft rounded-sm">
                  <span className="w-1.5 h-1.5 rounded-full bg-accent animate-blip shrink-0" />
                  <span className="font-mono text-[10.5px] text-accent">运行中…</span>
                </div>
              )}
              <div className="font-mono text-[10.5px] text-ink-2 leading-[1.6]">
                <div>tokens · {run?.total_tokens != null
                  ? (run.total_tokens >= 1000
                    ? `${(run.total_tokens / 1000).toFixed(1)}k`
                    : String(run.total_tokens))
                  : '—'}</div>
              </div>
            </div>
          </aside>
        )}

        {/* transcript area */}
        <section className="flex flex-col min-w-0 min-h-0 bg-surface">
          {/* Friendly mode banner — rendered here so it has access to setShowRail */}
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
                <button
                  className="cw-btn cw-btn-ghost cw-btn-sm shrink-0"
                  onClick={() => setShowRail((v) => !v)}
                >
                  <span className="font-mono">{showRail ? '−' : '+'}</span> details
                </button>
              </div>
            </div>
          )}

          {/* delegated to the existing ChatWindow + useChat SSE pipeline */}
          <div
            ref={splitContainerRef}
            className="flex-1 min-h-0"
            style={
              previewFile
                ? { display: 'grid', gridTemplateColumns: `1fr 6px ${previewWidth}px`, minHeight: 0 }
                : { display: 'flex', flexDirection: 'column', minHeight: 0 }
            }
          >
            <ChatWindow sessionId={id} friendly={friendly} onPreviewFile={setPreviewFile} />
            {previewFile && (
              <>
                {/* Draggable divider */}
                <div
                  onMouseDown={startPreviewDrag}
                  role="separator"
                  aria-orientation="vertical"
                  aria-label="拖动调整预览栏宽度"
                  title="拖动调整预览栏宽度"
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
                <FilePreviewPanel
                  sessionId={id}
                  file={previewFile}
                  onClose={() => setPreviewFile(null)}
                />
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
