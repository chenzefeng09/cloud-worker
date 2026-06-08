import { useEffect, useRef, useState, useCallback } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusZh } from '../lib/format';
import { FilePreviewPanel } from '../components/FilePreviewPanel';

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

  const { sessions } = useSessions();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const setActiveSession = useChatStore((s) => s.setActiveSession);
  const { sendMessage, cancel, isStreaming } = useChat();

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
          friendly ? null : (
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
