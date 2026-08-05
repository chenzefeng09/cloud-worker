import { useEffect, useState, type ReactNode } from 'react';

// ─── Archive (zip / tar / tar.gz / 7z / rar …) content-listing preview ────────
//
// Lists an archive's INTERNAL file tree WITHOUT extracting any file's contents,
// via libarchive.js (a WASM port of libarchive running in a Web Worker). The
// worker only reads entry metadata (name + uncompressed size) — it never
// materialises file bytes here (we never call extract()). For compressed
// containers (.tar.gz) or compressed headers (7z) the worker does inflate that
// wrapper in memory to reach the listing, but no per-file content is produced.
//
// The worker bundle + wasm are vendored as STATIC assets under
// `public/libarchive/` so the worker (and the wasm it locates next to itself)
// resolve at a stable, same-origin path — see apps/web/public/libarchive/.

const WORKER_URL = '/libarchive/worker-bundle.js';

// Listing feeds the ENTIRE archive blob to the WASM (and inflates the container
// stream for .tar.gz/7z), so guard very large archives to protect memory/CPU.
const MAX_LIST_BYTES = 64 * 1024 * 1024; // 64 MB

// Archive.init is process-global; only call it once.
let _inited = false;

interface Entry {
  path: string; // full path inside the archive, e.g. "docs/readme.md"
  size: number; // uncompressed size in bytes
}

interface FolderNode {
  type: 'folder';
  name: string;
  path: string;
  children: TreeNode[];
  fileCount: number;
}
interface FileNode {
  type: 'file';
  name: string;
  path: string;
  size: number;
}
type TreeNode = FolderNode | FileNode;

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

const FOLDER_GLYPH = '▣';
const FILE_GLYPH: Record<string, string> = {
  md: '◎', json: '{}', yaml: '≡', yml: '≡', csv: '⊞', xlsx: '⊞', xls: '⊞',
  pdf: '▤', pptx: '▣', ppt: '▣', doc: '▦', docx: '▦', txt: '≣',
};
function glyphFor(name: string): string {
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  return FILE_GLYPH[ext] ?? '·';
}

/** Build a sorted folder tree from flat archive entries (split on '/'). */
function buildTree(entries: Entry[]): TreeNode[] {
  const root: FolderNode = { type: 'folder', name: '', path: '', children: [], fileCount: 0 };
  const getFolder = (parent: FolderNode, name: string, path: string): FolderNode => {
    let child = parent.children.find(
      (c): c is FolderNode => c.type === 'folder' && c.name === name,
    );
    if (!child) {
      child = { type: 'folder', name, path, children: [], fileCount: 0 };
      parent.children.push(child);
    }
    return child;
  };
  for (const e of entries) {
    const segs = e.path.split('/').filter(Boolean);
    if (segs.length === 0) continue;
    let cur = root;
    for (let i = 0; i < segs.length - 1; i++) {
      cur = getFolder(cur, segs[i], segs.slice(0, i + 1).join('/'));
    }
    cur.children.push({ type: 'file', name: segs[segs.length - 1], path: e.path, size: e.size });
  }
  const finalize = (node: FolderNode): number => {
    const folders = node.children.filter((c): c is FolderNode => c.type === 'folder');
    const files = node.children.filter((c): c is FileNode => c.type === 'file');
    folders.sort((a, b) => a.name.localeCompare(b.name));
    files.sort((a, b) => a.name.localeCompare(b.name));
    let count = files.length;
    for (const f of folders) count += finalize(f);
    node.children = [...folders, ...files];
    node.fileCount = count;
    return count;
  };
  finalize(root);
  return root.children;
}

function renderNodes(nodes: TreeNode[], depth: number): ReactNode {
  return nodes.map((node) => {
    const indent = depth * 14;
    if (node.type === 'folder') {
      return (
        <div key={`d:${node.path}`}>
          <div
            className="flex items-center gap-1.5 py-[3px]"
            style={{ paddingLeft: indent }}
          >
            <span className="font-mono text-[11px] text-dim shrink-0" aria-hidden>▾</span>
            <span className="font-mono text-[11px] text-dim shrink-0" aria-hidden>{FOLDER_GLYPH}</span>
            <span className="font-mono text-[12px] text-ink truncate">{node.name}/</span>
            <span className="font-mono text-[10px] text-dim shrink-0">({node.fileCount})</span>
          </div>
          {renderNodes(node.children, depth + 1)}
        </div>
      );
    }
    return (
      <div
        key={`f:${node.path}`}
        className="flex items-center gap-1.5 py-[3px]"
        style={{ paddingLeft: indent + 16 }}
      >
        <span className="font-mono text-[11px] text-dim shrink-0" aria-hidden>{glyphFor(node.name)}</span>
        <span className="font-mono text-[12px] text-ink truncate flex-1">{node.name}</span>
        <span className="font-mono text-[10px] text-dim shrink-0">{formatBytes(node.size)}</span>
      </div>
    );
  });
}

interface State {
  loading: boolean;
  error: string | null;
  encrypted: boolean;
  entries: Entry[];
}

export default function ArchivePreview({ blob, filename }: { blob: Blob; filename: string }) {
  const [state, setState] = useState<State>({
    loading: true,
    error: null,
    encrypted: false,
    entries: [],
  });

  useEffect(() => {
    let cancelled = false;
    let archiveRef: { close: () => Promise<void> } | null = null;

    setState({ loading: true, error: null, encrypted: false, entries: [] });

    (async () => {
      if (blob.size > MAX_LIST_BYTES) {
        if (!cancelled) {
          setState({
            loading: false,
            error: `压缩包过大（${formatBytes(blob.size)}），暂不在线列出内容，请下载查看。`,
            encrypted: false,
            entries: [],
          });
        }
        return;
      }
      try {
        const { Archive } = await import('libarchive.js');
        if (!_inited) {
          Archive.init({ workerUrl: WORKER_URL });
          _inited = true;
        }
        const file = new File([blob], filename, { type: 'application/octet-stream' });
        const archive = await Archive.open(file);
        archiveRef = archive;

        let encrypted = false;
        try {
          encrypted = (await archive.hasEncryptedData()) ?? false;
        } catch {
          // some formats can't report this; ignore.
        }

        // Metadata-only listing (no extraction of file contents).
        const arr = await archive.getFilesArray();
        if (cancelled) return;

        const entries: Entry[] = arr
          .map((e) => ({
            path: `${e.path ?? ''}${e.file?.name ?? ''}`,
            size: Number(e.file?.size ?? 0),
          }))
          .filter((e) => e.path);

        setState({ loading: false, error: null, encrypted, entries });
      } catch {
        if (!cancelled) {
          setState({
            loading: false,
            error: '无法读取压缩包内容（可能格式不受支持，或头部已加密）。可下载后用本地工具打开。',
            encrypted: false,
            entries: [],
          });
        }
      } finally {
        try {
          await archiveRef?.close();
        } catch {
          /* worker already terminated */
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [blob, filename]);

  const tree = state.entries.length > 0 ? buildTree(state.entries) : [];
  const totalBytes = state.entries.reduce((s, e) => s + e.size, 0);

  return (
    <div className="flex flex-col gap-2 min-h-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-mono text-[11px] text-dim">
          {state.loading
            ? '读取压缩包目录…'
            : state.error
              ? '压缩包'
              : `${state.entries.length} 个文件 · 解压后共 ${formatBytes(totalBytes)}`}
        </span>
        {state.encrypted && (
          <span className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-accent-line bg-accent-soft text-accent">
            含加密内容
          </span>
        )}
        <span className="font-mono text-[10px] text-dim italic">— 仅列出目录，未解压文件内容 —</span>
      </div>

      {state.loading ? (
        <div className="font-mono text-[11px] text-dim animate-pulse">读取中…</div>
      ) : state.error ? (
        <div className="font-mono text-[11px] text-dim">{state.error}</div>
      ) : state.entries.length === 0 ? (
        <div className="font-mono text-[11px] text-dim italic">压缩包为空或无可列出的文件。</div>
      ) : (
        <div className="flex-1 overflow-auto cw-scroll border border-line rounded-md bg-surface px-2 py-1.5">
          {renderNodes(tree, 0)}
        </div>
      )}
    </div>
  );
}
