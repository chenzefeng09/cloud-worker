import { useEffect, useRef } from 'react';
import { useChat } from '../hooks/useChat';
import { useOutputAttachments } from '../hooks/useOutputAttachments';
import { useInstalledSkills } from '../hooks/useInstalledSkills';
import { useChatStore } from '../stores/chatStore';
import { MessageBubble, WorkingOnItPill } from './MessageBubble';
import { StepGroup } from './StepGroup';
import { OutputFileCards } from './OutputFileCards';
import { InputBar } from './InputBar';
import { PlanBlock } from './PlanBlock';
import { RewindConfirmModal } from './RewindConfirmModal';
import { DataNoticeModal } from './DataNoticeModal';
import { api } from '../api/client';
import type { Attachment, MessageAttachment } from '../api/types';
import { groupSteps } from '../lib/groupSteps';
import { DEFAULT_EFFORT, SYSTEM_FALLBACK_MODEL } from '../lib/modelOptions';
import { useDefaultModel } from '../hooks/useDefaultModel';
import { useFeatureFlag } from '../hooks/useFeatureFlags';
import {
  SHOW_DESIGN_FIXTURES,
  AGENT_TRANSCRIPT_FIXTURE,
} from '../lib/designFixtures';
import type { TranscriptMessage } from '../lib/designFixtures';

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

import { useState } from 'react';

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
  friendly,
  onPreviewFile,
}: {
  sessionId: string;
  friendly?: boolean;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void;
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
  const bottomRef = useRef<HTMLDivElement>(null);
  // Track previous isStreaming value so we can detect the true→false edge.
  const prevStreamingRef = useRef(isStreaming);

  // Per-session skill toggles, surfaced in the InputBar "+" popover.
  const skills = useInstalledSkills();
  const selectedSkills = useChatStore((s) => s.selectedSkills[sessionId]);
  const toggleSkill = useChatStore((s) => s.toggleSkill);
  const seedSkills = useChatStore((s) => s.seedSkills);

  // Per-session model + reasoning effort, surfaced in the InputBar toolbar.
  const showThinkingUi = useFeatureFlag('ai.thinking_ui');
  const selectedModel = useChatStore((s) => s.selectedModel[sessionId]);
  const selectedEffort = useChatStore((s) => s.selectedEffort[sessionId]);
  const setSessionModel = useChatStore((s) => s.setSessionModel);
  const setSessionEffort = useChatStore((s) => s.setSessionEffort);
  const { modelId: defaultModelId } = useDefaultModel('llm');

  // Live plan steps for this session — rendered at the bottom of the transcript.
  const planSteps = useChatStore((s) => s.planSteps[sessionId]);

  // First-use data-usage notice: show once per device, then remember the
  // acknowledgement in localStorage so it never appears again.
  const [showDataNotice, setShowDataNotice] = useState(false);
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

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, isStreaming]);

  // Refresh output attachments when streaming transitions true → false
  // (i.e. a run just completed). This surfaces newly written files quickly.
  useEffect(() => {
    if (prevStreamingRef.current && !isStreaming) {
      refreshOutputs();
    }
    prevStreamingRef.current = isStreaming;
  }, [isStreaming, refreshOutputs]);

  const handleUpload = async (file: File): Promise<Attachment> => {
    return api.attachments.upload(sessionId, file);
  };

  const handleSend = (text: string, attachmentIds?: string[], attachments?: Attachment[]) => {
    const attMeta: MessageAttachment[] | undefined = attachments?.map((a) => ({
      id: a.id,
      filename: a.filename,
      mime: a.mime,
      size: a.size_bytes,
    }));
    sendMessage(text, attachmentIds, attMeta);
  };

  // Show fixture fallback only when no real messages exist, not streaming,
  // and the design-fixture flag is on. DESIGN FIXTURE — remove the fixture
  // block when backend provides real data.
  const showFixture =
    SHOW_DESIGN_FIXTURES && messages.length === 0 && !isStreaming;

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="cw-scroll flex-1 px-8 py-5 flex flex-col gap-[8px]">
        <div style={{ maxWidth: 780, margin: '0 auto', width: '100%', display: 'flex', flexDirection: 'column', gap: 8 }}>
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

                return (
                  <div key={u.message.id}>
                    <MessageBubble
                      message={u.message}
                      friendly={friendly}
                      onPreviewFile={onPreviewFile}
                      onRewind={rewindTo}
                      showRoundCopy={showRoundCopy}
                      roundCopyText={roundKey != null ? roundCopyText.get(roundKey) : undefined}
                    />
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
          {!showFixture && messages.length === 0 && !isStreaming && (
            <div className="flex-1 flex items-center justify-center py-16">
              <span className="font-mono italic text-dim text-[12px]">
                开始与 AI Agent 对话
              </span>
            </div>
          )}

          {/* ── Current plan steps (live from agent TaskCreate/TaskUpdate) ── */}
          {planSteps && planSteps.length > 0 && <PlanBlock steps={planSteps} />}

          {/* ── Live "Working on it…" pill — shown while streaming, regardless of mode ── */}
          {isStreaming && <WorkingOnItPill />}

          <div ref={bottomRef} />
        </div>
      </div>
      <InputBar
        onSend={handleSend}
        disabled={isStreaming}
        isStreaming={isStreaming}
        onStop={cancel}
        onUpload={handleUpload}
        prefill={prefillText}
        onPrefillConsumed={clearPrefill}
        sessionId={sessionId}
        skills={skills}
        selectedSkills={selectedSkills}
        onToggleSkill={(name) => toggleSkill(sessionId, name)}
        model={selectedModel ?? defaultModelId ?? SYSTEM_FALLBACK_MODEL}
        effort={selectedEffort ?? DEFAULT_EFFORT}
        onModelChange={showThinkingUi ? (m) => setSessionModel(sessionId, m) : undefined}
        onEffortChange={showThinkingUi ? (e) => setSessionEffort(sessionId, e) : undefined}
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
  );
}
