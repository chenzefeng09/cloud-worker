import { useEffect, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { api } from '../api/client';
import { ApiError } from '../api/client';
import type { KnowledgeDocument, McpServer, Skill } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';

type Tab = 'mcp' | 'skills' | 'docs';

const TAB_LABELS: Record<Tab, string> = {
  mcp: 'MCP 工具',
  skills: '技能',
  docs: '知识源',
};

const DOC_STATUS_ZH: Record<string, string> = {
  PENDING: '待处理',
  INGESTING: '摄入中',
  READY: '就绪',
  FAILED: '失败',
};

export function LibraryPage() {
  const [tab, setTab] = useState<Tab>('mcp');

  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const fetchAll = async () => {
      try {
        const [mcpRes, skillRes, docRes] = await Promise.all([
          api.library.listMcpServers(),
          api.library.listSkills(),
          api.library.listDocuments(),
        ]);
        if (cancelled) return;
        setMcpServers(mcpRes.items);
        setSkills(skillRes.items);
        setDocs(docRes.items);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : '加载资源库失败，请稍后重试。');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    fetchAll();
    return () => { cancelled = true; };
  }, []);

  const counts: Record<Tab, number> = {
    mcp: mcpServers.length,
    skills: skills.length,
    docs: docs.length,
  };

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[{ label: '首页', to: '/home' }, { label: '资源库' }]}
        actions={
          <button className="cw-btn cw-btn-accent cw-btn-sm">
            + 安装
          </button>
        }
      />

      <div className="cw-scroll flex-1 px-8 py-5">
        <div className="max-w-[960px] flex flex-col gap-4">

          {/* stat tiles */}
          <div className="grid grid-cols-3 gap-px bg-line border border-line rounded-md overflow-hidden">
            {(['mcp', 'skills', 'docs'] as Tab[]).map((t) => (
              <div
                key={t}
                onClick={() => setTab(t)}
                className={`bg-surface px-4 py-3 cursor-pointer hover:bg-paper-2 transition-colors ${tab === t ? 'border-b-2 border-accent' : ''}`}
              >
                <div className="cw-label mb-1">
                  {TAB_LABELS[t]}
                </div>
                <div className="font-mono text-[24px] font-semibold tracking-[-0.01em]">
                  {loading ? '…' : counts[t]}
                </div>
              </div>
            ))}
          </div>

          {/* error */}
          {error && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {error}
            </div>
          )}

          {/* tab nav */}
          <div className="flex gap-0.5 border-b border-line">
            {(['mcp', 'skills', 'docs'] as Tab[]).map((t) => (
              <button
                key={t}
                onClick={() => setTab(t)}
                className={`px-3 py-1.5 font-mono text-[11px] rounded-t-md border-b-2 -mb-px transition-colors ${
                  tab === t
                    ? 'border-accent text-accent bg-paper'
                    : 'border-transparent text-ink-2 hover:text-ink hover:bg-paper-2'
                }`}
              >
                {TAB_LABELS[t]}
                <span className="ml-1.5 text-[10px] text-dim">
                  {loading ? '…' : counts[t]}
                </span>
              </button>
            ))}
          </div>

          {/* loading */}
          {loading && (
            <div className="font-mono text-[11px] text-dim">加载中…</div>
          )}

          {/* MCP servers */}
          {!loading && tab === 'mcp' && (
            <>
              {mcpServers.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">暂无 MCP 工具</div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {mcpServers.map((s) => (
                    <div
                      key={s.id}
                      className="grid grid-cols-[1fr_80px_80px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all"
                    >
                      <div>
                        <div className="text-[13.5px] font-medium">{s.name}</div>
                        <div className="font-mono text-[11px] text-dim truncate">
                          {s.transport === 'http' ? s.endpoint : s.command}
                        </div>
                      </div>
                      <span className="font-mono text-[11px] text-ink-2 uppercase">{s.transport}</span>
                      <Chip accent={s.enabled} dashed={!s.enabled}>
                        {s.enabled ? '已启用' : '已禁用'}
                      </Chip>
                      <span className="font-mono text-[11px] text-dim text-right">
                        {formatRelativeTimeZh(s.updated_at)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* Skills */}
          {!loading && tab === 'skills' && (
            <>
              {skills.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">暂无技能</div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {skills.map((sk) => (
                    <div
                      key={sk.id}
                      className="grid grid-cols-[1fr_80px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all"
                    >
                      <div>
                        <div className="text-[13.5px] font-medium">{sk.name}</div>
                        {sk.description && (
                          <div className="font-mono text-[11px] text-dim truncate">{sk.description}</div>
                        )}
                      </div>
                      <span className="font-mono text-[11px] text-ink-2">
                        {sk.owner ?? '—'}
                      </span>
                      <span className="font-mono text-[11px] text-dim text-right">
                        {formatRelativeTimeZh(sk.created_at)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* Knowledge documents */}
          {!loading && tab === 'docs' && (
            <>
              {docs.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">暂无知识源</div>
              ) : (
                <div className="flex flex-col gap-1.5">
                  {docs.map((d) => (
                    <div
                      key={d.id}
                      className="grid grid-cols-[1fr_80px_60px_100px] gap-3 items-center px-3.5 py-2.5 border border-line bg-surface rounded-md hover:border-ink-2 transition-all"
                    >
                      <div>
                        <div className="text-[13.5px] font-medium">{d.title}</div>
                        <div className="font-mono text-[11px] text-dim truncate">{d.mime}</div>
                      </div>
                      <Chip
                        accent={d.status === 'FAILED'}
                        dashed={d.status !== 'READY' && d.status !== 'FAILED'}
                      >
                        {DOC_STATUS_ZH[d.status] ?? d.status}
                      </Chip>
                      <span className="font-mono text-[11px] text-dim text-center">
                        {d.chunk_count} 块
                      </span>
                      <span className="font-mono text-[11px] text-dim text-right">
                        {formatRelativeTimeZh(d.uploaded_at)}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

        </div>
      </div>
    </div>
  );
}
