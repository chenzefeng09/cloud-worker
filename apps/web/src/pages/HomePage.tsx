import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { useWorkspace } from '../lib/workspaceContext';
import { useSessions } from '../hooks/useSessions';
import { formatRelativeTimeZh } from '../lib/format';
import { StatusDot, sessionToDesignStatus } from '../components/shell/StatusDot';
import { Chip } from '../components/shell/Chip';

const PLACEHOLDERS = [
  '帮我安排明天的团队站会…',
  '把这份 PDF 概括成 5 个要点？',
  '草拟一条回复供应商邮件的中文回信…',
  '迁移到 passkey 的最佳路径是什么？',
];

const SUGGESTIONS = ['帮我规划明天', '概括这份 PDF', '回复供应商邮件', 'Q3 发布命名头脑风暴'];

const TEMPLATES = [
  { id: 'code-refactor', name: '代码重构', desc: '指定仓库，描述变更。工作节点会读取、规划并逐步修改。', glyph: '⌥', kind: 'code' },
  { id: 'research', name: '研究简报', desc: '收集来源、评估并撰写一份结构化的单页报告。', glyph: '◐', kind: 'research' },
  { id: 'deck', name: '演示构建', desc: '把笔记或文档转换成可用于幻灯片的提纲。', glyph: '▤', kind: 'deck' },
  { id: 'inbox', name: '收件箱处理', desc: '整理消息、草拟回复，其余的升级处理。', glyph: '✉', kind: 'ops' },
  { id: 'data', name: '数据整理', desc: 'CSV 输入，整洁的表格 + 摘要输出。', glyph: '⌗', kind: 'data' },
  { id: 'blank', name: '空白任务', desc: '从零开始 — 用日常语言描述目标。', glyph: '+', kind: '—' },
];

export function HomePage() {
  const { ws } = useWorkspace();
  const { sessions, createSession } = useSessions();
  const navigate = useNavigate();
  const [draft, setDraft] = useState('');
  const [phIdx, setPhIdx] = useState(0);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (draft) return;
    const t = setInterval(() => setPhIdx((p) => (p + 1) % PLACEHOLDERS.length), 3200);
    return () => clearInterval(t);
  }, [draft]);

  const startChat = async () => {
    setSubmitError(null);
    setSubmitting(true);
    try {
      const draftText = draft.trim();
      const session = await createSession(draftText || undefined);
      if (session) {
        // Pass the first user message through router state so AgentPage can
        // dispatch it once the chat store is bound to the new session.  Without
        // this the draft was being thrown away.
        navigate(`/tasks/${session.id}?mode=friendly`, { state: { initialMessage: draftText } });
      }
    } catch (err) {
      setSubmitError('创建任务失败，请稍后重试。');
       
      console.warn('create session failed', err);
    } finally {
      setSubmitting(false);
    }
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
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={PLACEHOLDERS[phIdx]}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) startChat();
              }}
              className="w-full min-h-[110px] resize-none border-none outline-none bg-transparent text-ink text-[17px] leading-[1.45] tracking-[-0.01em] px-5 pt-[18px] pb-2"
            />
            <div className="flex items-center gap-1.5 px-3.5 py-2.5 border-t border-line-2 bg-paper">
              <button className="cw-btn cw-btn-ghost cw-btn-sm">
                <span className="font-mono">⊕</span> 文件
              </button>
              <div className="flex-1" />
              <span className="font-mono text-[10.5px] text-dim">sonnet 4.5 · {ws.name}</span>
              <button
                onClick={startChat}
                disabled={submitting}
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
              或从模板开始
            </span>
            <div className="flex-1 h-px bg-line" />
          </div>

          {/* templates */}
          <div className="w-[min(960px,95%)] grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
            {TEMPLATES.map((t) => (
              <div
                key={t.id}
                onClick={() => navigate('/tasks')}
                className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-1.5 min-h-[120px] transition-all hover:border-ink-2 hover:-translate-y-px"
              >
                <div className="flex items-center gap-2.5 mb-0.5">
                  <div className="w-7 h-7 border border-line rounded-sm flex items-center justify-center font-mono text-[13px] text-ink bg-paper">
                    {t.glyph}
                  </div>
                  <div className="text-[14px] font-semibold tracking-[-0.01em]">{t.name}</div>
                  <div className="flex-1" />
                  <span className="font-mono text-[10.5px] text-dim">→</span>
                </div>
                <div className="text-[12px] text-dim leading-[1.45]">{t.desc}</div>
                <div className="flex-1" />
                <div className="flex gap-1 flex-wrap">
                  <Chip>{t.kind}</Chip>
                </div>
              </div>
            ))}
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
                onClick={() => navigate(`/tasks/${s.id}`)}
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
    </div>
  );
}
