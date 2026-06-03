import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { Chip } from '../components/shell/Chip';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusZh } from '../lib/format';
import { useWorkspace } from '../lib/workspaceContext';
import {
  SHOW_DESIGN_FIXTURES,
  PLAN_STEPS,
  AGENT_SKILLS,
  AGENT_WORKER,
  AGENT_LIVE_STATS,
} from '../lib/designFixtures';

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

  const { ws, files: wsFiles } = useWorkspace();
  const { sessions } = useSessions();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const setActiveSession = useChatStore((s) => s.setActiveSession);
  const { sendMessage, cancel, isStreaming } = useChat();

  // Friendly defaults rail hidden; non-friendly defaults rail shown.
  const [showRail, setShowRail] = useState(!friendly);

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
    const initial = (location.state as { initialMessage?: string } | null)?.initialMessage?.trim();
    if (!initial) return;
    if (sentInitialRef.current === id) return;
    sentInitialRef.current = id;
    sendMessage(initial, []);
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
            {/* ── Workspace ── */}
            <div>
              <div className="cw-label mb-1.5">工作区</div>
              <div
                onClick={() => navigate('/workspace')}
                className="flex items-center gap-2 px-2 py-1.5 bg-surface rounded-sm cursor-pointer"
                style={{
                  border: `1px solid ${ws.accent}33`,
                  borderLeft: `2px solid ${ws.accent}`,
                  borderRadius: 'var(--r-sm)',
                }}
              >
                <span
                  className="cw-ws-chip sm shrink-0"
                  style={{
                    background: ws.accent + '14',
                    borderColor: ws.accent + '55',
                    color: ws.accent,
                  }}
                >
                  {ws.glyph}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[12.5px] font-semibold tracking-[-0.01em] truncate">
                    {ws.name}
                  </div>
                  <div className="font-mono text-[10px] text-dim">
                    {wsFiles.length} base files
                  </div>
                </div>
              </div>
              {/* dotted vertical base-files list */}
              {wsFiles.slice(0, 4).length > 0 && (
                <div className="flex flex-col gap-0.5 mt-1.5 pl-1">
                  {wsFiles.slice(0, 4).map((f) => (
                    <div
                      key={f.n}
                      className="flex items-center gap-1.5 px-1 font-mono text-[10.5px] text-ink-2 min-w-0"
                    >
                      <span style={{ color: ws.accent, fontSize: 9 }}>●</span>
                      <span className="whitespace-nowrap overflow-hidden text-ellipsis flex-1">
                        {f.n}
                      </span>
                    </div>
                  ))}
                  {wsFiles.length === 0 && (
                    <span className="font-mono text-[11px] italic text-dim">无基础文件</span>
                  )}
                </div>
              )}
            </div>

            {/* ── Worker card (fixture) ── */}
            {SHOW_DESIGN_FIXTURES && (
              <div>
                <div className="cw-label mb-1.5">Worker</div>
                <div
                  className="flex items-center gap-2 px-2 py-1.5 bg-surface border border-line"
                  style={{ borderRadius: 'var(--r-sm)' }}
                >
                  <span className="cw-avatar accent lg shrink-0">{AGENT_WORKER.glyph}</span>
                  <div className="min-w-0 flex-1">
                    <div className="font-mono text-[12px] font-semibold">{AGENT_WORKER.id}</div>
                    <div className="font-mono text-[10px] text-dim">
                      since {AGENT_WORKER.ago} ago
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ── Skills & uploads (fixture) ── */}
            {SHOW_DESIGN_FIXTURES && (
              <div>
                <div className="cw-label mb-1.5">
                  Skills &amp; uploads · {AGENT_SKILLS.length}
                </div>
                <div className="flex flex-col gap-1">
                  {AGENT_SKILLS.map((s) => (
                    <Chip key={s.label} dashed={s.dashed}>
                      {s.label}
                    </Chip>
                  ))}
                </div>
              </div>
            )}

            {/* ── Plan (fixture) ── */}
            {SHOW_DESIGN_FIXTURES && (
              <div>
                <div className="cw-label mb-1.5">
                  Plan · {PLAN_STEPS.length} steps
                </div>
                <div className="flex flex-col gap-0.5">
                  {PLAN_STEPS.map((s) => (
                    <div
                      key={s.n}
                      className="flex items-center gap-2 px-1.5 py-1"
                      style={{
                        borderRadius: 4,
                        background: s.live ? 'var(--accent-soft)' : 'transparent',
                      }}
                    >
                      <span
                        className="inline-flex items-center justify-center shrink-0 font-mono"
                        style={{
                          width: 14,
                          height: 14,
                          borderRadius: 3,
                          border: `1px solid ${s.d ? 'var(--ink)' : s.live ? 'var(--accent)' : 'var(--line)'}`,
                          background: s.d ? 'var(--ink)' : 'transparent',
                          color: 'var(--paper)',
                          fontSize: 9,
                        }}
                      >
                        {s.d ? '✓' : ''}
                      </span>
                      <span
                        className="text-[12px]"
                        style={{
                          color: s.live ? 'var(--accent)' : s.d ? 'var(--dim)' : 'var(--ink)',
                          textDecoration: s.d ? 'line-through' : 'none',
                          fontWeight: s.live ? 600 : 400,
                        }}
                      >
                        {s.t}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            <div className="flex-1" />

            {/* ── Live stats (fixture + real streaming blip) ── */}
            <div>
              <div className="cw-label mb-1.5">Live</div>
              {isStreaming && (
                <div className="flex items-center gap-1.5 mb-1.5 px-2 py-1 border border-dashed border-accent-line bg-accent-soft rounded-sm">
                  <span className="w-1.5 h-1.5 rounded-full bg-accent animate-blip shrink-0" />
                  <span className="font-mono text-[10.5px] text-accent">运行中…</span>
                </div>
              )}
              {SHOW_DESIGN_FIXTURES && (
                <div className="font-mono text-[10.5px] text-ink-2 leading-[1.6]">
                  <div>elapsed · {AGENT_LIVE_STATS.elapsed}</div>
                  <div>tokens · {AGENT_LIVE_STATS.tokens}</div>
                  <div>cost · {AGENT_LIVE_STATS.cost}</div>
                </div>
              )}
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
          <ChatWindow sessionId={id} friendly={friendly} />
        </section>
      </div>
    </div>
  );
}
