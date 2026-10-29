import { useEffect, useMemo, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import { markdownRemarkPlugins, markdownRehypePlugins, normalizeMathDelimiters } from '../lib/markdownPlugins';
import { PageHead, type Crumb } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { StatusDot, type DesignStatus } from '../components/shell/StatusDot';
import { FilePreviewPanel, type FilePreviewPanelFile } from '../components/FilePreviewPanel';
import { markdownComponents } from '../components/chatStepRenderers';
import {
  MarkdownAssetsProvider,
  makeAttachmentImageResolver,
  type MarkdownAssets,
} from '../components/markdownAssets';
import { ConfirmCard } from '../components/hitl/ConfirmCard';
import { QuestionCard } from '../components/hitl/QuestionCard';
import { useOutputAttachments } from '../hooks/useOutputAttachments';
import { applyPendingInteractions } from '../hooks/useChat';
import { useChatStore } from '../stores/chatStore';
import { api } from '../api/client';
import type { Run, RunState, RunStatus, AppRunSnapshot } from '../api/types';
import { modelLabel, effortLabel } from '../lib/modelOptions';

const TERMINAL = new Set<RunStatus>(['COMPLETED', 'FAILED', 'CANCELLED']);

function runToDesignStatus(status: RunStatus): DesignStatus {
  switch (status) {
    case 'PENDING':
      return 'queued';
    case 'RUNNING':
      return 'thinking';
    case 'PAUSED':
      return 'paused';
    case 'COMPLETED':
      return 'done';
    case 'FAILED':
      return 'awaiting';
    case 'CANCELLED':
      return 'idle';
    default:
      return 'idle';
  }
}

function runStatusZh(status: RunStatus): string {
  switch (status) {
    case 'PENDING':
      return 'Queued';
    case 'RUNNING':
      return 'Running';
    case 'PAUSED':
      return 'Awaiting confirmation';
    case 'COMPLETED':
      return 'Completed';
    case 'FAILED':
      return 'Failed';
    case 'CANCELLED':
      return 'Cancelled';
    default:
      return status;
  }
}

/**
 * Translate the run hot-state `cursor` (the raw agent stream event type) into a
 * phase label the user can act on.
 *
 * The sibling `step` field is NOT a step count — the worker bumps it once per
 * stream event, and token-level deltas dominate, so it reads in the thousands
 * for a run that made a dozen real moves. We show the phase + elapsed time
 * instead; both are honest.
 */
function phaseZh(cursor: string | undefined, status: RunStatus): string {
  if (status === 'PENDING') return 'Queued…';
  // Empty cursor covers both "worker hasn't written state yet" and inline-mode
  // runs (which never write it) — the generic label is the honest one there.
  if (!cursor) return 'Running…';
  if (cursor === 'start') return 'Starting…';
  if (cursor.startsWith('TextBlock')) return 'Writing a reply…';
  if (cursor.startsWith('ToolCall')) return 'Calling a tool…';
  if (cursor.startsWith('ToolResult')) return 'Reading tool results…';
  if (cursor === 'RequireExternalExecutionEvent') return 'Waiting for external execution…';
  if (cursor === 'ReplyEndEvent') return 'Wrapping up…';
  // ThinkEvent / ThinkingBlock* / ModelCall* / ReplyStartEvent — the model is
  // working and hasn't emitted user-visible output yet.
  return 'Thinking…';
}

/** `mm:ss` (or `h:mm:ss`) since the run started. */
function elapsedZh(fromIso: string, nowMs: number): string {
  const secs = Math.max(0, Math.floor((nowMs - new Date(fromIso).getTime()) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

/** Render one snapshot input value as a readable string. */
function renderSnapshotValue(value: unknown): string {
  if (value == null) return '—';
  if (Array.isArray(value)) {
    // multiselect → strings; file → [{filename,...}]
    return value
      .map((v) => {
        if (v && typeof v === 'object' && 'filename' in (v as Record<string, unknown>)) {
          return String((v as { filename: unknown }).filename);
        }
        return String(v);
      })
      .join('、');
  }
  return String(value);
}

function SnapshotPanel({ snapshot }: { snapshot: AppRunSnapshot }) {
  return (
    <div className="border border-line rounded-md bg-surface overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 py-2.5 border-b border-line-2 bg-paper">
        <span className="text-[13px] font-semibold text-ink">{snapshot.app_name}</span>
        <Chip>v{snapshot.app_version}</Chip>
        <div className="flex-1" />
        <Chip>Model · {modelLabel(snapshot.model)}</Chip>
        {snapshot.reasoning_effort && <Chip>Reasoning · {effortLabel(snapshot.reasoning_effort)}</Chip>}
      </div>
      <div className="px-3.5 py-3 flex flex-col gap-2.5">
        <div className="flex flex-wrap gap-1.5 pb-1">
          {snapshot.forced_skills.map((s) => (
            <Chip key={s} dashed>Skill · {s}</Chip>
          ))}
          {(snapshot.forced_mcp_server_ids ?? []).map((id) => (
            <Chip key={id} dashed>MCP · {id}</Chip>
          ))}
          {Object.entries(snapshot.sampling_params ?? {}).map(([k, v]) => (
            <Chip key={k}>{k} · {String(v)}</Chip>
          ))}
        </div>
        {snapshot.inputs.length === 0 && (
          <div className="font-mono italic text-dim text-[11.5px]">No inputs</div>
        )}
        {snapshot.inputs.map((inp) => (
          <div key={inp.key} className="grid grid-cols-[120px_1fr] gap-3 items-start">
            <div className="text-[12px] text-dim truncate" title={inp.label}>
              {inp.label}
            </div>
            <div className="text-[12.5px] text-ink leading-[1.5] break-words whitespace-pre-wrap">
              {renderSnapshotValue(inp.value)}
            </div>
          </div>
        ))}
        {snapshot.rendered_task_preview && (
          <div className="border-t border-line-2 pt-3 mt-1 flex flex-col gap-1">
            <div className="cw-label">Final task</div>
            <pre className="m-0 font-mono text-[11.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap break-words bg-paper border border-line rounded-sm px-2.5 py-2">
              {snapshot.rendered_task_preview}
            </pre>
          </div>
        )}
        {(snapshot.system_prompt || snapshot.user_prompt_template) && (
          <details className="border-t border-line-2 pt-3 mt-1">
            <summary className="cw-label cursor-pointer">Fixed prompt snapshot</summary>
            <div className="mt-2 flex flex-col gap-2">
              {snapshot.system_prompt && (
                <pre className="m-0 font-mono text-[11px] text-ink-2 leading-[1.55] whitespace-pre-wrap break-words bg-paper border border-line rounded-sm px-2.5 py-2">
                  {snapshot.system_prompt}
                </pre>
              )}
              {snapshot.user_prompt_template && (
                <pre className="m-0 font-mono text-[11px] text-ink-2 leading-[1.55] whitespace-pre-wrap break-words bg-paper border border-line rounded-sm px-2.5 py-2">
                  {snapshot.user_prompt_template}
                </pre>
              )}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

export function AppResultPage() {
  const { sessionId, runId } = useParams<{ sessionId: string; runId: string }>();

  const [run, setRun] = useState<Run | null>(null);
  const [state, setState] = useState<RunState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [previewFile, setPreviewFile] = useState<FilePreviewPanelFile | null>(null);

  const { byRunId, refresh: refreshOutputs } = useOutputAttachments(sessionId);
  const outputs = runId ? byRunId.get(runId) ?? [] : [];

  // Lets the markdown renderer turn `![x](outputs/chart.png)` into this run's
  // real attachment URL, and open it in the preview panel on click.
  // `outputs` is a fresh array every render, so the memo keys off the id list
  // instead — a new resolver identity per render would re-render the whole
  // markdown tree for nothing.
  const outputsKey = outputs.map((a) => a.id).join(',');
  const markdownAssets = useMemo<MarkdownAssets>(
    () => ({
      resolveImage: makeAttachmentImageResolver(outputs, sessionId),
      onOpen: (t) =>
        t.id ? setPreviewFile({ id: t.id, filename: t.filename, mime: t.mime }) : undefined,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- outputsKey is the stable identity of outputs
    [outputsKey, sessionId],
  );

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const refreshedOnDoneRef = useRef(false);

  // ── HITL pause ─────────────────────────────────────────────────────────────
  //
  // A run can pause mid-flight to ask for a tool-call approval (`confirm`) or a
  // free-text answer (`question`). The cards are driven by the chat store —
  // exactly like the chat page — so we inherit its dedup + "acknowledged"
  // suppression (an answered card cannot be resurrected by a later refetch).
  // Only this run's interactions are pulled in, and they are cleared on unmount
  // so nothing leaks into the chat page for the same session.
  const sessionPending = useChatStore((s) =>
    sessionId ? s.pendingInteractions[sessionId] : undefined,
  );
  const runPending = (sessionPending ?? []).filter((p) => p.runId === runId);
  const [pendingLoaded, setPendingLoaded] = useState(false);
  const [pendingError, setPendingError] = useState<string | null>(null);

  // Ticking clock for the elapsed-time readout; only runs while non-terminal.
  const [nowMs, setNowMs] = useState(() => Date.now());
  const running = run != null && !TERMINAL.has(run.status);
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  useEffect(() => {
    if (!sessionId || !runId) {
      setLoadError('Missing run parameters.');
      return;
    }
    let alive = true;

    const tick = async () => {
      try {
        const r = await api.runs.get(sessionId, runId);
        if (!alive) return;
        setRun(r);
        if (!TERMINAL.has(r.status)) {
          // Live hot-state for plan/step while running.
          try {
            const st = await api.runs.getState(sessionId, runId);
            if (alive) setState(st);
          } catch {
            /* hot-state may not exist yet; ignore */
          }
        } else {
          // Terminal — fetch outputs once, then stop polling.
          if (!refreshedOnDoneRef.current) {
            refreshedOnDoneRef.current = true;
            refreshOutputs();
          }
          if (pollRef.current) {
            clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }
      } catch (e) {
        if (alive) setLoadError((e as Error).message);
      }
    };

    tick();
    pollRef.current = setInterval(tick, 2000);

    return () => {
      alive = false;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [sessionId, runId, refreshOutputs]);

  const snapshot = run?.app_snapshot ?? null;
  const status = run?.status;

  // Pull this run's paused interactions whenever the run enters PAUSED. The
  // 2 s poll above keeps `status` fresh, so a pause → resume → pause sequence
  // re-fetches naturally without any extra plumbing.
  useEffect(() => {
    if (!sessionId || !runId) return;
    if (status !== 'PAUSED') {
      setPendingLoaded(false);
      setPendingError(null);
      return;
    }
    let alive = true;
    (async () => {
      try {
        const res = await api.sessions.pending(sessionId);
        if (!alive) return;
        applyPendingInteractions(
          sessionId,
          res.items.filter((it) => it.run_id === runId),
        );
        setPendingError(null);
      } catch (e) {
        if (alive) setPendingError((e as Error).message);
      } finally {
        if (alive) setPendingLoaded(true);
      }
    })();
    return () => {
      alive = false;
    };
  }, [sessionId, runId, status]);

  // Drop this run's cards from the shared store on unmount so they never show
  // up on the chat page for the same session.
  useEffect(() => {
    if (!sessionId || !runId) return;
    return () => {
      useChatStore.getState().clearPendingInteractions(sessionId, runId);
    };
  }, [sessionId, runId]);

  const crumbs: Crumb[] = [
    { label: 'Home', to: '/home' },
    { label: 'Apps', to: '/apps' },
    ...(snapshot?.app_id
      ? [{ label: snapshot.app_name, to: `/apps/${snapshot.app_id}` }]
      : []),
    { label: 'Run result' },
  ];

  // ─── Split layout (transcript-ish column + optional preview panel) ──────────
  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={crumbs}
        status={
          status ? (
            <StatusDot status={runToDesignStatus(status)} label={runStatusZh(status)} />
          ) : undefined
        }
      />

      <div
        className="min-h-0 flex-1"
        style={
          previewFile
            ? { display: 'grid', gridTemplateColumns: '1fr 360px', minHeight: 0 }
            : { display: 'flex', flexDirection: 'column', minHeight: 0 }
        }
      >
        <div className="cw-scroll flex-1 min-h-0 px-[5vw] py-8 flex flex-col items-center">
          <div className="w-[min(760px,95%)] flex flex-col gap-6">
            {loadError && (
              <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
                {loadError}
              </div>
            )}

            {!run && !loadError && (
              <div className="font-mono text-[12px] text-dim">Loading…</div>
            )}

            {/* RUNNING / PENDING — streamlined progress, NO composer */}
            {run && (status === 'PENDING' || status === 'RUNNING') && (
              <div className="flex flex-col gap-4">
                {snapshot && <SnapshotPanel snapshot={snapshot} />}
                <div className="border border-line rounded-md bg-surface px-4 py-4 flex flex-col gap-3">
                  <div className="flex items-center gap-2">
                    <span className="cw-dot animate-blip" style={{ background: '#d23a1f' }} />
                    <span className="text-[13px] font-medium text-ink">
                      {phaseZh(state?.cursor, status)}
                    </span>
                    <span className="font-mono text-[11px] text-dim">
                      {elapsedZh(run.started_at ?? run.created_at, nowMs)}
                    </span>
                  </div>
                  {state?.plan && (
                    <div className="text-[12.5px] text-ink-2 leading-[1.55] whitespace-pre-wrap border-t border-line-2 pt-3">
                      {state.plan}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* PAUSED — snapshot + HITL cards so the run can be resumed here */}
            {run && status === 'PAUSED' && sessionId && (
              <div className="flex flex-col gap-4">
                {snapshot && <SnapshotPanel snapshot={snapshot} />}

                <div className="border border-line rounded-md bg-surface px-4 py-4 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    <span className="cw-dot" style={{ background: '#c68a17' }} />
                    <span className="text-[13px] font-medium text-ink">Run paused — waiting for your confirmation</span>
                  </div>
                  <div className="text-[12.5px] text-dim leading-[1.55]">
                    Once you handle the request below, the run continues automatically — no need to refresh.
                  </div>
                </div>

                {pendingError && (
                  <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
                    Failed to load pending items: {pendingError}
                  </div>
                )}

                {runPending.map((p) =>
                  p.kind === 'question' ? (
                    <QuestionCard key={p.toolCallId} sessionId={sessionId} interaction={p} />
                  ) : (
                    <ConfirmCard key={p.toolCallId} sessionId={sessionId} interaction={p} />
                  ),
                )}

                {pendingLoaded && !pendingError && runPending.length === 0 && (
                  <div className="font-mono italic text-dim text-[12px]">
                    No pending confirmations (they may have been answered elsewhere). The run status refreshes automatically.
                  </div>
                )}
              </div>
            )}

            {/* COMPLETED — snapshot + result_summary markdown + output files */}
            {run && status === 'COMPLETED' && (
              <div className="flex flex-col gap-6">
                {snapshot && <SnapshotPanel snapshot={snapshot} />}

                {run.result_summary && (
                  <div className="flex flex-col gap-2">
                    <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
                      Output
                    </div>
                    <div className="prose-cw text-[14px] leading-[1.6] text-ink">
                      <MarkdownAssetsProvider value={markdownAssets}>
                        <ReactMarkdown
                          remarkPlugins={markdownRemarkPlugins}
                          rehypePlugins={markdownRehypePlugins as never}
                          components={markdownComponents}
                        >
                          {normalizeMathDelimiters(run.result_summary)}
                        </ReactMarkdown>
                      </MarkdownAssetsProvider>
                    </div>
                  </div>
                )}

                {outputs.length > 0 && (
                  <div className="flex flex-col gap-2">
                    <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
                      Output files
                    </div>
                    <div className="flex flex-col gap-1.5">
                      {outputs.map((att) => (
                        <div
                          key={att.id}
                          className="flex items-center gap-2 px-3 py-2 border border-line rounded-md bg-surface"
                        >
                          <span className="font-mono text-[12px] text-dim">📎</span>
                          <button
                            type="button"
                            onClick={() =>
                              setPreviewFile({ id: att.id, filename: att.filename, mime: att.mime })
                            }
                            className="text-[13px] text-ink hover:text-accent truncate flex-1 text-left"
                            title={att.filename}
                          >
                            {att.filename}
                          </button>
                          <a
                            href={`/api/bff/v1/sessions/${sessionId}/files/${att.id}/content`}
                            download={att.filename}
                            className="cw-btn cw-btn-ghost cw-btn-sm shrink-0"
                          >
                            Download
                          </a>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {!run.result_summary && outputs.length === 0 && (
                  <div className="font-mono italic text-dim text-[12px]">This run produced no text or files.</div>
                )}
              </div>
            )}

            {/* FAILED / CANCELLED — error + snapshot */}
            {run && (status === 'FAILED' || status === 'CANCELLED') && (
              <div className="flex flex-col gap-6">
                <div className="px-3.5 py-3 border border-accent-line bg-accent-soft rounded-md">
                  <div className="text-[13px] font-medium text-accent mb-1">
                    {status === 'FAILED' ? 'Run failed' : 'Run cancelled'}
                  </div>
                  {run.error_message && (
                    <div className="text-[12.5px] text-ink-2 leading-[1.5] whitespace-pre-wrap break-words">
                      {run.error_message}
                    </div>
                  )}
                </div>
                {snapshot && <SnapshotPanel snapshot={snapshot} />}
              </div>
            )}
          </div>
        </div>

        {previewFile && sessionId && (
          <FilePreviewPanel
            sessionId={sessionId}
            file={previewFile}
            onClose={() => setPreviewFile(null)}
            closeVariant="x"
          />
        )}
      </div>
    </div>
  );
}
