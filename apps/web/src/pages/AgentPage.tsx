import { useEffect, useRef, useState, useCallback } from 'react';
import { useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { ChatWindow } from '../components/ChatWindow';
import { useChat } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { useSessions } from '../hooks/useSessions';
import { sessionStatusZh } from '../lib/format';
import { FilePreviewPanel } from '../components/FilePreviewPanel';
import {
  ArticleCanvasPanel,
  loadStoredCanvasDraft,
} from '../components/ArticleCanvasPanel';
import {
  applyCanvasReplacement,
  createCanvasDraft,
  prepareCanvasOpenSeed,
  sanitizeCanvasDraft,
  type CanvasDocumentDraft,
  type CanvasEditRequest,
  type CanvasEditSuggestion,
  type CanvasOpenSeed,
} from '../lib/canvasObjects';
import { routeCanvasIntent } from '../lib/canvasIntentRouting';
import { api } from '../api/client';
import type { MessageAttachment } from '../api/types';
import { useFeatureFlag } from '../hooks/useFeatureFlags';
import { useCanvasStore } from '../stores/canvasStore';

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
  const { sendMessage, isStreaming, messages } = useChat();

  // canvas.v2 — when ON, Phase-A creation binds the card to a real server
  // canvas object (via canvasStore) instead of the in-memory draft. When OFF,
  // every existing draft-based code path below runs completely unchanged; all
  // new logic is gated behind `if (canvasV2)` / `canvasV2 ? … : …` branches.
  const canvasV2 = useFeatureFlag('canvas.v2');
  const createCanvasFromMessage = useCanvasStore((s) => s.createFromMessage);
  const createCanvasFromContent = useCanvasStore((s) => s.createFromContent);
  const requestCanvasEditAction = useCanvasStore((s) => s.requestEdit);

  const [previewFile, setPreviewFile] = useState<{ id: string; filename: string; mime?: string } | null>(null);
  const [canvasDraft, setCanvasDraft] = useState<CanvasDocumentDraft | null>(() =>
    id ? loadStoredCanvasDraft(id) : null,
  );
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [inlineCanvasOpen, setInlineCanvasOpen] = useState(false);
  const [inlineCanvasAnchorMessageId, setInlineCanvasAnchorMessageId] = useState<string | null>(null);
  const [canvasSuggestion, setCanvasSuggestion] = useState<CanvasEditSuggestion | null>(null);
  const [inlineCanvasV2Id, setInlineCanvasV2Id] = useState<string | null>(null);
  // canvas.v2 three-state routing: when an update-canvas send fails (409 / edit
  // error), we refill the composer so the user never loses their typed text.
  const [composerRefill, setComposerRefill] = useState<string | null>(null);
  // Silent canvas revision: request in flight + last error, both scoped to the
  // canvas card (never surfaced as chat messages).
  const [canvasRevising, setCanvasRevising] = useState(false);
  const [canvasEditError, setCanvasEditError] = useState<string | null>(null);
  // Resizable preview panel width (px). Persisted across opens within a session.
  const [previewWidth, setPreviewWidth] = useState(360);
  const PREVIEW_MIN = 280;
  const PREVIEW_MAX = 760;
  const splitContainerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const sanitizedCanvasDraftKeyRef = useRef<string | null>(null);

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
    setCanvasDraft(loadStoredCanvasDraft(id));
    setCanvasOpen(false);
    setInlineCanvasOpen(false);
    setInlineCanvasAnchorMessageId(null);
    setCanvasSuggestion(null);
    setCanvasRevising(false);
    setCanvasEditError(null);
    setPreviewFile(null);
    // canvas.v2 — clear the local anchor id on session switch to avoid
    // cross-session bleed. We don't reset the whole canvasStore here (other
    // panels/tabs may still reference cached canvases); clearing the local id
    // is enough since nothing renders without it.
    setInlineCanvasV2Id(null);
    setComposerRefill(null);
  }, [id]);

  useEffect(() => {
    if (!canvasDraft) {
      sanitizedCanvasDraftKeyRef.current = null;
      return;
    }
    const draftKey = [
      canvasDraft.id,
      canvasDraft.sourceMessageId ?? '',
      canvasDraft.title,
      canvasDraft.content,
    ].join('\u0000');
    if (sanitizedCanvasDraftKeyRef.current === draftKey) return;
    sanitizedCanvasDraftKeyRef.current = draftKey;
    const sanitized = sanitizeCanvasDraft(canvasDraft);
    if (sanitized !== canvasDraft) setCanvasDraft(sanitized);
  }, [canvasDraft]);

  const session = sessions.find((s) => s.id === id);

  const openPreviewFile = useCallback((file: { id: string; filename: string; mime?: string }) => {
    setPreviewFile(file);
    setCanvasOpen(false);
  }, []);

  const openBlankCanvas = useCallback(() => {
    setCanvasDraft((current) => current ?? createCanvasDraft('article'));
    setInlineCanvasOpen(true);
    setInlineCanvasAnchorMessageId(null);
    setCanvasOpen(false);
    setCanvasSuggestion(null);
    setPreviewFile(null);
  }, []);

  const openCanvasFromMessage = useCallback(
    (seed: CanvasOpenSeed) => {
      const sourceMessageId = seed.sourceMessageId;

      if (canvasV2) {
        // canvas.v2 ON — materialize a real server canvas object instead of an
        // in-memory draft. FAIL-OPEN: if creation rejects, warn and leave the
        // message bubble untouched (do not set an anchor). The card renders
        // additively below the anchor bubble — the bubble is never hidden.
        const materialize = sourceMessageId
          ? createCanvasFromMessage(id!, sourceMessageId, { kind: seed.kind, title: seed.title })
          : createCanvasFromContent(id!, seed.content, { kind: seed.kind, title: seed.title });
        materialize
          .then((canvas) => {
            setInlineCanvasV2Id(canvas.id);
            setInlineCanvasAnchorMessageId(sourceMessageId ?? null);
            setCanvasOpen(false);
            setPreviewFile(null);
          })
          .catch((err) => {
            console.warn('canvas.v2 materialize failed', err);
          });
        return;
      }

      const extracted = prepareCanvasOpenSeed(seed);
      setCanvasDraft(
        createCanvasDraft(extracted.kind, {
          title: extracted.title,
          content: extracted.content,
          sourceMessageId: extracted.sourceMessageId,
        }),
      );
      setInlineCanvasOpen(true);
      setInlineCanvasAnchorMessageId(sourceMessageId ?? null);
      setCanvasOpen(false);
      setCanvasSuggestion(null);
      setPreviewFile(null);
    },
    [canvasV2, createCanvasFromContent, createCanvasFromMessage, id],
  );

  const openInlineCanvasInPanel = useCallback(() => {
    if (!canvasDraft) return;
    setCanvasOpen(true);
    setInlineCanvasOpen(false);
    setPreviewFile(null);
  }, [canvasDraft]);

  const sendMessageWithCanvasDetection = useCallback(
    (text: string, attachmentIds?: string[], attachmentsMeta?: MessageAttachment[]) => {
      // canvas.v2 three-state routing (design §6.3 Phase A): when the flag is on
      // and the session has an active canvas, a *modify-intent* message routes to
      // the silent edit path — no chat message, no run. Attachments force the
      // normal chat path (an edit request carries no files). The composer has
      // already cleared its text (InputBar clears on send); on failure we refill
      // it so the user never loses input (fail-open).
      //
      // NOTE: send-time create-intent detection has been retired (design §7.1).
      // New-content creation is now driven by the model (Phase B): the worker's
      // `create_canvas` tool fires materialization and the SPA renders the card
      // off the assistant message's `canvas_id`. The composer no longer guesses.
      const hasAttachments = Boolean(attachmentIds && attachmentIds.length > 0);
      if (
        canvasV2 &&
        id &&
        inlineCanvasV2Id &&
        !hasAttachments &&
        routeCanvasIntent(text, true) === 'update_canvas'
      ) {
        const canvasId = inlineCanvasV2Id;
        requestCanvasEditAction(canvasId, id, {
          instruction: text,
          scope: { type: 'document' },
        })
          .then((suggestion) => {
            if (!suggestion) {
              // 409 / edit failure — the store recorded a localized error on the
              // card; refill the composer so nothing is lost.
              setComposerRefill(text);
            }
          })
          .catch(() => {
            setComposerRefill(text);
          });
        return Promise.resolve();
      }

      return sendMessage(text, attachmentIds, attachmentsMeta);
    },
    [canvasV2, id, inlineCanvasV2Id, requestCanvasEditAction, sendMessage],
  );

  // If HomePage routed us here with an `initialMessage` in state, dispatch it
  // once the chat store has bound to this session. Guard with a ref so React
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
    sendMessageWithCanvasDetection(initial ?? '', attachmentIds);
    // Clear router state so a refresh won't re-send.
    navigate(location.pathname, { replace: true, state: null });
  }, [id, activeSessionId, location.pathname, location.state, navigate, sendMessageWithCanvasDetection]);

  // Silent canvas revision. Unlike creation, this does NOT go through the chat
  // pipeline: no user prompt message, no agent run, nothing in the transcript.
  // We call the dedicated BFF endpoint directly and drop the returned
  // replacement straight into the existing suggestion / diff / accept flow.
  const requestCanvasEdit = useCallback(
    async (request: CanvasEditRequest) => {
      if (!id) return;
      setCanvasSuggestion(null);
      setCanvasEditError(null);
      setCanvasRevising(true);
      try {
        const model = useChatStore.getState().selectedModel[id] || undefined;
        const result = await api.sessions.canvasEdit(id, {
          title: request.title,
          content: request.content,
          scope: request.scope,
          selection:
            request.scope === 'selection' && request.selection
              ? {
                  text: request.selection.text,
                  start: request.selection.start,
                  end: request.selection.end,
                }
              : null,
          instruction: request.instruction,
          ...(model ? { model } : {}),
        });
        // The BFF may downgrade a selection-scoped request to a whole-document
        // replacement when it detects the model echoed the full document (or the
        // selection's surrounding context). In that case we must apply the
        // replacement as the entire document — splicing it at the selection would
        // duplicate the surrounding text. Override the suggestion's scope so both
        // the review card and applyCanvasReplacement take the whole-document path.
        const suggestionRequest =
          result.apply_to === 'document' && request.scope === 'selection'
            ? { ...request, scope: 'document' as const }
            : request;
        setCanvasSuggestion({
          request: suggestionRequest,
          replacement: result.replacement,
          summary: result.summary,
          receivedAt: new Date().toISOString(),
        });
        setInlineCanvasOpen(true);
        setCanvasOpen(false);
        setPreviewFile(null);
      } catch (err) {
        setCanvasEditError(
          err instanceof Error && err.message ? err.message : '修订失败，请稍后重试。',
        );
      } finally {
        setCanvasRevising(false);
      }
    },
    [id],
  );

  // Keep the manual "put in canvas" card anchored to the draft's source message
  // even as that message's id changes. On the home → task path the assistant
  // reply's id is swapped when its transient streaming block becomes the durable
  // DB row (and again on the run_complete re-pull). Without re-binding, the card
  // would strand on a stale anchor id and fall back to the bottom slot. Hiding
  // is gone (design §7.1) — the card renders additively below the bubble, so we
  // only re-pin the anchor and never touch a hidden set.
  useEffect(() => {
    const sourceId = canvasDraft?.sourceMessageId;
    if (!sourceId || !inlineCanvasOpen) return;
    if (!messages.some((m) => m.id === sourceId)) return;
    setInlineCanvasAnchorMessageId((current) => (current === sourceId ? current : sourceId));
  }, [canvasDraft?.sourceMessageId, inlineCanvasOpen, messages]);

  const acceptCanvasSuggestion = useCallback(() => {
    setCanvasDraft((current) => {
      if (!current || !canvasSuggestion || current.id !== canvasSuggestion.request.documentId) return current;
      return applyCanvasReplacement(current, canvasSuggestion.request, canvasSuggestion.replacement);
    });
    setCanvasSuggestion(null);
  }, [canvasSuggestion]);

  const rejectCanvasSuggestion = useCallback(() => {
    setCanvasSuggestion(null);
  }, []);

  const dismissCanvasEditError = useCallback(() => {
    setCanvasEditError(null);
  }, []);

  const hasSidePanel = previewFile !== null || (canvasOpen && canvasDraft !== null);

  if (!id) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={[{ label: '首页', to: '/home' }, { label: '会话' }]} />
        <div className="flex-1 flex items-center justify-center text-dim">未指定任务。</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: '首页', to: '/home' },
          { label: friendly ? 'Chat' : '会话', to: '/home' },
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
          <>
            <button
              onClick={openBlankCanvas}
              className="cw-btn cw-btn-ghost cw-btn-sm"
            >
              <span className="font-mono">◇</span> 画布
            </button>
            {!friendly && (
              <button
                onClick={() => navigate('/files')}
                className="cw-btn cw-btn-ghost cw-btn-sm"
              >
                <span className="font-mono">⌗</span> 文件
              </button>
            )}
          </>
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
              hasSidePanel
                ? { display: 'grid', gridTemplateColumns: `1fr 6px ${previewWidth}px`, minHeight: 0 }
                : { display: 'flex', flexDirection: 'column', minHeight: 0 }
            }
          >
            <ChatWindow
              sessionId={id}
              friendly={friendly}
              onPreviewFile={openPreviewFile}
              onOpenCanvas={openCanvasFromMessage}
              onSendMessage={sendMessageWithCanvasDetection}
              inlineCanvasDraft={inlineCanvasOpen ? canvasDraft : null}
              inlineCanvasRevising={canvasRevising}
              inlineCanvasEditError={canvasEditError}
              inlineCanvasSuggestion={canvasSuggestion}
              inlineCanvasAnchorMessageId={inlineCanvasAnchorMessageId}
              inlineCanvasV2Id={canvasV2 ? inlineCanvasV2Id : null}
              externalPrefill={composerRefill}
              onExternalPrefillConsumed={() => setComposerRefill(null)}
              onInlineCanvasDraftChange={setCanvasDraft}
              onInlineCanvasRequestEdit={requestCanvasEdit}
              onInlineCanvasDismissEditError={dismissCanvasEditError}
              onInlineCanvasOpenPanel={canvasDraft ? openInlineCanvasInPanel : undefined}
              onInlineCanvasAcceptSuggestion={canvasSuggestion ? acceptCanvasSuggestion : undefined}
              onInlineCanvasRejectSuggestion={canvasSuggestion ? rejectCanvasSuggestion : undefined}
            />
            {hasSidePanel && (
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
                {previewFile ? (
                  <FilePreviewPanel
                    sessionId={id}
                    file={previewFile}
                    onClose={() => setPreviewFile(null)}
                  />
                ) : canvasDraft ? (
                  <ArticleCanvasPanel
                    sessionId={id}
                    draft={canvasDraft}
                    aiDisabled={isStreaming}
                    revising={canvasRevising}
                    editError={canvasEditError}
                    onDraftChange={setCanvasDraft}
                    onClose={() => setCanvasOpen(false)}
                    onRequestEdit={requestCanvasEdit}
                    onDismissEditError={dismissCanvasEditError}
                  />
                ) : null}
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
