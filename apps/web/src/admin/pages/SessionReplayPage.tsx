import { useCallback, useEffect, useState } from 'react';
import { api } from '../../api/client';
import type { Message, Run, Session } from '../../api/types';
import { formatDateTimeAuto } from '../../i18n/dates';
import { groupSteps } from '../../lib/groupSteps';
import { MessageBubble } from '../../components/MessageBubble';
import { StepGroup } from '../../components/StepGroup';
import { formatRelativeTimeZh } from '../../lib/format';

// ─── helpers ─────────────────────────────────────────────────────────────────

function fmtTokens(n: number | null | undefined): string {
  if (n == null) return '—';
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—';
  return formatDateTimeAuto(new Date(iso));
}

function runStatusZh(status: string | null | undefined): string {
  switch (status) {
    case 'PENDING':   return '待执行';
    case 'RUNNING':   return '运行中';
    case 'COMPLETED': return '已完成';
    case 'FAILED':    return '失败';
    case 'CANCELLED': return '已取消';
    default:          return status ?? '—';
  }
}

// ─── sub-components ───────────────────────────────────────────────────────────

/** Metadata banner shown at the top of the replay. */
function ReplayMeta({ session, run }: { session: Session; run: Run }) {
  return (
    <div
      className="flex flex-wrap gap-5 px-5 py-3 border-b border-line bg-paper text-[12px] font-mono text-ink-2 shrink-0"
    >
      <span>
        <span className="text-dim">session </span>
        <b className="text-ink">{session.id.slice(0, 8)}…</b>
      </span>
      <span>
        <span className="text-dim">run </span>
        <b className="text-ink">{run.id.slice(0, 8)}…</b>
      </span>
      <span>
        <span className="text-dim">状态 </span>
        <b
          style={{
            color:
              run.status === 'COMPLETED'
                ? 'var(--cw-green, #2c8f5d)'
                : run.status === 'FAILED'
                  ? 'var(--accent)'
                  : 'var(--ink)',
          }}
        >
          {runStatusZh(run.status)}
        </b>
      </span>
      <span>
        <span className="text-dim">开始 </span>
        {fmtDate(run.started_at)}
      </span>
      <span>
        <span className="text-dim">结束 </span>
        {fmtDate(run.finished_at)}
      </span>
      <span>
        <span className="text-dim">tokens </span>
        <b className="text-ink">
          in {fmtTokens(run.input_tokens)} · out {fmtTokens(run.output_tokens)} · total {fmtTokens(run.total_tokens)}
        </b>
      </span>
      {session.title && (
        <span>
          <span className="text-dim">标题 </span>
          <b className="text-ink">{session.title}</b>
        </span>
      )}
    </div>
  );
}

/** Scrollable read-only transcript using the same renderers as the live chat. */
function ReplayTranscript({ messages }: { messages: Message[] }) {
  const units = groupSteps(messages, false /* isStreaming = false */);

  if (units.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-dim font-mono text-[12px] italic">
        该 run 无消息记录
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto px-8 py-5">
      <div
        style={{ maxWidth: 780, margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 18 }}
      >
        {units.map((u) =>
          u.kind === 'message' ? (
            <MessageBubble key={u.message.id} message={u.message} friendly={false} />
          ) : (
            <StepGroup
              key={`steps_${u.runId}_${u.steps[0]?.id ?? '0'}`}
              group={u}
              friendly={false}
            />
          ),
        )}
      </div>
    </div>
  );
}

// ─── Run picker ───────────────────────────────────────────────────────────────

function RunPicker({
  runs,
  selectedId,
  onSelect,
}: {
  runs: Run[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  if (runs.length === 0) {
    return (
      <div className="font-mono text-[12px] italic text-dim px-1">该 session 暂无 run</div>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      {runs.map((r) => {
        const active = r.id === selectedId;
        return (
          <button
            key={r.id}
            onClick={() => onSelect(r.id)}
            className="text-left px-2 py-1.5 rounded-sm border text-[12px] font-mono transition-colors"
            style={{
              background: active ? 'var(--surface)' : 'transparent',
              borderColor: active ? 'var(--line)' : 'transparent',
              color: active ? 'var(--ink)' : 'var(--ink-2)',
            }}
          >
            <div className="flex items-center gap-2">
              <span
                style={{
                  color:
                    r.status === 'COMPLETED'
                      ? 'var(--cw-green, #2c8f5d)'
                      : r.status === 'FAILED'
                        ? 'var(--accent)'
                        : 'var(--dim)',
                  fontSize: 9,
                }}
              >
                ●
              </span>
              <span className="text-dim text-[10.5px]">{r.id.slice(0, 8)}</span>
              <span className="text-dim text-[10.5px] ml-auto">{formatRelativeTimeZh(r.created_at)}</span>
            </div>
            <div className="text-[10.5px] text-dim mt-0.5 truncate">
              {runStatusZh(r.status)} · {fmtTokens(r.total_tokens)} tok
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ─── Session search ───────────────────────────────────────────────────────────

function SessionSearch({
  sessions,
  selectedId,
  onSelect,
  searchText,
  onSearchChange,
}: {
  sessions: Session[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  searchText: string;
  onSearchChange: (v: string) => void;
}) {
  const lower = searchText.toLowerCase();
  const filtered = sessions.filter(
    (s) =>
      s.id.toLowerCase().includes(lower) ||
      (s.title ?? '').toLowerCase().includes(lower),
  );

  return (
    <div className="flex flex-col gap-1.5">
      <input
        value={searchText}
        onChange={(e) => onSearchChange(e.target.value)}
        placeholder="搜索 session ID 或标题…"
        className="w-full border border-line rounded-sm px-2 py-1.5 text-[12px] font-mono bg-paper focus:outline-none focus:border-accent"
      />
      <div className="flex flex-col gap-0.5 max-h-[220px] overflow-y-auto">
        {filtered.length === 0 && (
          <div className="font-mono text-[11px] italic text-dim px-1">无匹配 session</div>
        )}
        {filtered.map((s) => {
          const active = s.id === selectedId;
          return (
            <button
              key={s.id}
              onClick={() => onSelect(s.id)}
              className="text-left px-2 py-1.5 rounded-sm border transition-colors"
              style={{
                background: active ? 'var(--surface)' : 'transparent',
                borderColor: active ? 'var(--line)' : 'transparent',
                color: active ? 'var(--ink)' : 'var(--ink-2)',
              }}
            >
              <div className="text-[12px] font-mono truncate">
                {s.title || '未命名任务'}
              </div>
              <div className="text-[10.5px] font-mono text-dim truncate">
                {s.id.slice(0, 12)}… · {formatRelativeTimeZh(s.updated_at)}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

/**
 * SessionReplayPage — read-only replay of any agent session within 30 days.
 *
 * Layout:
 *   left rail (280px): session picker → run list
 *   main area: metadata banner + transcript
 *
 * Reuses the exact same rendering chain as the live chat:
 *   groupSteps() → MessageBubble / StepGroup → chatStepRenderers (ThinkingBlock, ToolRow)
 *
 * Session listing uses the admin-only api.admin.sessions.list() (whole-tenant,
 * tenant_admin-gated in the BFF); runs and messages reuse the existing
 * api.runs.list / api.messages.list proxies (scoped per session_id, which the
 * orchestrator already tenant-checks).
 *
 * TODO (time-axis): add a step-by-step playback slider that reveals messages
 * one-by-one so operators can "scrub" through the run timeline.  The data is
 * already in `messages` sorted by `sequence`; a simple index cursor + Next/Prev
 * buttons would be sufficient.  Skipping for now due to scope.
 */
export function SessionReplayPage() {
  // ── session list ────────────────────────────────────────────────────────────
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [searchText, setSearchText] = useState('');

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      // Admin-only: lists the WHOLE tenant's sessions (gated server-side by a
      // tenant_admin authz check).  The chat page uses api.sessions.list(),
      // which is owner-scoped and only returns the caller's own sessions.
      const page = await api.admin.sessions.list();
      setSessions(Array.isArray(page?.items) ? page.items : []);
    } catch {
      // failure-open
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  // ── selected session ────────────────────────────────────────────────────────
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoading, setRunsLoading] = useState(false);

  const selectSession = useCallback(async (sid: string) => {
    setSelectedSessionId(sid);
    setSelectedRunId(null);
    setMessages([]);
    setRunsLoading(true);
    try {
      const res = await api.runs.list(sid);
      const items = Array.isArray(res?.items) ? res.items : [];
      // Sort newest-first for display
      items.sort((a, b) => (b.created_at > a.created_at ? 1 : -1));
      setRuns(items);
    } catch {
      setRuns([]);
    } finally {
      setRunsLoading(false);
    }
  }, []);

  // ── selected run ────────────────────────────────────────────────────────────
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesError, setMessagesError] = useState<string | null>(null);

  const selectRun = useCallback(
    async (runId: string) => {
      if (!selectedSessionId) return;
      setSelectedRunId(runId);
      setMessagesLoading(true);
      setMessagesError(null);
      try {
        // Load all messages for the session and filter to this run.
        // The API doesn't support per-run filtering yet, so we filter client-side.
        // For sessions with many messages, pagination may be needed (future work).
        const page = await api.messages.list(selectedSessionId);
        const all = Array.isArray(page?.items) ? page.items : [];
        // Keep messages that belong to this run, plus USER messages (they seed each run).
        // USER messages have run_id = null so we include them all.
        const filtered = all.filter(
          (m) =>
            m.run_id === runId ||
            (m.role || '').toUpperCase() === 'USER' ||
            m.run_id == null,
        );
        setMessages(filtered);
      } catch (err) {
        setMessagesError((err as Error).message ?? '加载失败');
        setMessages([]);
      } finally {
        setMessagesLoading(false);
      }
    },
    [selectedSessionId],
  );

  const selectedSession = sessions.find((s) => s.id === selectedSessionId) ?? null;
  const selectedRun = runs.find((r) => r.id === selectedRunId) ?? null;

  return (
    <div className="flex h-full min-w-0">
      {/* ── Left rail ─────────────────────────────────────────────────────── */}
      <aside
        className="w-[280px] shrink-0 border-r border-line bg-paper overflow-hidden flex flex-col"
      >
        {/* Header */}
        <div className="px-4 py-3 border-b border-line shrink-0">
          <div className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim mb-2.5">
            会话回放
          </div>
          {sessionsLoading ? (
            <div className="font-mono text-[11px] text-dim animate-pulse">加载中…</div>
          ) : (
            <SessionSearch
              sessions={sessions}
              selectedId={selectedSessionId}
              onSelect={selectSession}
              searchText={searchText}
              onSearchChange={setSearchText}
            />
          )}
        </div>

        {/* Run list */}
        {selectedSessionId && (
          <div className="flex-1 overflow-y-auto p-3">
            <div className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-dim mb-2">
              Runs
            </div>
            {runsLoading ? (
              <div className="font-mono text-[11px] text-dim animate-pulse">加载中…</div>
            ) : (
              <RunPicker
                runs={runs}
                selectedId={selectedRunId}
                onSelect={selectRun}
              />
            )}
          </div>
        )}
      </aside>

      {/* ── Main replay area ───────────────────────────────────────────────── */}
      <main className="flex-1 flex flex-col min-w-0 min-h-0 bg-surface">
        {!selectedSessionId && (
          <div className="flex-1 flex flex-col items-center justify-center gap-2">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
              选择左侧 session 开始回放
            </span>
          </div>
        )}

        {selectedSessionId && !selectedRunId && (
          <div className="flex-1 flex flex-col items-center justify-center gap-2">
            <span className="font-mono text-[10.5px] uppercase tracking-[0.1em] text-dim">
              选择左侧 run 加载对话记录
            </span>
          </div>
        )}

        {selectedSession && selectedRun && (
          <>
            {/* Metadata banner */}
            <ReplayMeta session={selectedSession} run={selectedRun} />

            {/* Read-only tag */}
            <div className="px-5 py-1.5 shrink-0 flex items-center gap-2 border-b border-line-2 bg-paper">
              <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-dim">
                只读回放
              </span>
              <span className="font-mono text-[10px] text-dim">·</span>
              <span className="font-mono text-[10px] text-dim">
                {messages.length} 条消息
              </span>
            </div>

            {/* Transcript */}
            {messagesLoading ? (
              <div className="flex-1 flex items-center justify-center font-mono text-[12px] text-dim animate-pulse">
                加载对话记录…
              </div>
            ) : messagesError ? (
              <div className="flex-1 flex items-center justify-center px-8">
                <div className="border border-accent-line bg-accent-soft rounded-md px-4 py-3 text-[13px] text-accent font-mono max-w-md text-center">
                  {messagesError}
                </div>
              </div>
            ) : (
              <ReplayTranscript messages={messages} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
