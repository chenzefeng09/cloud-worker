import { useCallback, useEffect, useMemo, useRef, useState, type WheelEvent } from 'react';
import { useChat } from '../hooks/useChat';
import { useOutputAttachments } from '../hooks/useOutputAttachments';
import { useInstalledSkills } from '../hooks/useInstalledSkills';
import { useMcpServers } from '../hooks/useMcpServers';
import { useChatStore, hasPendingInteraction } from '../stores/chatStore';
import { MessageBubble, WorkingOnItPill } from './MessageBubble';
import { StepGroup } from './StepGroup';
import { OutputFileCards } from './OutputFileCards';
import { Composer } from './composer/Composer';
import { PlanBlock } from './PlanBlock';
import { RewindConfirmModal } from './RewindConfirmModal';
import { DataNoticeModal } from './DataNoticeModal';
import { ConfirmCard } from './hitl/ConfirmCard';
import { QuestionCard } from './hitl/QuestionCard';
import { CanvasMessageCard } from './CanvasMessageCard';
import {
  MarkdownAssetsProvider,
  makeAttachmentImageResolver,
  type MarkdownAssets,
} from './markdownAssets';
import { api } from '../api/client';
import type { Attachment, MessageAttachment } from '../api/types';
import type { MentionCandidate } from '../lib/mentionTrigger';
import { groupSteps } from '../lib/groupSteps';
import { DEFAULT_EFFORT, SYSTEM_FALLBACK_MODEL } from '../lib/modelOptions';
import { useDefaultModel } from '../hooks/useDefaultModel';
import { useFeatureFlag } from '../hooks/useFeatureFlags';
import {
  SHOW_DESIGN_FIXTURES,
  AGENT_TRANSCRIPT_FIXTURE,
} from '../lib/designFixtures';
import type { TranscriptMessage } from '../lib/designFixtures';
import type { PendingInteraction } from '../api/types';
import type {
  CanvasDocumentDraft,
  CanvasEditRequest,
  CanvasEditSuggestion,
} from '../lib/canvasObjects';
import { hasVisibleCanvasAnchor } from '../lib/canvasAnchoring';
import {
  isUserScrollingAwayFromBottom,
  resolveTranscriptScrollState,
} from '../lib/chatScroll';
import { useCanvasStore } from '../stores/canvasStore';

// Stable empty list for the pending-interactions selector — a fresh `[]` per
// getSnapshot call would re-render forever (Zustand caches by reference).
const EMPTY_INTERACTIONS: PendingInteraction[] = [];

// ── Design-only fixture renderer ──────────────────────────────────────────────
// Renders AGENT_TRANSCRIPT_FIXTURE when there are no real messages yet.
// DESIGN FIXTURE — delete this component (and its usage below) once the backend
// provides real transcript data.
function FixtureMessage({
  m,
  friendly,
}: {
  m: TranscriptMessage;
  friendly?: boolean;
}) {
  return <FixtureMessageInner m={m} friendly={friendly} />;
}

function FixtureMessageInner({
  m,
  friendly,
}: {
  m: TranscriptMessage;
  friendly?: boolean;
}) {
  const [open, setOpen] = useState(() => {
    // thoughts + tool groups: collapsed by default in friendly mode
    if (m.from === 'agent' && (m.kind === 'thought' || m.kind === 'tools')) {
      return !friendly;
    }
    return true;
  });
  const [toolExpanded, setToolExpanded] = useState<Record<string, boolean>>({});

  if (m.from === 'user') {
    return (
      <div className="flex justify-end">
        <div
          className="max-w-[80%] text-[14px] leading-[1.55] tracking-[-0.005em]"
          style={{
            background: 'var(--paper-2)',
            padding: '10px 14px',
            borderRadius: 'var(--r-md)',
            border: '1px solid var(--line-2)',
          }}
        >
          {m.text}
        </div>
      </div>
    );
  }

  if (m.kind === 'thought') {
    return (
      <div className="min-w-0">
          <div
            onClick={() => setOpen((v) => !v)}
            className="flex items-center gap-2 cursor-pointer text-ink-2"
            style={{
              padding: '6px 10px',
              border: '1px dashed var(--line)',
              borderRadius: 'var(--r-sm)',
              background: open ? 'var(--surface)' : 'transparent',
            }}
          >
            <span className="font-mono text-[10.5px] text-dim">{open ? '▾' : '▸'}</span>
            <span className="cw-label">{friendly ? 'thinking' : 'Thought'}</span>
            <span className="text-[13px]">{m.title}</span>
          </div>
          {open && (
            <div
              className="flex flex-col gap-1.5 mt-1.5 ml-1.5 pl-3.5 py-1"
              style={{ borderLeft: '2px solid var(--accent-line)' }}
            >
              {m.body.map((p, i) => (
                <div key={i} className="flex gap-2 items-start">
                  <span className="font-mono text-dim text-[11px] mt-0.5 min-w-[16px]">{i + 1}.</span>
                  <span className="text-[13px] text-ink-2 leading-[1.5]">{p}</span>
                </div>
              ))}
            </div>
          )}
      </div>
    );
  }

  if (m.kind === 'tools') {
    const itemCount = m.items.length;
    return (
      <div className="min-w-0">
          <div
            onClick={() => setOpen((v) => !v)}
            className="inline-flex items-center gap-2 cursor-pointer text-dim"
            style={
              friendly
                ? {
                    padding: '6px 10px',
                    border: '1px solid var(--line-2)',
                    borderRadius: 'var(--r-sm)',
                    background: 'var(--paper)',
                  }
                : { padding: '4px 8px' }
            }
          >
            <span className="font-mono text-[10.5px]">{open ? '▾' : '▸'}</span>
            <span className="cw-label">
              {friendly
                ? `worked through ${itemCount} step${itemCount > 1 ? 's' : ''}`
                : `${itemCount} tool call${itemCount > 1 ? 's' : ''}`}
            </span>
            {friendly && !open && (
              <span className="font-mono text-[10.5px] text-dim">
                — {m.items.slice(0, 2).map((it) => it.name).join(', ')}
                {m.items.length > 2 ? '…' : ''}
              </span>
            )}
          </div>
          {open && (
            <div className="flex flex-col gap-1 mt-1">
              {m.items.map((it, i) => {
                const key = `${m.id}:${i}`;
                const exp = toolExpanded[key];
                return (
                  <div key={i}>
                    <div
                      onClick={() =>
                        setToolExpanded((prev) => ({ ...prev, [key]: !exp }))
                      }
                      className="flex items-center gap-2 cursor-pointer font-mono text-[12px]"
                      style={{
                        padding: '6px 10px',
                        border: '1px solid var(--line)',
                        borderRadius: 'var(--r-sm)',
                        background: exp ? 'var(--surface)' : 'var(--paper)',
                      }}
                    >
                      <span className="text-dim text-[10.5px]">{exp ? '▾' : '▸'}</span>
                      <span
                        style={{
                          fontWeight: 600,
                          color:
                            it.name === 'edit' || it.name === 'write'
                              ? 'var(--accent)'
                              : 'var(--ink)',
                        }}
                      >
                        {it.name}
                      </span>
                      <span className="text-ink-2">{it.target}</span>
                      <div className="flex-1" />
                      <span className="text-dim text-[10.5px]">{it.lines}</span>
                    </div>
                    {exp && (
                      <div
                        className="mt-1 ml-4 font-mono text-[11.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap"
                        style={{
                          padding: '10px 12px',
                          background: 'var(--paper-2)',
                          border: '1px solid var(--line-2)',
                          borderRadius: 'var(--r-sm)',
                        }}
                      >
                        {it.name === 'read' &&
                          `// ${it.target}\nexport async function validateSession(token: string) {\n  const session = await db.session.findUnique({ where: { token } });\n  ⋯\n}`}
                        {it.name === 'search' &&
                          `9 matches in 4 files\n  src/auth/session.ts:23\n  src/auth/login.tsx:8\n  ⋯`}
                        {it.name === 'edit' &&
                          `// ${it.target}\n−  if (await bcrypt.compare(password, user.passwordHash)) {\n+  const credential = await db.credential.findUnique({\n+    where: { userId, id: credentialId }\n+  });`}
                        {it.name === 'write' &&
                          `// new file: ${it.target}\nimport { generateRegistrationOptions } from "@simplewebauthn/server";\n⋯ 48 lines`}
                        {it.name === 'run' &&
                          `$ ${it.target}\n  Generated Prisma Client (5.18.0)\n  ✓ done in 1.4s`}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
      </div>
    );
  }

  if (m.kind === 'text') {
    return (
      <div className="min-w-0 text-[14px] leading-[1.6] text-ink" style={{ paddingTop: 3 }}>
        {m.text.split(/(`[^`]+`)/).map((part, i) =>
          part.startsWith('`') ? (
            <span
              key={i}
              className="font-mono text-[12.5px] px-1.5 py-0.5 bg-paper-2 border border-line-2 rounded"
            >
              {part.slice(1, -1)}
            </span>
          ) : (
            <span key={i}>{part}</span>
          )
        )}
      </div>
    );
  }

  if (m.kind === 'live') {
    return (
      <div className="min-w-0">
          <div
            className="flex items-start gap-2.5"
            style={{
              padding: '10px 12px',
              border: '1.5px solid var(--accent-line)',
              background: 'var(--accent-soft)',
              borderRadius: 'var(--r-sm)',
            }}
          >
            <span className="w-1.5 h-1.5 rounded-full bg-accent animate-blip mt-1.5 shrink-0" />
            <div className="flex-1">
              <div
                className={friendly ? 'text-[13.5px] font-semibold tracking-[-0.005em]' : 'font-mono text-[12px] font-semibold'}
                style={{ color: 'var(--accent)' }}
              >
                {friendly ? 'Working on it…' : m.label}
              </div>
              {!friendly && m.body && (
                <div
                  className="font-mono text-[11.5px] text-ink-2 mt-1 leading-[1.55] whitespace-pre-wrap"
                >
                  {m.body}
                  <span
                    className="inline-block bg-accent ml-0.5 align-[-2px]"
                    style={{
                      width: 6,
                      height: 12,
                      animation: 'blip 1s steps(2) infinite',
                    }}
                  />
                </div>
              )}
            </div>
          </div>
      </div>
    );
  }

  return null;
}


// ── ChatWindow ────────────────────────────────────────────────────────────────

// localStorage key recording that the user has acknowledged the first-use data
// notice. Versioned so the notice can be re-shown if the policy text changes.
const DATA_NOTICE_ACK_KEY = 'data-notice-ack-v1';

export function ChatWindow({
  sessionId,
  workspaceId,
  friendly,
  onPreviewFile,
  onSendMessage,
  inlineCanvasDraft,
  inlineCanvasPending,
  inlineCanvasRevising,
  inlineCanvasEditError,
  inlineCanvasSuggestion,
  inlineCanvasAnchorMessageId,
  inlineCanvasV2Id,
  onInlineCanvasDraftChange,
  onInlineCanvasRequestEdit,
  onInlineCanvasDismissEditError,
  onInlineCanvasOpenPanel,
  onInlineCanvasAcceptSuggestion,
  onInlineCanvasRejectSuggestion,
  externalPrefill,
  onExternalPrefillConsumed,
}: {
  sessionId: string;
  /** Workspace bound to this session — sources the "@" mention workspace files. */
  workspaceId?: string;
  friendly?: boolean;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
  onSendMessage?: (text: string, attachmentIds?: string[], attachmentsMeta?: MessageAttachment[], workspaceFileIds?: string[]) => void | Promise<void>;
  inlineCanvasDraft?: CanvasDocumentDraft | null;
  inlineCanvasPending?: boolean;
  inlineCanvasRevising?: boolean;
  inlineCanvasEditError?: string | null;
  inlineCanvasSuggestion?: CanvasEditSuggestion | null;
  inlineCanvasAnchorMessageId?: string | null;
  /** canvas.v2 ON: id of a server canvas object to render inline instead of the draft-mode card. */
  inlineCanvasV2Id?: string | null;
  onInlineCanvasDraftChange?: (draft: CanvasDocumentDraft) => void;
  onInlineCanvasRequestEdit?: (request: CanvasEditRequest) => void | Promise<void>;
  onInlineCanvasDismissEditError?: () => void;
  onInlineCanvasOpenPanel?: () => void;
  onInlineCanvasAcceptSuggestion?: () => void;
  onInlineCanvasRejectSuggestion?: () => void;
  /** canvas.v2: text to push back into the composer after a failed silent edit
   *  (fail-open — the user's input is never lost). */
  externalPrefill?: string | null;
  onExternalPrefillConsumed?: () => void;
}) {
  const {
    messages,
    isStreaming,
    sendMessage,
    cancel,
    prefillText,
    clearPrefill,
    rewindTo,
    pendingRewind,
    confirmRewind,
    cancelRewind,
    rewindBusy,
    rewindError,
  } = useChat();
  const { byRunId, refresh: refreshOutputs } = useOutputAttachments(sessionId);

  // Resolve sandbox-relative markdown image paths in assistant replies against
  // this session's output attachments — reusing the list `OutputFileCards`
  // already fetched, so no extra request. Scoped to the whole session rather
  // than one run: the markdown renderer has no run context, and filenames are
  // what the model wrote anyway.
  // `byRunId` is rebuilt every render, so the memo keys off the flattened id
  // list; returning a fresh context value each render would re-render every
  // markdown tree in the transcript.
  const outputAttachments = useMemo(
    () => Array.from(byRunId.values()).flat(),
    [byRunId],
  );
  const outputAttachmentsKey = outputAttachments.map((a) => a.id).join(',');
  const markdownAssets = useMemo<MarkdownAssets>(
    () => ({
      resolveImage: makeAttachmentImageResolver(outputAttachments, sessionId),
      onOpen: (t) =>
        t.id ? onPreviewFile?.({ id: t.id, filename: t.filename, mime: t.mime }) : undefined,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- outputAttachmentsKey is the stable identity of outputAttachments
    [outputAttachmentsKey, sessionId, onPreviewFile],
  );

  // "@ file" mention candidates: workspace base files (labelled Workspace) + this
  // session's input attachments (labelled Attachment). Best-effort — any fetch that
  // fails degrades silently to whatever loaded. Refreshed on a run-completion
  // edge so files uploaded/added earlier in the session surface without a page
  // reload.
  const [mentionCandidates, setMentionCandidates] = useState<MentionCandidate[]>([]);
  const loadMentionCandidates = useCallback(async () => {
    const [wsFiles, inputAtts] = await Promise.all([
      workspaceId
        ? api.workspaces.listFiles(workspaceId).catch(() => [])
        : Promise.resolve([]),
      api.attachments
        .list(sessionId, 'input')
        .then((r) => (Array.isArray(r?.items) ? r.items : []))
        .catch(() => []),
    ]);
    setMentionCandidates([
      ...wsFiles.map((f) => ({
        id: f.id,
        filename: f.filename,
        kind: 'workspace' as const,
        mime: f.mime,
      })),
      ...inputAtts.map((a) => ({
        id: a.id,
        filename: a.filename,
        kind: 'attachment' as const,
        mime: a.mime,
      })),
    ]);
  }, [sessionId, workspaceId]);
  useEffect(() => {
    void loadMentionCandidates();
  }, [loadMentionCandidates]);

  const scrollRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const shouldStickToBottomRef = useRef(true);
  const userDetachedFromBottomRef = useRef(false);
  const previousScrollTopRef = useRef(0);
  // Track previous isStreaming value so we can detect the true→false edge.
  const prevStreamingRef = useRef(isStreaming);
  const [isAwayFromBottom, setIsAwayFromBottom] = useState(false);

  // Per-session skill toggles, surfaced in the composer "+" menu.
  const skills = useInstalledSkills();
  const selectedSkills = useChatStore((s) => s.selectedSkills[sessionId]);
  const toggleSkill = useChatStore((s) => s.toggleSkill);
  const seedSkills = useChatStore((s) => s.seedSkills);

  // Per-session MCP server toggles, same "+" menu. `selectedMcpServerIds` is
  // intentionally left `undefined` until the user actually toggles something —
  // see the onToggle comment at the <Composer> call site.
  const { servers: mcpServers } = useMcpServers();
  const selectedMcpServerIds = useChatStore((s) => s.selectedMcpServerIds[sessionId]);
  const toggleMcpServer = useChatStore((s) => s.toggleMcpServer);
  const seedMcpServerIds = useChatStore((s) => s.seedMcpServerIds);

  // Per-session model + reasoning effort, surfaced in the composer toolbar.
  const showThinkingUi = useFeatureFlag('ai.thinking_ui');
  // canvas.v2 Phase B: gate the message-anchored canvas card (rendered under an
  // assistant bubble whose row carries a materialized canvas_id).
  const canvasV2 = useFeatureFlag('canvas.v2');
  const selectedModel = useChatStore((s) => s.selectedModel[sessionId]);
  const selectedEffort = useChatStore((s) => s.selectedEffort[sessionId]);
  const setSessionModel = useChatStore((s) => s.setSessionModel);
  const setSessionEffort = useChatStore((s) => s.setSessionEffort);
  // Per-session chat/agent mode. Atomic selector (no new object) so the snapshot
  // stays reference-stable; defaults applied at render, not in the selector.
  // Per-session opt-out of the platform-global default skills (default on).
  const platformDefaultsEnabled =
    useChatStore((s) => s.platformDefaultsEnabled[sessionId]) !== false;
  const setPlatformDefaultsEnabled = useChatStore((s) => s.setPlatformDefaultsEnabled);
  const { modelId: defaultModelId } = useDefaultModel('llm');

  // Live plan steps for this session — rendered at the bottom of the transcript.
  const planSteps = useChatStore((s) => s.planSteps[sessionId]);

  // HITL — pending interactions (confirm / question cards).
  // `?? EMPTY_INTERACTIONS` (module-level constant) instead of `|| []`: a fresh
  // array per call makes getSnapshot unstable → infinite re-render loop.
  const pendingInteractions = useChatStore(
    (s) => s.pendingInteractions[sessionId] ?? EMPTY_INTERACTIONS,
  );
  // Derived: true when at least one pending interaction exists.
  // Used to disable the composer while HITL cards are shown — prevents the user
  // from forking a paused run with a new message before resolving it.
  const hasPending = useChatStore((s) => hasPendingInteraction(s, sessionId));

  // First-use data-usage notice: show once per device, then remember the
  // acknowledgement in localStorage so it never appears again.
  const [showDataNotice, setShowDataNotice] = useState(false);
  // canvas.v2: read the server canvas so the anchor gate can verify it's loaded
  // before rendering the card at its anchor. Selector returns a stable
  // reference (or undefined) — never a fresh object.
  const v2Canvas = useCanvasStore((s) => (inlineCanvasV2Id ? s.canvasesById[inlineCanvasV2Id] : undefined));
  const canRenderInlineCanvas = Boolean(
    (inlineCanvasDraft && onInlineCanvasDraftChange && onInlineCanvasRequestEdit) ||
      (inlineCanvasV2Id && v2Canvas),
  );
  // Message hiding has been retired (design §7.1): the "put in canvas" action no
  // longer removes the source bubble. The card and the bubble coexist — content
  // is always reachable in the transcript. We still ask whether the inline
  // canvas is anchored to an on-screen message so the card can render at that
  // anchor (additively, below the bubble) rather than in the bottom fallback.
  const anchoredInlineCanvasVisible = Boolean(
    hasVisibleCanvasAnchor(messages, canRenderInlineCanvas, inlineCanvasAnchorMessageId),
  );

  // canvas.v2 Phase B — warm the canvas cache for message-anchored cards.
  // The live SSE `canvas_created` handler pre-fetches, but on HYDRATION (page
  // refresh / session switch) the message rows arrive from the DB carrying
  // canvas_id with an empty canvas store, and CanvasMessageCard(canvasId) reads
  // the store without self-fetching. This effect fills that gap: for every
  // assistant message that carries a canvas_id not yet cached, fire a
  // best-effort fetchCanvas. Reads canvasesById via getState() (not a selector)
  // so it never churns the render, and fetchCanvas is idempotent + fail-open.
  const fetchCanvas = useCanvasStore((s) => s.fetchCanvas);
  const anchoredCanvasIdsKey = useMemo(
    () =>
      messages
        .filter((m) => m.canvas_id && (m.role || '').toUpperCase() === 'ASSISTANT')
        .map((m) => m.canvas_id)
        .join(','),
    [messages],
  );
  useEffect(() => {
    if (!canvasV2 || !anchoredCanvasIdsKey) return;
    const cached = useCanvasStore.getState().canvasesById;
    const ids = new Set(anchoredCanvasIdsKey.split(',').filter(Boolean));
    for (const cid of ids) {
      if (!cached[cid]) void fetchCanvas(cid).catch(() => {});
    }
  }, [canvasV2, anchoredCanvasIdsKey, fetchCanvas]);

  useEffect(() => {
    try {
      if (!localStorage.getItem(DATA_NOTICE_ACK_KEY)) {
        setShowDataNotice(true);
      }
    } catch {
      // localStorage unavailable (e.g. privacy mode) — skip the notice.
    }
  }, []);
  const ackDataNotice = () => {
    try {
      localStorage.setItem(DATA_NOTICE_ACK_KEY, '1');
    } catch {
      // Ignore storage failures; just close the modal.
    }
    setShowDataNotice(false);
  };

  // Seed this session's enabled skills from the user's install defaults, once
  // the installed list loads — only if the user hasn't chosen yet this session.
  useEffect(() => {
    if (skills.length === 0) return;
    seedSkills(sessionId, skills.filter((s) => s.enabled_by_default).map((s) => s.name));
  }, [sessionId, skills, seedSkills]);

  const updateScrollStickiness = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const next = resolveTranscriptScrollState({
      scrollTop: el.scrollTop,
      scrollHeight: el.scrollHeight,
      clientHeight: el.clientHeight,
      previousScrollTop: previousScrollTopRef.current,
      wasSticking: shouldStickToBottomRef.current,
      wasDetached: userDetachedFromBottomRef.current,
    });
    previousScrollTopRef.current = next.nextPreviousScrollTop;
    shouldStickToBottomRef.current = next.shouldStickToBottom;
    userDetachedFromBottomRef.current = next.isAwayFromBottom;
    setIsAwayFromBottom((current) =>
      current === next.isAwayFromBottom ? current : next.isAwayFromBottom,
    );
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
    shouldStickToBottomRef.current = true;
    userDetachedFromBottomRef.current = false;
    setIsAwayFromBottom(false);
    requestAnimationFrame(() => {
      bottomRef.current?.scrollIntoView({ behavior, block: 'end' });
    });
  }, []);

  useEffect(() => {
    if (!shouldStickToBottomRef.current) {
      setIsAwayFromBottom(true);
      return;
    }
    scrollToBottom(isStreaming ? 'auto' : 'smooth');
  }, [messages, isStreaming, scrollToBottom]);

  // Refresh output attachments when streaming transitions true → false
  // (i.e. a run just completed). This surfaces newly written files quickly.
  useEffect(() => {
    if (prevStreamingRef.current && !isStreaming) {
      refreshOutputs();
      void loadMentionCandidates();
    }
    prevStreamingRef.current = isStreaming;
  }, [isStreaming, refreshOutputs, loadMentionCandidates]);

  const handleUpload = async (file: File): Promise<Attachment> => {
    return api.attachments.upload(sessionId, file);
  };

  const handleSend = (
    text: string,
    attachmentIds?: string[],
    attachments?: Attachment[],
    workspaceFileIds?: string[],
  ) => {
    const attMeta: MessageAttachment[] | undefined = attachments?.map((a) => ({
      id: a.id,
      filename: a.filename,
      mime: a.mime,
      size: a.size_bytes,
    }));
    shouldStickToBottomRef.current = true;
    scrollToBottom('smooth');
    if (onSendMessage) {
      return onSendMessage(text, attachmentIds, attMeta, workspaceFileIds);
    }
    return sendMessage(text, attachmentIds, attMeta, workspaceFileIds);
  };

  const handleTranscriptWheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    if (!isUserScrollingAwayFromBottom(event.deltaY)) return;
    userDetachedFromBottomRef.current = true;
    shouldStickToBottomRef.current = false;
    setIsAwayFromBottom(true);
  }, []);

  // Show fixture fallback only when no real messages exist, not streaming,
  // and the design-fixture flag is on. DESIGN FIXTURE — remove the fixture
  // block when backend provides real data.
  const showFixture =
    SHOW_DESIGN_FIXTURES &&
    messages.length === 0 &&
    !isStreaming &&
    !inlineCanvasDraft &&
    !inlineCanvasV2Id;

  const renderInlineCanvasCard = () => {
    if (inlineCanvasV2Id) {
      return (
        <CanvasMessageCard
          sessionId={sessionId}
          canvasId={inlineCanvasV2Id}
          aiDisabled={isStreaming}
          onOpenPanel={onInlineCanvasOpenPanel}
        />
      );
    }
    if (!canRenderInlineCanvas || !inlineCanvasDraft || !onInlineCanvasDraftChange || !onInlineCanvasRequestEdit) {
      return null;
    }
    return (
      <CanvasMessageCard
        sessionId={sessionId}
        draft={inlineCanvasDraft}
        pending={inlineCanvasPending}
        aiDisabled={isStreaming}
        revising={inlineCanvasRevising}
        editError={inlineCanvasEditError}
        onDraftChange={onInlineCanvasDraftChange}
        onRequestEdit={onInlineCanvasRequestEdit}
        onDismissEditError={onInlineCanvasDismissEditError}
        onOpenPanel={onInlineCanvasOpenPanel}
        suggestion={inlineCanvasSuggestion}
        onAcceptSuggestion={onInlineCanvasAcceptSuggestion}
        onRejectSuggestion={onInlineCanvasRejectSuggestion}
      />
    );
  };

  return (
    <MarkdownAssetsProvider value={markdownAssets}>
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <div
          ref={scrollRef}
          onScroll={updateScrollStickiness}
          onWheel={handleTranscriptWheel}
          className="cw-scroll flex min-w-0 flex-1 flex-col gap-[8px] px-8 py-5"
        >
          <div className="min-w-0" style={{ maxWidth: 780, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 8 }}>
            {/* ── Real messages ── */}
            {(() => {
              const units = groupSteps(messages, isStreaming);

              // Pre-compute: for each run_id that has output files, find the
              // index of the LAST assistant message unit with that run_id.
              // We only attach file cards to that final unit to avoid duplication.
              const lastAssistantUnitIdx = new Map<string, number>();
              units.forEach((u, idx) => {
                if (u.kind === 'message') {
                  const role = (u.message.role || '').toUpperCase();
                  const runId = u.message.run_id;
                  if (role === 'ASSISTANT' && runId && byRunId.has(runId)) {
                    lastAssistantUnitIdx.set(runId, idx);
                  }
                }
              });

              // Per-run copy support: for each run, find the LAST assistant TEXT
              // message unit (so the copy button appears only there), and build
              // the concatenated text of ALL assistant messages in that round.
              const isAssistantText = (u: typeof units[number]) =>
                u.kind === 'message' &&
                (u.message.role || '').toUpperCase() === 'ASSISTANT' &&
                u.message.blockKind !== 'tool' &&
                u.message.blockKind !== 'thinking';
              const roundKeyOf = (m: { run_id: string | null; streamRunId?: string }) =>
                m.run_id ?? m.streamRunId ?? null;

              const lastAssistantTextUnitIdx = new Map<string, number>();
              const roundCopyText = new Map<string, string>();
              units.forEach((u, idx) => {
                if (!isAssistantText(u) || u.kind !== 'message') return;
                const key = roundKeyOf(u.message);
                if (!key) return;
                lastAssistantTextUnitIdx.set(key, idx);
                const prev = roundCopyText.get(key);
                roundCopyText.set(
                  key,
                  prev ? `${prev}\n\n${u.message.content}` : u.message.content,
                );
              });

              return units.map((u, idx) => {
                if (u.kind === 'message') {
                  const role = (u.message.role || '').toUpperCase();
                  const runId = u.message.run_id;
                  // Manual "put in canvas": the card renders ADDITIVELY below its
                  // anchor bubble (design §7.1 — no more hiding, card + bubble
                  // coexist). Covers both draft mode (flag off) and the v2
                  // inlineCanvasV2Id path (flag on).
                  const isInlineCanvasAnchor =
                    canRenderInlineCanvas && inlineCanvasAnchorMessageId === u.message.id;
                  // Show output file cards only under the last assistant message
                  // for a given run_id (prevents duplicate cards if a run has
                  // multiple assistant text blocks).
                  const showCards =
                    role === 'ASSISTANT' &&
                    runId != null &&
                    byRunId.has(runId) &&
                    lastAssistantUnitIdx.get(runId) === idx;

                  const roundKey = u.message.run_id ?? u.message.streamRunId ?? null;
                  const showRoundCopy =
                    roundKey != null && lastAssistantTextUnitIdx.get(roundKey) === idx;

                  // canvas.v2 Phase B: anchor a materialized canvas card under
                  // this assistant bubble when the row carries a canvas_id.  The
                  // card is ADDITIVE — the bubble always renders (a Phase B
                  // assistant turn writes one explanatory sentence as normal
                  // text), so the model's content is never hidden behind the
                  // card.  Skip when the Phase A inlineCanvasV2Id path already
                  // renders THIS canvas, so the same canvas never draws twice.
                  const showCanvasCard =
                    canvasV2 &&
                    role === 'ASSISTANT' &&
                    Boolean(u.message.canvas_id) &&
                    u.message.canvas_id !== inlineCanvasV2Id;

                  return (
                    <div key={u.message.id} className="min-w-0">
                      <MessageBubble
                        message={u.message}
                        friendly={friendly}
                        onPreviewFile={onPreviewFile}
                        onRewind={rewindTo}
                        showRoundCopy={showRoundCopy}
                        roundCopyText={roundKey != null ? roundCopyText.get(roundKey) : undefined}
                      />
                      {isInlineCanvasAnchor && renderInlineCanvasCard()}
                      {showCanvasCard && (
                        <CanvasMessageCard
                          sessionId={sessionId}
                          canvasId={u.message.canvas_id!}
                          aiDisabled={isStreaming}
                          onOpenPanel={onInlineCanvasOpenPanel}
                        />
                      )}
                      {showCards && (
                        <OutputFileCards
                          files={byRunId.get(runId!)!}
                          sessionId={sessionId}
                          onPreviewFile={onPreviewFile}
                        />
                      )}
                    </div>
                  );
                }
                return (
                  <StepGroup
                    key={`steps_${u.runId}_${u.steps[0]?.id ?? '0'}`}
                    group={u}
                    friendly={friendly}
                    onPreviewFile={onPreviewFile}
                  />
                );
              });
            })()}

            {/* ── Design fixture fallback (no real messages yet) ── */}
            {/* DESIGN FIXTURE — remove when backend provides transcript data */}
            {showFixture &&
              AGENT_TRANSCRIPT_FIXTURE.map((m) => (
                <FixtureMessage key={m.id} m={m} friendly={friendly} />
              ))}

            {/* ── Empty state (fixtures off, no messages) ── */}
            {!showFixture &&
              messages.length === 0 &&
              !isStreaming &&
              !inlineCanvasDraft &&
              !inlineCanvasV2Id && (
              <div className="flex-1 flex items-center justify-center py-16">
                <span className="font-mono italic text-dim text-[12px]">
                  Start a conversation with the AI agent
                </span>
              </div>
            )}

            {canRenderInlineCanvas && !anchoredInlineCanvasVisible && renderInlineCanvasCard()}

            {/* ── Current plan steps (live from agent TaskCreate/TaskUpdate) ── */}
            {planSteps && planSteps.length > 0 && <PlanBlock steps={planSteps} />}

            {/* ── Live "Working on it…" pill — shown while streaming, regardless of mode ── */}
            {isStreaming && <WorkingOnItPill />}

            {/* ── HITL cards — confirm / question ──────────────────────────────
                Pinned at the bottom of the message list when the agent pauses.
                The composer is DISABLED while cards are present (hasPending) to
                prevent the user from forking the paused run with a new message.

                Survives refresh/reconnect: useChat hydrates these on mount via
                GET /sessions/{sid}/pending (api.sessions.pending) so a run that
                paused while the page was closed still shows its card. */}
            {pendingInteractions.map((interaction) =>
              interaction.kind === 'confirm' ? (
                <ConfirmCard
                  key={interaction.toolCallId}
                  sessionId={sessionId}
                  interaction={interaction}
                />
              ) : (
                <QuestionCard
                  key={interaction.toolCallId}
                  sessionId={sessionId}
                  interaction={interaction}
                />
              ),
            )}

            <div ref={bottomRef} />
          </div>
        </div>
        {isAwayFromBottom && (
          <button
            type="button"
            className="chat-scroll-to-bottom"
            onClick={() => scrollToBottom('smooth')}
            aria-label="Scroll to latest message"
            title="Scroll to latest message"
          >
            ↓
          </button>
        )}
        <Composer
          onSend={handleSend}
          disabled={isStreaming || hasPending}
          isStreaming={isStreaming}
          onStop={cancel}
          onUpload={handleUpload}
          prefill={externalPrefill || prefillText}
          onPrefillConsumed={() => {
            if (externalPrefill) {
              onExternalPrefillConsumed?.();
            } else {
              clearPrefill();
            }
          }}
          skills={{
            skills,
            selected: selectedSkills,
            onToggle: (name) => toggleSkill(sessionId, name),
          }}
          mcp={{
            servers: mcpServers,
            selected: selectedMcpServerIds,
            // Seed-then-toggle: the first toggle materialises the tenant's
            // enabled-by-default set for this session, then flips one entry in
            // it. Until that happens the key stays `undefined` so /chat omits
            // `mcp_server_ids` entirely and the backend applies its own
            // defaults — unchecking the last server yields an explicit [],
            // which is a different thing (everything off).
            onToggle: (id) => {
              seedMcpServerIds(sessionId, mcpServers.filter((s) => s.enabled).map((s) => s.id));
              toggleMcpServer(sessionId, id);
            },
          }}
          model={selectedModel ?? defaultModelId ?? SYSTEM_FALLBACK_MODEL}
          effort={selectedEffort ?? DEFAULT_EFFORT}
          onModelChange={showThinkingUi ? (m) => setSessionModel(sessionId, m) : undefined}
          onEffortChange={showThinkingUi ? (e) => setSessionEffort(sessionId, e) : undefined}
          platformDefaults={{
            enabled: platformDefaultsEnabled,
            onToggle: () => setPlatformDefaultsEnabled(sessionId, !platformDefaultsEnabled),
          }}
          mentionCandidates={mentionCandidates}
        />
        {showDataNotice && <DataNoticeModal onAck={ackDataNotice} />}
        {pendingRewind && (
          <RewindConfirmModal
            message={pendingRewind}
            busy={rewindBusy}
            error={rewindError}
            onConfirm={confirmRewind}
            onCancel={cancelRewind}
          />
        )}
      </div>
    </MarkdownAssetsProvider>
  );
}
