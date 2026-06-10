import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { useWorkspace } from '../lib/workspaceContext';
import { useSessions } from '../hooks/useSessions';
import { api } from '../api/client';
import type { Attachment, Session } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
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

const PLACEHOLDERS = [
  '帮我安排明天的团队站会…',
  '把这份 PDF 概括成 5 个要点？',
  '草拟一条回复供应商邮件的中文回信…',
  '迁移到 passkey 的最佳路径是什么？',
];

const SUGGESTIONS = ['帮我规划明天', '概括这份 PDF', '回复供应商邮件', 'Q3 发布命名头脑风暴'];

/** Default glyph for an App card when the App has no icon set. */
const DEFAULT_APP_GLYPH = '▤';

export function HomePage() {
  const { ws } = useWorkspace();
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
    const session = await createSession(draft.trim() || undefined);
    if (session) sessionRef.current = session;
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

  const dateLabel = new Date().toLocaleDateString('zh-CN', {
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
            <Chip>
              <span className="w-1.5 h-1.5 rounded-full bg-accent animate-blip" />
              1 个工作节点运行中
            </Chip>
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
          <div className="w-[min(720px,95%)] bg-surface border border-line rounded-lg shadow-[0_10px_30px_-10px_rgba(15,17,20,0.08)] overflow-hidden shrink-0">
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={PLACEHOLDERS[phIdx]}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !submitting && !uploading)
                  startChat();
              }}
              className="w-full min-h-[110px] resize-none border-none outline-none bg-transparent text-ink text-[17px] leading-[1.45] tracking-[-0.01em] px-5 pt-[18px] pb-2"
            />
            {/* Attachment chips */}
            {(attachments.length > 0 || uploading) && (
              <div className="flex flex-wrap gap-1.5 px-3.5 pt-2 pb-0.5">
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
            <div className="flex items-center gap-1.5 px-3.5 py-2.5 border-t border-line-2 bg-paper">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept="image/*,.pdf,.txt,.csv,.md,.json,.py,.js,.ts,.html,.xml,.yaml,.yml,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.wps,.et,.dps"
                className="hidden"
                onChange={handleFileChange}
                disabled={uploading}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-50"
                title="上传文件或图片"
              >
                <span className="font-mono">⊕</span> 文件
              </button>
              <div className="flex-1" />
              <Dropdown
                value={model}
                options={MODEL_OPTIONS}
                onChange={setModel}
                title="选择模型"
              />
              <span className="text-line-2 text-[10px] select-none">·</span>
              <Dropdown
                value={effort}
                options={EFFORT_OPTIONS}
                onChange={setEffort}
                title="推理强度"
              />
              <span className="font-mono text-[10.5px] text-dim select-none">· {ws.name}</span>
              <button
                onClick={startChat}
                disabled={submitting || uploading}
                className="cw-btn cw-btn-accent cw-btn-sm disabled:opacity-50"
              >
                {submitting ? '创建中…' : '发送 ↵'}
              </button>
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
            <a
              onClick={() => navigate('/tasks')}
              className="cursor-pointer font-mono text-[11px] text-ink-2 hover:text-ink"
            >
              查看全部任务 →
            </a>
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
