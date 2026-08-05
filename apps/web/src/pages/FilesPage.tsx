import { useEffect, useMemo, useRef, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { useWorkspace } from '../lib/workspaceContext';
import { api, ApiError } from '../api/client';
import type { Attachment, KnowledgeDocument } from '../api/types';
import { useChatStore } from '../stores/chatStore';
import { formatRelativeTimeZh } from '../lib/format';
import { mimeToKind, FilePreviewBody } from '../components/filePreview';

const KIND_GLYPH: Record<string, string> = {
  md: '☷',
  json: '{}',
  yaml: '≡',
  env: '·',
  csv: '⊞',
  pdf: '▤',
  xlsx: '⊞',
  docx: '☱',
  pptx: '▤',
  png: '◰',
  jpg: '◰',
  jpeg: '◰',
  txt: '☷',
  ics: '◷',
  folder: '▸',
};

const DOC_STATUS_ZH: Record<string, string> = {
  PENDING: '待处理',
  INGESTING: '摄入中',
  READY: '就绪',
  FAILED: '失败',
};

function extFromFilename(name: string): string {
  const parts = name.split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

type SelItem =
  | { type: 'doc'; doc: KnowledgeDocument }
  | { type: 'att'; att: Attachment };

export function FilesPage() {
  const { ws } = useWorkspace();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<SelItem | null>(null);

  // Upload state
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);

  // Remote knowledge documents
  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [docsLoading, setDocsLoading] = useState(false);
  const [docsError, setDocsError] = useState<string | null>(null);

  // Session attachments (inputs + outputs)
  const [sessionFiles, setSessionFiles] = useState<Attachment[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesError, setFilesError] = useState<string | null>(null);

  function loadDocs() {
    setDocsLoading(true);
    setDocsError(null);
    api.library.listDocuments().then((res) => {
      setDocs(res.items);
    }).catch((e) => {
      setDocsError(e instanceof ApiError ? e.message : '加载知识文档失败');
    }).finally(() => {
      setDocsLoading(false);
    });
  }

  useEffect(() => {
    loadDocs();
     
  }, []);

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;

    setUploading(true);
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append('title', file.name);
      fd.append('mime', file.type || 'text/plain');
      fd.append('file', file, file.name);
      const doc = await api.library.uploadDocument(fd);
      await api.library.ingestDocument(doc.id);
      loadDocs();
    } catch (e) {
      setUploadError(e instanceof ApiError ? e.message : '上传文档失败');
    } finally {
      setUploading(false);
    }
  }

  useEffect(() => {
    if (!activeSessionId) {
      setSessionFiles([]);
      return;
    }
    let cancelled = false;
    setFilesLoading(true);
    setFilesError(null);

    api.attachments.list(activeSessionId).then((res) => {
      if (cancelled) return;
      setSessionFiles(res.items);
    }).catch((e) => {
      if (cancelled) return;
      setFilesError(e instanceof ApiError ? e.message : '加载会话文件失败');
    }).finally(() => {
      if (!cancelled) setFilesLoading(false);
    });

    return () => { cancelled = true; };
  }, [activeSessionId]);

  const inputFiles = useMemo(() => sessionFiles.filter((f) => f.kind === 'input'), [sessionFiles]);
  const outputFiles = useMemo(() => sessionFiles.filter((f) => f.kind === 'output'), [sessionFiles]);

  const filterDoc = (doc: KnowledgeDocument) => !query.trim() || doc.title.toLowerCase().includes(query.trim().toLowerCase());
  const filterAtt = (att: Attachment) => !query.trim() || att.filename.toLowerCase().includes(query.trim().toLowerCase());

  const totalCount = docs.length + sessionFiles.length;

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: '首页', to: '/home' },
          { label: '我的工作区' },
        ]}
        actions={
          <>
            <input
              ref={uploadInputRef}
              type="file"
              className="hidden"
              onChange={handleUpload}
            />
            <button
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={uploading}
              onClick={() => uploadInputRef.current?.click()}
            >
              {uploading ? '上传中…' : '+ 上传'}
            </button>
          </>
        }
      />

      <div className="grid grid-cols-[260px_1fr] min-h-0 flex-1">
        <aside className="border-r border-line flex flex-col bg-paper">
          <div className="px-3 py-2.5 border-b border-line-2 flex items-center gap-1.5">
            <span className="font-mono text-[11px] text-dim">⌕</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="筛选文件…"
              className="flex-1 border-none bg-transparent outline-none font-mono text-[11.5px] text-ink"
            />
            <span className="font-mono text-[10px] text-dim">{totalCount}</span>
          </div>

          <div className="cw-scroll flex-1 py-1.5">
            {(docsLoading || filesLoading) && (
              <div className="px-3 py-2 font-mono text-[11px] text-dim">加载中…</div>
            )}
            {docsError && (
              <div className="px-3 py-2 font-mono text-[11px] text-accent">{docsError}</div>
            )}
            {uploadError && (
              <div className="px-3 py-2 font-mono text-[11px] text-accent">{uploadError}</div>
            )}
            {filesError && (
              <div className="px-3 py-2 font-mono text-[11px] text-accent">{filesError}</div>
            )}

            {/* Agent artifacts section */}
            {outputFiles.filter(filterAtt).length > 0 && (
              <>
                <div className="cw-label px-3 pt-2 pb-1">
                  Agent 产物 · {outputFiles.filter(filterAtt).length}
                </div>
                {outputFiles.filter(filterAtt).map((att) => {
                  const isSel = selected?.type === 'att' && selected.att.id === att.id;
                  const ext = extFromFilename(att.filename) || mimeToKind(att.mime);
                  return (
                    <div
                      key={`out:${att.id}`}
                      onClick={() => setSelected({ type: 'att', att })}
                      className={`flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${
                        isSel
                          ? 'bg-accent-soft border-accent'
                          : 'border-transparent hover:bg-paper-2'
                      }`}
                    >
                      <span className="font-mono text-[11px] text-dim w-3 text-center">
                        {KIND_GLYPH[ext] ?? '·'}
                      </span>
                      <span className={`font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`}>
                        {att.filename}
                      </span>
                      <span className="font-mono text-[9px] text-dim">出</span>
                    </div>
                  );
                })}
              </>
            )}

            {/* User uploads (inputs) section */}
            {inputFiles.filter(filterAtt).length > 0 && (
              <>
                <div className="cw-label px-3 pt-2 pb-1">
                  会话上传 · {inputFiles.filter(filterAtt).length}
                </div>
                {inputFiles.filter(filterAtt).map((att) => {
                  const isSel = selected?.type === 'att' && selected.att.id === att.id;
                  const ext = extFromFilename(att.filename) || mimeToKind(att.mime);
                  return (
                    <div
                      key={`in:${att.id}`}
                      onClick={() => setSelected({ type: 'att', att })}
                      className={`flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${
                        isSel
                          ? 'bg-accent-soft border-accent'
                          : 'border-transparent hover:bg-paper-2'
                      }`}
                    >
                      <span className="font-mono text-[11px] text-dim w-3 text-center">
                        {KIND_GLYPH[ext] ?? '·'}
                      </span>
                      <span className={`font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`}>
                        {att.filename}
                      </span>
                      <span className="font-mono text-[9px] text-dim">入</span>
                    </div>
                  );
                })}
              </>
            )}

            {/* Knowledge documents section */}
            {!docsLoading && docs.filter(filterDoc).length > 0 && (
              <>
                <div className="cw-label px-3 pt-2 pb-1">
                  知识文档 · {docs.filter(filterDoc).length}
                </div>
                {docs.filter(filterDoc).map((doc) => {
                  const isSel = selected?.type === 'doc' && selected.doc.id === doc.id;
                  const ext = mimeToKind(doc.mime);
                  return (
                    <div
                      key={`doc:${doc.id}`}
                      onClick={() => setSelected({ type: 'doc', doc })}
                      className={`flex items-center gap-1.5 px-3 py-1 cursor-pointer border-l-2 ${
                        isSel
                          ? 'bg-accent-soft border-accent'
                          : 'border-transparent hover:bg-paper-2'
                      }`}
                    >
                      <span className="font-mono text-[11px] text-dim w-3 text-center">
                        {KIND_GLYPH[ext] ?? '·'}
                      </span>
                      <span className={`font-mono text-[12px] truncate flex-1 ${isSel ? 'text-accent font-semibold' : 'text-ink'}`}>
                        {doc.title}
                      </span>
                    </div>
                  );
                })}
              </>
            )}

            {totalCount === 0 && !docsLoading && !filesLoading && (
              <div className="px-3 py-2 font-mono italic text-dim text-[11px]">
                {activeSessionId ? '无文件' : '请先选择一个会话'}
              </div>
            )}
          </div>

          <div className="px-3 py-2.5 border-t border-line-2 font-mono text-[10.5px] text-ink-2 leading-[1.7]">
            <div className="cw-label mb-1.5">
              {ws.name}
            </div>
            {activeSessionId ? (
              <div>{inputFiles.length} 个上传 · {outputFiles.length} 个产物 · {docs.length} 个知识文档</div>
            ) : (
              <div>{docs.length} 个知识文档</div>
            )}
          </div>
        </aside>

        <section className="flex flex-col min-w-0 bg-surface">
          {selected ? (
            selected.type === 'doc' ? (
              <DocDetail doc={selected.doc} />
            ) : (
              <AttDetail att={selected.att} />
            )
          ) : (
            <div className="flex-1 flex items-center justify-center text-dim text-[13px]">
              从左侧选择一个文件查看详情
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function DocDetail({ doc }: { doc: KnowledgeDocument }) {
  return (
    <>
      <div className="px-5 py-3 border-b border-line-2 flex items-center gap-2.5">
        <span className="font-mono text-[10px] text-dim">{KIND_GLYPH[mimeToKind(doc.mime)] ?? '·'}</span>
        <span className="font-mono text-[12.5px] font-semibold">{doc.title}</span>
        <Chip
          accent={doc.status === 'FAILED'}
          dashed={doc.status !== 'READY' && doc.status !== 'FAILED'}
        >
          {DOC_STATUS_ZH[doc.status] ?? doc.status}
        </Chip>
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-dim">{formatRelativeTimeZh(doc.uploaded_at)}</span>
      </div>
      <div className="cw-scroll flex-1 px-6 py-6 bg-paper">
        <div className="max-w-[720px] flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-px bg-line border border-line rounded-md overflow-hidden">
            {[
              { label: 'MIME 类型', value: doc.mime },
              { label: '分块数', value: `${doc.chunk_count} 块` },
              { label: '摄入状态', value: DOC_STATUS_ZH[doc.status] ?? doc.status },
              { label: '上传时间', value: formatRelativeTimeZh(doc.uploaded_at) },
            ].map((row) => (
              <div key={row.label} className="bg-surface px-4 py-3">
                <div className="cw-label mb-1">{row.label}</div>
                <div className="font-mono text-[13px]">{row.value}</div>
              </div>
            ))}
          </div>
          {doc.source_uri && (
            <div className="bg-surface border border-line rounded-md px-4 py-3">
              <div className="cw-label mb-1">来源 URI</div>
              <div className="font-mono text-[12px] text-ink-2 break-all">{doc.source_uri}</div>
            </div>
          )}
          <div className="font-mono text-[11px] text-dim italic">文档 ID · {doc.id}</div>
        </div>
      </div>
    </>
  );
}

function AttDetail({ att }: { att: Attachment }) {
  const ext = att.filename.split('.').pop()?.toLowerCase() ?? '';

  return (
    <>
      <div className="px-5 py-3 border-b border-line-2 flex items-center gap-2.5">
        <span className="font-mono text-[10px] text-dim">{KIND_GLYPH[ext] ?? '·'}</span>
        <span className="font-mono text-[12.5px] font-semibold">{att.filename}</span>
        <Chip accent={att.kind === 'output'} dashed={att.kind !== 'output'}>
          {att.kind === 'output' ? 'Agent 产物' : '用户上传'}
        </Chip>
        <div className="flex-1" />
        <span className="font-mono text-[10.5px] text-dim">
          {formatBytes(att.size_bytes)} · {formatRelativeTimeZh(att.created_at)}
        </span>
      </div>
      <div className="cw-scroll flex-1 px-6 py-6 bg-paper">
        <div className="max-w-[720px] flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-px bg-line border border-line rounded-md overflow-hidden">
            {[
              { label: '文件名', value: att.filename },
              { label: 'MIME 类型', value: att.mime },
              { label: '大小', value: formatBytes(att.size_bytes) },
              { label: '类型', value: att.kind === 'output' ? 'Agent 产物' : '用户上传' },
            ].map((row) => (
              <div key={row.label} className="bg-surface px-4 py-3">
                <div className="cw-label mb-1">{row.label}</div>
                <div className="font-mono text-[13px]">{row.value}</div>
              </div>
            ))}
          </div>

          {/* Preview — delegated to shared FilePreviewBody */}
          <FilePreviewBody
            sessionId={att.session_id}
            fileId={att.id}
            filename={att.filename}
            mime={att.mime}
          />

          <div className="font-mono text-[11px] text-dim italic">附件 ID · {att.id}</div>
          {att.run_id && (
            <div className="font-mono text-[11px] text-dim italic">Run ID · {att.run_id}</div>
          )}
        </div>
      </div>
    </>
  );
}
