import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { useTranslation } from '../i18n';
import { formatDate } from '../i18n/dates';
import { useWorkspace } from '../lib/workspaceContext';
import { SENSITIVITY_LABELS, DEFAULT_SENSITIVITY } from '../lib/sensitivity';
import { useSessions } from '../hooks/useSessions';
import { api } from '../api/client';
import type { Attachment, ExecutionMode, Session } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
import { HoverHint } from '../components/shell/HoverHint';
import { useChatStore } from '../stores/chatStore';
import { useApps } from '../hooks/useApps';
import { CreateAppModal } from '../components/app/CreateAppModal';
import {
  MODEL_OPTIONS,
  EFFORT_OPTIONS,
  SYSTEM_FALLBACK_MODEL,
  DEFAULT_EFFORT,
} from '../lib/modelOptions';
import { useDefaultModel } from '../hooks/useDefaultModel';
import { useCanUploadAttachment } from '../hooks/useCanUploadAttachment';
import { useFeatureFlag } from '../hooks/useFeatureFlags';

const PLACEHOLDERS = [
  '帮我安排明天的团队站会…',
  '把这份 PDF 概括成 5 个要点？',
  '草拟一条回复供应商邮件的中文回信…',
  '迁移到 passkey 的最佳路径是什么？',
];

const SUGGESTIONS = ['帮我规划明天', '概括这份 PDF', '回复供应商邮件', 'Q3 发布命名头脑风暴'];

const EXECUTION_MODE_OPTIONS = [
  { id: 'sandbox', label: '智能体' },
  { id: 'inline', label: '对话' },
];

/** Default glyph for an App card when the App has no icon set. */
const DEFAULT_APP_GLYPH = '▤';

export function HomePage() {
  const { lang } = useTranslation();
  const { activeWorkspace, workspaces, setActiveWorkspaceId } = useWorkspace();
  const { sessions, createSession } = useSessions();
  const { apps, loading: appsLoading } = useApps('published');
  const { apps: myApps, loading: myAppsLoading } = useApps('mine');
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [phIdx, setPhIdx] = useState(0);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const { checkFile } = useCanUploadAttachment();
  // Reasoning-effort control is gated behind the same flag as the chat page
  // (ChatWindow). Default false → the "Off▾" dropdown is hidden until enabled.
  const showThinkingUi = useFeatureFlag('ai.thinking_ui');
  const { modelId: defaultModelId, isLoading: defaultModelLoading } = useDefaultModel('llm');
  const [model, setModelRaw] = useState<string>(SYSTEM_FALLBACK_MODEL);
  const [effort, setEffort] = useState<string>(DEFAULT_EFFORT);
  // Keep new sessions aligned with ChatWindow/useChat's authoritative default:
  // agent mode loads the tenant's enabled MCP servers, while inline mode is the
  // deliberately lightweight direct-conversation path.
  const [executionMode, setExecutionMode] = useState<ExecutionMode>('sandbox');
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
  const setSessionExecutionMode = useChatStore((s) => s.setSessionExecutionMode);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
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
  // earlier upload).  Title defaults to the current draft text.
  const ensureSession = async (): Promise<Session | null> => {
    if (sessionRef.current) return sessionRef.current;
    // Started inside a workspace → tag the session with it so the backend
    // injects the workspace's instructions / base files / skills.
    const session = await createSession(draft.trim() || undefined, activeWorkspace?.id);
    if (session) {
      sessionRef.current = session;
      setSessionExecutionMode(session.id, executionMode);
    }
    return session ?? null;
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (!files.length) return;
    setSubmitError(null);
    setUploading(true);
    try {
      const session = await ensureSession();
      if (!session) {
        setSubmitError('创建任务失败，请稍后重试。');
        return;
      }
      for (const file of files) {
        // Pre-upload gate: large attachments require the large-attachment
        // entitlement; check before the upload network call.
        const gate = await checkFile(file);
        if (!gate.allowed) {
          setSubmitError('该文件较大，需要管理员授予大附件权限后才能上传，请联系管理员。');
          return;
        }
        const att = await api.attachments.upload(session.id, file);
        setAttachments((prev) => [...prev, att]);
      }
    } catch (err) {
      setSubmitError('附件上传失败，请稍后重试。');
      console.warn('attachment upload failed', err);
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const removeAttachment = (id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  };

  const isImage = (mime: string) => mime.startsWith('image/');

  const startChat = async () => {
    setSubmitError(null);
    setSubmitting(true);
    try {
      const draftText = draft.trim();
      // Reuse the session created during an earlier file upload, if any.
      const session = await ensureSession();
      if (session) {
        // Persist the composer's model + effort choice onto the new session so
        // useChat picks them up at send time (same store the chat page reads).
        setSessionModel(session.id, model);
        setSessionEffort(session.id, effort);
        setSessionExecutionMode(session.id, executionMode);
        // Pass the first user message and any pre-uploaded attachments through
        // router state so AgentPage can dispatch them once the chat store is
        // bound to the new session.  Without this the draft was being thrown
        // away.
        navigate(`/tasks/${session.id}?mode=friendly`, {
          state: {
            initialMessage: draftText,
            attachmentIds: attachments.map((a) => a.id),
          },
        });
      }
    } catch (err) {
      setSubmitError('创建任务失败，请稍后重试。');
       
      console.warn('create session failed', err);
    } finally {
      setSubmitting(false);
    }
  };


  const openSession = (s: Session) => {
    if (s.metadata?.kind === 'app_run' && s.current_run_id) {
      navigate(`/apps/runs/${s.id}/${s.current_run_id}`);
      return;
    }
    navigate(`/tasks/${s.id}`);
  };

  const dateLabel = formatDate(new Date(), lang, {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
  });

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页' }]}
        actions={
          <>
            <button className="cw-btn cw-btn-ghost cw-btn-sm">⌘K 启动</button>
          </>
        }
      />

      <div className="relative flex-1 min-h-0 flex flex-col">
        {/* faint grid background */}
        <div
          aria-hidden
          className="absolute inset-0 pointer-events-none opacity-60"
          style={{
            backgroundImage:
              'linear-gradient(#e6e3d6 1px, transparent 1px), linear-gradient(90deg, #e6e3d6 1px, transparent 1px)',
            backgroundSize: '32px 32px',
          }}
        />

        <div className="cw-scroll flex-1 px-[5vw] pt-8 pb-8 flex flex-col items-center gap-7 relative">
          {/* greeting */}
          <div className="text-center shrink-0">
            <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-2">
              {dateLabel}
            </div>
            <h1 className="m-0 font-display font-normal tracking-[-0.02em] leading-[1.15] text-[clamp(24px,3.4vw,40px)]">
              你好。
              <br />
              <span className="text-dim">需要我做点什么？</span>
            </h1>
          </div>

          {/* composer */}
          <div className="w-[min(720px,95%)] bg-surface border border-line rounded-[20px] shadow-[0_18px_44px_-24px_rgba(15,17,20,0.3)] shrink-0">
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={PLACEHOLDERS[phIdx]}
              onKeyDown={(e) => {
                if (e.key !== 'Enter' || e.shiftKey) return;
                // IME 组合期间的 Enter（如拼音上屏）不触发发送
                if (e.nativeEvent.isComposing) return;
                e.preventDefault();
                if (!submitting && !uploading) startChat();
              }}
              className="w-full min-h-[132px] resize-none border-none outline-none bg-transparent text-ink text-[17px] leading-[1.5] tracking-[-0.01em] px-5 pt-5 pb-2 rounded-t-[20px]"
            />
            {/* Attachment chips */}
            {(attachments.length > 0 || uploading) && (
              <div className="flex flex-wrap gap-1.5 px-4 pt-2 pb-1">
                {attachments.map((att) => (
                  <div
                    key={att.id}
                    className="flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface"
                  >
                    <span className="text-ink-2">{isImage(att.mime) ? '🖼' : '📎'}</span>
                    <span className="max-w-[120px] truncate text-ink">{att.filename}</span>
                    <button
                      type="button"
                      onClick={() => removeAttachment(att.id)}
                      className="font-mono text-dim hover:text-ink ml-0.5 leading-none"
                      aria-label="移除附件"
                    >
                      ×
                    </button>
                  </div>
                ))}
                {uploading && (
                  <div className="flex items-center px-2 py-1 text-[11px] text-dim font-mono">
                    <span className="animate-pulse">上传中…</span>
                  </div>
                )}
              </div>
            )}
            <div
              data-testid="home-composer-toolbar"
              className="flex items-center gap-3 min-h-[58px] px-4 pt-1 pb-3"
            >
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,.pdf,.txt,.csv,.md,.json,.py,.js,.ts,.html,.xml,.yaml,.yml,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.wps,.et,.dps"
                className="hidden"
                onChange={handleFileChange}
                disabled={uploading}
              />
              <HoverHint label="添加文件或图片" align="left">
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  disabled={uploading}
                  className="w-9 h-9 shrink-0 inline-flex items-center justify-center rounded-full bg-paper text-ink text-[21px] leading-none hover:bg-paper-2 transition-colors disabled:opacity-40"
                  aria-label="添加文件"
                >
                  <span aria-hidden className="font-sans -translate-y-px">+</span>
                </button>
              </HoverHint>
              <div className="flex min-w-0 items-center gap-3">
                <HoverHint label="切换任务使用的工作区" align="left">
                  <Dropdown
                    value={activeWorkspace?.id ?? ''}
                    options={workspaces.map((w) => ({
                      id: w.id,
                      label: `${w.name}（${SENSITIVITY_LABELS[w.sensitivity_level ?? DEFAULT_SENSITIVITY]}）`,
                    }))}
                    onChange={setActiveWorkspaceId}
                    disabled={workspaces.length === 0}
                    ariaLabel="选择工作区"
                    align="left"
                    triggerClassName="max-w-[190px] overflow-hidden text-ellipsis text-[12px] text-ink-2"
                  />
                </HoverHint>
                <HoverHint label="选择智能体或对话模式">
                  <Dropdown
                    value={executionMode}
                    options={EXECUTION_MODE_OPTIONS}
                    onChange={(mode) => setExecutionMode(mode as ExecutionMode)}
                    disabled={submitting || uploading}
                    ariaLabel="选择运行模式"
                    align="left"
                    triggerClassName="text-[12px] text-accent"
                  />
                </HoverHint>
              </div>
              <div className="flex-1 min-w-3" />
              <div className="flex items-center gap-3 shrink-0">
                <HoverHint label="选择新任务使用的模型">
                  <Dropdown
                    value={model}
                    options={MODEL_OPTIONS}
                    onChange={setModel}
                    ariaLabel="选择模型"
                    triggerClassName="text-[12.5px] text-ink-2"
                  />
                </HoverHint>
                {showThinkingUi && (
                  <HoverHint label="调整模型的思考深度">
                    <Dropdown
                      value={effort}
                      options={EFFORT_OPTIONS}
                      onChange={setEffort}
                      ariaLabel="调整推理强度"
                      triggerClassName="text-[12.5px] text-ink-2"
                    />
                  </HoverHint>
                )}
                <HoverHint label={submitting ? '正在创建任务' : '发送并创建任务（Enter）'} align="right">
                  <button
                    type="button"
                    onClick={startChat}
                    disabled={submitting || uploading || (!draft.trim() && attachments.length === 0)}
                    className="w-10 h-10 shrink-0 inline-flex items-center justify-center rounded-full bg-accent text-white shadow-[0_6px_16px_-8px_rgba(210,58,31,0.8)] hover:brightness-95 hover:-translate-y-px transition-all disabled:bg-line disabled:text-dim disabled:shadow-none disabled:translate-y-0 disabled:cursor-not-allowed"
                    aria-label={submitting ? '正在创建任务' : '发送'}
                  >
                    {submitting ? (
                      <span aria-hidden className="font-mono text-[13px] animate-pulse">•••</span>
                    ) : (
                      <svg aria-hidden viewBox="0 0 24 24" className="w-5 h-5" fill="none">
                        <path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    )}
                  </button>
                </HoverHint>
              </div>
            </div>
          </div>

          {submitError && (
            <div className="w-[min(720px,95%)] px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {submitError}
            </div>
          )}

          {/* suggestions */}
          <div className="flex gap-2 flex-wrap justify-center max-w-[720px]">
            <span className="cw-label self-center">试试：</span>
            {SUGGESTIONS.map((s) => (
              <Chip key={s} dashed onClick={() => setDraft(s)}>{s}</Chip>
            ))}
          </div>

          {/* divider */}
          <div className="w-[min(960px,95%)] flex items-center gap-3.5 mt-1">
            <div className="flex-1 h-px bg-line" />
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              或选择一个应用
            </span>
            <button
              type="button"
              onClick={() => setShowCreateModal(true)}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              title="从零创建一个应用"
            >
              <span className="font-mono">+</span> 创建应用
            </button>
            <div className="flex-1 h-px bg-line" />
          </div>

          {/* 应用中心 — published apps */}
          <div className="w-[min(960px,95%)] grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            {/* 空白任务 */}
            <div
              onClick={() => setShowCreateModal(true)}
              className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
            >
              <div className="flex items-center gap-2.5 mb-0.5">
                <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
                  +
                </div>
                <div className="text-[14px] font-semibold tracking-[-0.01em]">空白任务</div>
                <div className="flex-1" />
                <span className="font-mono text-[10.5px] text-dim">→</span>
              </div>
              <div className="text-[12px] text-dim leading-[1.45]">
                从零开始 — 用日常语言描述目标。
              </div>
              <div className="flex-1" />
              <div className="flex gap-1 flex-wrap">
                <Chip>自由会话</Chip>
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
                  {app.description || '运行此应用'}
                </div>
                <div className="flex-1" />
                <div className="flex gap-1 flex-wrap">
                  <Chip>应用</Chip>
                </div>
              </div>
            ))}

            {!appsLoading && apps.length === 0 && (
              <div className="border border-line border-dashed bg-surface rounded-md p-3.5 flex flex-col gap-2 items-start justify-center min-h-[120px]">
                <div className="font-mono italic text-dim text-[12px]">暂无已发布应用</div>
              </div>
            )}
          </div>

          {/* 我的应用 divider */}
          <div className="w-[min(960px,95%)] flex items-center gap-3.5 mt-1">
            <div className="flex-1 h-px bg-line" />
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              我的应用
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
                latestVersion?.status === 'draft' ? '草稿'
                : latestVersion?.status === 'pending_review' ? '待审核'
                : latestVersion?.status === 'published' ? '已发布'
                : latestVersion?.status === 'rejected' ? '已拒绝'
                : '草稿';
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
                    {app.description || '点击编辑此应用'}
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
                <div className="font-mono italic text-dim text-[12px]">你还没有创建应用</div>
                <button
                  type="button"
                  onClick={() => setShowCreateModal(true)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                >
                  <span className="font-mono">+</span> 创建应用
                </button>
              </div>
            )}
          </div>
        </div>

        {/* recent activity rail */}
        <div className="border-t border-line bg-paper px-6 py-3 shrink-0 relative">
          <div className="flex items-baseline justify-between mb-2.5">
            <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim">
              最近 · 进行中
            </span>
          </div>
          <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-2.5">
            {sessions.slice(0, 4).map((s) => (
              <div
                key={s.id}
                onClick={() => openSession(s)}
                className="border border-line rounded-md p-3 bg-surface cursor-pointer flex flex-col gap-2 hover:border-ink-2 hover:-translate-y-px transition-all"
              >
                <div className="flex items-center gap-2">
                  <StatusDot status={sessionToDesignStatus(s.status)} />
                  <div className="flex-1" />
                  <span className="font-mono text-[10.5px] text-dim">
                    {formatRelativeTimeZh(s.updated_at)}
                  </span>
                </div>
                <div className="text-[13px] font-medium tracking-[-0.01em] leading-[1.3] truncate">
                  {s.title || '未命名任务'}
                </div>
              </div>
            ))}
            {sessions.length === 0 && (
              <div className="font-mono italic text-dim text-[11px] px-2">暂无任务</div>
            )}
          </div>
        </div>
      </div>

      <CreateAppModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
      />
    </div>
  );
}
