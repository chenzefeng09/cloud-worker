import { useCallback, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { api } from '../api/client';
import { ApiError } from '../api/client';
import type { KnowledgeDocument, McpServer, Skill } from '../api/types';
import { formatRelativeTimeZh } from '../lib/format';
import { sha256Hex } from '../lib/sha256';

// Skill tarball upload limits — kept in lockstep with the BFF (20 MiB) and the
// skill-store backend, which also enforces the SKILL.md / gzip checks.
const SKILL_MAX_BYTES = 20 * 1024 * 1024;
const SKILL_ACCEPT = '.tar.gz,.tgz,application/gzip,application/x-gzip';

// Local preflight: reject obviously-bad packages before hitting the network.
// Returns a Chinese error message, or null when the file looks acceptable.
function preflightSkillFile(file: File): string | null {
  const name = file.name.toLowerCase();
  const okExt = name.endsWith('.tar.gz') || name.endsWith('.tgz');
  const okMime =
    file.type === 'application/gzip' || file.type === 'application/x-gzip';
  if (!okExt && !okMime) return '请上传 .tar.gz 格式的技能包';
  if (file.size > SKILL_MAX_BYTES) return '技能包超过大小上限 20 MB';
  return null;
}

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
  const [installOpen, setInstallOpen] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [mcpRes, skillRes, docRes] = await Promise.all([
        api.library.listMcpServers(),
        api.library.listSkills(),
        api.library.listDocuments(),
      ]);
      setMcpServers(mcpRes.items);
      setSkills(skillRes.items);
      setDocs(docRes.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '加载资源库失败，请稍后重试。');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

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
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={() => setInstallOpen(true)}
          >
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

      {installOpen && (
        <InstallModal
          tab={tab}
          onClose={() => setInstallOpen(false)}
          onInstalled={() => {
            setInstallOpen(false);
            void fetchAll();
          }}
        />
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Install modal
// ─────────────────────────────────────────────────────────────────────────────

const INSTALL_TITLE: Record<Tab, string> = {
  mcp: '安装 MCP 工具',
  skills: '安装技能',
  docs: '添加知识源',
};

const inputCls =
  'w-full px-2.5 py-1.5 text-[12.5px] border border-line bg-paper rounded-md ' +
  'focus:outline-none focus:border-accent transition-colors';
const labelCls = 'font-mono text-[11px] text-dim';

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className={labelCls}>{label}</span>
      {children}
    </label>
  );
}

function InstallModal({
  tab,
  onClose,
  onInstalled,
}: {
  tab: Tab;
  onClose: () => void;
  onInstalled: () => void;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  // MCP fields
  const [mcpName, setMcpName] = useState('');
  const [mcpTransport, setMcpTransport] = useState<McpServer['transport']>('http');
  const [mcpEndpoint, setMcpEndpoint] = useState('');
  const [mcpCommand, setMcpCommand] = useState('');
  const [mcpEnabled, setMcpEnabled] = useState(true);

  // Skill fields
  const [skName, setSkName] = useState('');
  const [skDesc, setSkDesc] = useState('');
  const [skVisibility, setSkVisibility] = useState<Skill['visibility']>('private');
  const [skVersion, setSkVersion] = useState('0.1.0');
  const [skFile, setSkFile] = useState<File | null>(null);

  // Document fields
  const [docTitle, setDocTitle] = useState('');
  const [docMime, setDocMime] = useState('');
  const [docFile, setDocFile] = useState<File | null>(null);

  const errMsg = (e: unknown) =>
    e instanceof ApiError ? e.message : '安装失败，请稍后重试。';

  const submit = async () => {
    setErr(null);
    setNote(null);
    setSubmitting(true);
    try {
      if (tab === 'mcp') {
        if (!mcpName.trim()) throw new ApiError(400, 'VALIDATION', '请填写名称');
        if (mcpTransport === 'http' && !mcpEndpoint.trim())
          throw new ApiError(400, 'VALIDATION', '请填写 endpoint');
        if (mcpTransport === 'stdio' && !mcpCommand.trim())
          throw new ApiError(400, 'VALIDATION', '请填写 command');
        await api.library.installMcpServer({
          name: mcpName.trim(),
          transport: mcpTransport,
          endpoint: mcpTransport === 'http' ? mcpEndpoint.trim() : null,
          command: mcpTransport === 'stdio' ? mcpCommand.trim() : null,
          enabled: mcpEnabled,
        });
        onInstalled();
      } else if (tab === 'skills') {
        if (!skName.trim()) throw new ApiError(400, 'VALIDATION', '请填写名称');
        if (!skVersion.trim()) throw new ApiError(400, 'VALIDATION', '请填写版本号');
        if (!skFile) throw new ApiError(400, 'VALIDATION', '请选择技能包文件 (tarball)');
        // Local preflight: catch wrong format / oversized packages before any
        // network round-trip (and before we create the skill record).
        const preflightErr = preflightSkillFile(skFile);
        if (preflightErr) throw new ApiError(400, 'VALIDATION', preflightErr);
        // sha256 of the compressed bytes — the backend re-verifies integrity
        // against this. Optional: if Web Crypto is unavailable we skip it.
        const sha = await sha256Hex(skFile);
        const skill = await api.library.createSkill({
          name: skName.trim(),
          description: skDesc.trim() || null,
          visibility: skVisibility,
        });
        const fd = new FormData();
        fd.append('version', skVersion.trim());
        fd.append('file', skFile, skFile.name);
        if (sha) fd.append('expected_sha256', sha);
        await api.library.uploadSkillVersion(skill.id, fd);
        onInstalled();
      } else {
        if (!docFile) throw new ApiError(400, 'VALIDATION', '请选择文件');
        const title = docTitle.trim() || docFile.name;
        const mime = docMime.trim() || docFile.type || 'text/plain';
        const fd = new FormData();
        fd.append('title', title);
        fd.append('mime', mime);
        fd.append('file', docFile, docFile.name);
        const doc = await api.library.uploadDocument(fd);
        // Trigger ingestion so the document gets chunked and becomes READY.
        const result = await api.library.ingestDocument(doc.id);
        setNote(`已摄入，生成 ${result.chunks_created} 个分块`);
        // Give the user a moment to read the result, then refresh.
        setTimeout(onInstalled, 700);
        return;
      }
    } catch (e) {
      setErr(errMsg(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-[420px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">{INSTALL_TITLE[tab]}</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors"
            onClick={onClose}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          {tab === 'mcp' && (
            <>
              <Field label="名称">
                <input
                  className={inputCls}
                  value={mcpName}
                  onChange={(e) => setMcpName(e.target.value)}
                  placeholder="例如 github-mcp"
                />
              </Field>
              <Field label="传输方式">
                <select
                  className={inputCls}
                  value={mcpTransport}
                  onChange={(e) => setMcpTransport(e.target.value as McpServer['transport'])}
                >
                  <option value="http">http</option>
                  <option value="stdio">stdio</option>
                </select>
              </Field>
              {mcpTransport === 'http' ? (
                <Field label="Endpoint">
                  <input
                    className={inputCls}
                    value={mcpEndpoint}
                    onChange={(e) => setMcpEndpoint(e.target.value)}
                    placeholder="https://example.com/mcp"
                  />
                </Field>
              ) : (
                <Field label="Command">
                  <input
                    className={inputCls}
                    value={mcpCommand}
                    onChange={(e) => setMcpCommand(e.target.value)}
                    placeholder="npx -y @scope/server"
                  />
                </Field>
              )}
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={mcpEnabled}
                  onChange={(e) => setMcpEnabled(e.target.checked)}
                />
                <span className={labelCls}>安装后启用</span>
              </label>
            </>
          )}

          {tab === 'skills' && (
            <>
              <Field label="名称">
                <input
                  className={inputCls}
                  value={skName}
                  onChange={(e) => setSkName(e.target.value)}
                  placeholder="例如 pdf-summarizer"
                />
              </Field>
              <Field label="描述（可选）">
                <input
                  className={inputCls}
                  value={skDesc}
                  onChange={(e) => setSkDesc(e.target.value)}
                  placeholder="简要说明该技能用途"
                />
              </Field>
              <Field label="可见性">
                <select
                  className={inputCls}
                  value={skVisibility}
                  onChange={(e) => setSkVisibility(e.target.value as Skill['visibility'])}
                >
                  <option value="private">私有</option>
                  <option value="tenant">租户</option>
                  <option value="public">公开</option>
                </select>
              </Field>
              <Field label="版本号">
                <input
                  className={inputCls}
                  value={skVersion}
                  onChange={(e) => setSkVersion(e.target.value)}
                  placeholder="0.1.0"
                />
              </Field>
              <Field label="技能包 (tarball)">
                <input
                  type="file"
                  className="text-[12px] text-ink-2 file:mr-2 file:px-2 file:py-1 file:text-[11px] file:font-mono file:border file:border-line file:rounded-md file:bg-paper file:cursor-pointer"
                  accept={SKILL_ACCEPT}
                  onChange={(e) => setSkFile(e.target.files?.[0] ?? null)}
                />
              </Field>
            </>
          )}

          {tab === 'docs' && (
            <>
              <Field label="标题（可选，默认取文件名）">
                <input
                  className={inputCls}
                  value={docTitle}
                  onChange={(e) => setDocTitle(e.target.value)}
                  placeholder="文档标题"
                />
              </Field>
              <Field label="文件">
                <input
                  type="file"
                  className="text-[12px] text-ink-2 file:mr-2 file:px-2 file:py-1 file:text-[11px] file:font-mono file:border file:border-line file:rounded-md file:bg-paper file:cursor-pointer"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null;
                    setDocFile(f);
                    if (f && !docMime) setDocMime(f.type || 'text/plain');
                  }}
                />
              </Field>
              <Field label="MIME 类型">
                <input
                  className={inputCls}
                  value={docMime}
                  onChange={(e) => setDocMime(e.target.value)}
                  placeholder="text/plain"
                />
              </Field>
            </>
          )}

          {err && (
            <div className="px-2.5 py-1.5 border border-accent-line bg-accent-soft rounded-md text-[12px] text-accent">
              {err}
            </div>
          )}
          {note && (
            <div className="px-2.5 py-1.5 border border-line bg-paper rounded-md text-[12px] text-ink-2 font-mono">
              {note}
            </div>
          )}
        </div>

        <div className="flex justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={submitting}>
            取消
          </button>
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={submit}
            disabled={submitting}
          >
            {submitting ? '安装中…' : '安装'}
          </button>
        </div>
      </div>
    </div>
  );
}
