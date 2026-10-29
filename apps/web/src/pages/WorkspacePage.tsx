import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
import { Dialog } from '../admin/components/Dialog';
import { useConfirm, useToast } from '../admin/components/dialogs';
import { FilePreviewPanel } from '../components/FilePreviewPanel';
import type { FileContentSource } from '../components/filePreview';
import { useWorkspace } from '../lib/workspaceContext';
import { api, ApiError } from '../api/client';
import { toWorkspaceView } from '../lib/workspaceData';
import { formatRelativeTime } from '../lib/format';
import {
  LevelBadge,
  SENSITIVITY_LABELS,
  SENSITIVITY_ORDER,
  DEFAULT_SENSITIVITY,
  sensitivityRank,
} from '../lib/sensitivity';
import { useChatStore } from '../stores/chatStore';
import type { DlpSensitivity, WorkspaceFile, WorkspaceFileVersion } from '../api/types';

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

/** Map a filename extension to the same mono glyphs the shell uses elsewhere. */
const FILE_GLYPH: Record<string, string> = {
  md: '◎',
  json: '{}',
  yaml: '≡',
  yml: '≡',
  csv: '⊞',
  xlsx: '⊞',
  xls: '⊞',
  pdf: '▤',
  pptx: '▣',
  ppt: '▣',
  doc: '▦',
  docx: '▦',
  txt: '≣',
};

function glyphFor(filename: string): string {
  const ext = filename.split('.').pop()?.toLowerCase() ?? '';
  return FILE_GLYPH[ext] ?? '·';
}

// ─── Row-action icons (14px, stroke = currentColor) ──────────────────────────
const iconProps = {
  width: 15,
  height: 15,
  viewBox: '0 0 24 24',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.9,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  'aria-hidden': true,
};

function DownloadIcon() {
  return (
    <svg {...iconProps}>
      <path d="M12 3.5v11" />
      <path d="m7.5 10.5 4.5 4.5 4.5-4.5" />
      <path d="M5 20h14" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg {...iconProps}>
      <path d="M4 6.5h16" />
      <path d="M9.5 6.5V4.5h5v2" />
      <path d="M6.5 6.5 7.4 19a1.8 1.8 0 0 0 1.8 1.7h5.6a1.8 1.8 0 0 0 1.8-1.7l.9-12.5" />
      <path d="M10 10v6.5M14 10v6.5" />
    </svg>
  );
}

function EnterFolderIcon() {
  return (
    <svg {...iconProps}>
      <path d="M4 8.5h16v9a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 17.5z" />
      <path d="M4 8.5V6.5A1.5 1.5 0 0 1 5.5 5h4l2 2.5" />
      <path d="M9.5 14h5m0 0-2-2m2 2-2 2" />
    </svg>
  );
}

// ─── Virtual-folder tree (frontend-only; folders are implied by '/' in filename) ──

interface FolderNode {
  type: 'folder';
  name: string; // segment name (last path component)
  path: string; // full path, e.g. "docs/sub" ("" at root)
  children: TreeNode[];
  fileCount: number; // recursive count of files under this folder
}
interface FileNode {
  type: 'file';
  name: string; // display name (last path component)
  path: string; // full filename, e.g. "docs/readme.md"
  file: WorkspaceFile;
}
type TreeNode = FolderNode | FileNode;

/**
 * Build a sorted tree from a flat file list by splitting each filename on '/'.
 * Path segments become folders; the last segment is the file's display name.
 * Empty (client-only) folders from localStorage are merged in so the user can
 * create folders before any file lands in them. Within each level: folders
 * first (alphabetical), then files (alphabetical).
 */
function buildTree(files: WorkspaceFile[], emptyFolders: string[]): TreeNode[] {
  const root: FolderNode = { type: 'folder', name: '', path: '', children: [], fileCount: 0 };

  const getOrCreateFolder = (parent: FolderNode, name: string, path: string): FolderNode => {
    let child = parent.children.find(
      (c): c is FolderNode => c.type === 'folder' && c.name === name,
    );
    if (!child) {
      child = { type: 'folder', name, path, children: [], fileCount: 0 };
      parent.children.push(child);
    }
    return child;
  };

  for (const file of files) {
    const segs = file.filename.split('/');
    let current = root;
    for (let i = 0; i < segs.length - 1; i++) {
      const seg = segs[i];
      if (!seg) continue; // tolerate leading/double slashes
      current = getOrCreateFolder(current, seg, segs.slice(0, i + 1).join('/'));
    }
    const name = segs[segs.length - 1];
    current.children.push({ type: 'file', name, path: file.filename, file });
  }

  for (const folderPath of emptyFolders) {
    const segs = folderPath.split('/').filter(Boolean);
    if (segs.length === 0) continue;
    let current = root;
    for (let i = 0; i < segs.length; i++) {
      current = getOrCreateFolder(current, segs[i], segs.slice(0, i + 1).join('/'));
    }
  }

  const finalize = (node: FolderNode): number => {
    const folders = node.children.filter((c): c is FolderNode => c.type === 'folder');
    const fileNodes = node.children.filter((c): c is FileNode => c.type === 'file');
    folders.sort((a, b) => a.name.localeCompare(b.name));
    fileNodes.sort((a, b) => a.name.localeCompare(b.name));
    let count = fileNodes.length;
    for (const f of folders) count += finalize(f);
    node.children = [...folders, ...fileNodes];
    node.fileCount = count;
    return count;
  };
  finalize(root);
  return root.children;
}

/** Extension of a file's *base* name (last path segment), lowercased, or '' if none. */
function extOf(filename: string): string {
  const base = filename.split('/').pop() ?? filename;
  const dot = base.lastIndexOf('.');
  if (dot <= 0 || dot === base.length - 1) return ''; // no ext / leading-dot / trailing-dot
  return base.slice(dot + 1).toLowerCase();
}

// ─── Empty-folder persistence (client-only; backend has no empty-folder concept) ──

const emptyFolderKey = (wsId: string) => `ws-empty-folders:${wsId}`;
function loadEmptyFolders(wsId: string): string[] {
  try {
    const raw = localStorage.getItem(emptyFolderKey(wsId));
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string' && x) : [];
  } catch {
    return [];
  }
}
function saveEmptyFolders(wsId: string, folders: string[]): void {
  try {
    localStorage.setItem(emptyFolderKey(wsId), JSON.stringify(folders));
  } catch {
    /* ignore storage failures (private mode / quota) */
  }
}

/** Which editor dialog (if any) is currently open from the 设置 menu. */
type DialogKind = 'detail' | 'instructions' | 'delete' | null;

export function WorkspacePage() {
  const {
    workspaces,
    activeWorkspace,
    setActiveWorkspaceId,
    files,
    loading,
    createWorkspace,
    updateWorkspace,
    deleteWorkspace,
    uploadFile,
    deleteFile,
  } = useWorkspace();
  const confirm = useConfirm();
  const toast = useToast();

  // ── New-workspace dialog ────────────────────────────────────────────────
  const [showCreate, setShowCreate] = useState(false);

  // ── Center: file upload state ───────────────────────────────────────────
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);

  // ── Version history ─────────────────────────────────────────────────────
  const [versionFile, setVersionFile] = useState<WorkspaceFile | null>(null);

  // ── Settings dropdown + editor dialogs ──────────────────────────────────
  const [menuOpen, setMenuOpen] = useState(false);
  const [dialog, setDialog] = useState<DialogKind>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  // ── Workspace switcher dropdown (replaces the old left list column) ──────
  const [wsMenuOpen, setWsMenuOpen] = useState(false);
  const wsMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!wsMenuOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (wsMenuRef.current && !wsMenuRef.current.contains(e.target as Node)) setWsMenuOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setWsMenuOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [wsMenuOpen]);

  const openDialog = (kind: Exclude<DialogKind, null>) => {
    setMenuOpen(false);
    setDialog(kind);
  };

  // ── Right-side file preview panel ────────────────────────────────────────
  const [previewFile, setPreviewFile] = useState<{ id: string; filename: string; mime?: string } | null>(null);
  // Resizable preview panel width (px). Mirrors AgentPage's drag-resize logic.
  const [previewWidth, setPreviewWidth] = useState(360);
  const PREVIEW_MIN = 280;
  const PREVIEW_MAX = 720;
  const splitContainerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);

  const startPreviewDrag = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    const onMove = (ev: MouseEvent) => {
      if (!draggingRef.current || !splitContainerRef.current) return;
      const rect = splitContainerRef.current.getBoundingClientRect();
      // Preview is on the right; width = distance from cursor to right edge.
      const next = rect.right - ev.clientX;
      setPreviewWidth(Math.min(PREVIEW_MAX, Math.max(PREVIEW_MIN, next)));
    };
    const onUp = () => {
      draggingRef.current = false;
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  // Close the preview when switching workspaces - file ids belong to the
  // previous workspace and would otherwise fetch from the wrong context.
  useEffect(() => setPreviewFile(null), [activeWorkspace?.id]);

  // ── Folder explorer state (tree view / search / type filter / nav) ────────
  // Virtual folders are implied by '/' in filenames; the tree is built purely
  // client-side. `currentFolder` is the upload target (default root). Empty
  // folders (created via 新建文件夹 before any file lands) persist in localStorage
  // keyed by workspace id, since the backend has no empty-folder concept.
  const [currentFolder, setCurrentFolder] = useState('');
  const [expanded, setExpanded] = useState<Set<string> | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [typeFilter, setTypeFilter] = useState('all');
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [emptyFolders, setEmptyFolders] = useState<string[]>([]);
  // Sensitivity level applied to newly uploaded files. Resets to the active
  // workspace's level on switch (a file cannot exceed its workspace's level).
  const [uploadLevel, setUploadLevel] = useState<DlpSensitivity>(DEFAULT_SENSITIVITY);

  // Reset folder nav + filters + empty-folder cache on workspace switch.
  useEffect(() => {
    setCurrentFolder('');
    setExpanded(null);
    setSearchQuery('');
    setTypeFilter('all');
    setEmptyFolders(activeWorkspace ? loadEmptyFolders(activeWorkspace.id) : []);
    setUploadLevel(activeWorkspace?.sensitivity_level ?? DEFAULT_SENSITIVITY);
  }, [activeWorkspace?.id]);

  // Distinct extensions for the type-filter dropdown (extension = '' -> "Other").
  const extensions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const f of files) {
      const key = extOf(f.filename) || 'Other';
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return Array.from(counts.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [files]);

  // Sensitivity levels an uploaded file may take. A file cannot exceed its
  // workspace's level; when the workspace level is undefined, allow all.
  const uploadLevelOptions = useMemo(() => {
    const maxRank = activeWorkspace?.sensitivity_level
      ? sensitivityRank(activeWorkspace.sensitivity_level)
      : Infinity;
    return SENSITIVITY_ORDER.filter((lvl) => sensitivityRank(lvl) <= maxRank).map((lvl) => ({
      id: lvl,
      label: `${lvl} ${SENSITIVITY_LABELS[lvl]}`,
    }));
  }, [activeWorkspace?.sensitivity_level]);

  // Files surviving the type filter. Search is applied on top in searchResults.
  const typeFilteredFiles = useMemo(() => {
    if (typeFilter === 'all') return files;
    return files.filter((f) => (extOf(f.filename) || 'Other') === typeFilter);
  }, [files, typeFilter]);

  // Empty folders that are NOT yet backed by real data (a folder is "backed"
  // once any file lives under it). Backed ones are dropped so they don't
  // double-render; they persist naturally from the file list.
  const effectiveEmptyFolders = useMemo(() => {
    const known = new Set<string>();
    for (const f of files) {
      const segs = f.filename.split('/');
      let p = '';
      for (let i = 0; i < segs.length - 1; i++) {
        p = p ? `${p}/${segs[i]}` : segs[i];
        known.add(p);
      }
    }
    return emptyFolders.filter((p) => !known.has(p));
  }, [emptyFolders, files]);

  // Hide empty folders while a type filter is active - they have no matching
  // files, so per the spec ("folders with no matching files are hidden") they
  // should not show.
  const tree = useMemo(
    () => buildTree(typeFilteredFiles, typeFilter === 'all' ? effectiveEmptyFolders : []),
    [typeFilteredFiles, effectiveEmptyFolders, typeFilter],
  );

  // Flat search results across ALL folders (search ignores tree structure).
  // Composed with the active type filter, if any.
  const searchResults = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return null;
    const pool = typeFilter === 'all' ? files : typeFilteredFiles;
    return pool
      .filter((f) => f.filename.toLowerCase().includes(q))
      .sort((a, b) => a.filename.localeCompare(b.filename));
  }, [files, typeFilteredFiles, searchQuery, typeFilter]);

  // Lazy default expansion: top-level folders expanded, deeper collapsed.
  // Only fires once per workspace (when `expanded` is still null and folders
  // exist); user toggles afterwards are preserved.
  useEffect(() => {
    if (expanded !== null) return;
    const topLevel = tree.filter((n): n is FolderNode => n.type === 'folder').map((n) => n.path);
    if (topLevel.length === 0) return;
    setExpanded(new Set(topLevel));
  }, [expanded, tree]);

  // Auto-expand the path to the current folder so the user can see where they
  // are after navigating into it.
  useEffect(() => {
    if (!currentFolder) return;
    setExpanded((prev) => {
      const next = new Set(prev ?? []);
      const segs = currentFolder.split('/').filter(Boolean);
      let p = '';
      for (const s of segs) {
        p = p ? `${p}/${s}` : s;
        next.add(p);
      }
      return next;
    });
  }, [currentFolder]);

  const expandedSet = expanded ?? new Set<string>();

  const toggleExpand = useCallback((path: string) => {
    setExpanded((prev) => {
      const next = new Set(prev ?? []);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  const navigateInto = useCallback((path: string) => {
    setCurrentFolder(path);
    setExpanded((prev) => {
      const next = new Set(prev ?? []);
      const segs = path.split('/').filter(Boolean);
      let p = '';
      for (const s of segs) {
        p = p ? `${p}/${s}` : s;
        next.add(p);
      }
      return next;
    });
  }, []);

  const handleCreateFolder = useCallback(() => {
    if (!activeWorkspace) return;
    setNewFolderName('');
    setNewFolderOpen(true);
  }, [activeWorkspace]);

  const confirmNewFolder = useCallback(() => {
    if (!activeWorkspace) return;
    const clean = newFolderName.trim().replace(/^\/+|\/+$/g, '');
    if (!clean) return;
    const path = currentFolder ? `${currentFolder}/${clean}` : clean;
    setNewFolderOpen(false);
    if (emptyFolders.includes(path)) return;
    const next = [...emptyFolders, path];
    setEmptyFolders(next);
    saveEmptyFolders(activeWorkspace.id, next);
    // Expand the new folder's ancestors so it's visible immediately.
    navigateInto(path);
  }, [activeWorkspace, newFolderName, currentFolder, emptyFolders, navigateInto]);

  const doUpload = async (list: FileList | File[]) => {
    const arr = Array.from(list);
    if (!arr.length || !activeWorkspace) return;
    setFileError(null);
    setUploading(true);
    try {
      for (const f of arr) await uploadFile(f, undefined, currentFolder, uploadLevel);
    } catch (err) {
      setFileError(err instanceof ApiError ? err.message : 'File upload failed. Please try again later.');
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = '';
    }
  };

  const handleDelete = async (file: WorkspaceFile) => {
    const ok = await confirm({
      title: 'Delete file',
      message: `Delete “${file.filename}”? This can’t be undone.`,
      tone: 'danger',
      confirmText: 'Delete',
    });
    if (!ok) return;
    setFileError(null);
    if (previewFile?.id === file.id) setPreviewFile(null);
    try {
      await deleteFile(file.id);
    } catch (err) {
      setFileError(err instanceof ApiError ? err.message : 'Delete failed. Please try again later.');
    }
  };

  const handleDownload = (file: WorkspaceFile) => {
    if (!activeWorkspace) return;
    window.open(api.workspaces.fileContentUrl(activeWorkspace.id, file.id), '_blank');
  };

  // ── Tree / search-row rendering (closes over handlers above) ─────────────
  // `name` is the display name: the last path segment in tree view, or the
  // full path when rendering flat search results. `indent` is the left padding
  // (px) driving per-depth indentation; a 10px spacer aligns file glyphs with
  // the folder chevron column.
  const renderFileRow = (f: WorkspaceFile, name: string, indent: number): ReactNode => (
    <div
      key={f.id}
      onClick={() => setPreviewFile({ id: f.id, filename: f.filename, mime: f.mime })}
      className={`group grid py-[10px] gap-[10px] items-center border-b border-line-2 hover:bg-paper-2 transition-colors cursor-pointer ${
        previewFile?.id === f.id ? 'bg-paper-2' : ''
      }`}
      style={{ paddingLeft: indent, paddingRight: 22, gridTemplateColumns: '1fr 88px 64px 72px 56px' }}
    >
      <div className="flex items-center gap-1.5 min-w-0">
        <span className="w-[10px] shrink-0" aria-hidden />
        <span className="font-mono text-dim text-[12px] shrink-0">{glyphFor(f.filename)}</span>
        <div className="min-w-0">
          <div className="font-mono text-[12.5px] text-ink font-medium truncate">{name}</div>
          {f.description && (
            <div className="font-mono text-[11px] text-dim truncate mt-0.5">{f.description}</div>
          )}
        </div>
      </div>
      <span className="font-mono text-[11px] text-dim text-right">{formatBytes(f.size_bytes)}</span>
      <div className="flex items-center justify-end">
        <LevelBadge level={f.sensitivity_level} small />
      </div>
      <span className="font-mono text-[11px] text-dim text-right">
        {formatRelativeTime(f.created_at)}
      </span>
      <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        {f.current_version != null && f.current_version > 1 && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setVersionFile(f);
            }}
            className="inline-flex items-center justify-center w-[26px] h-[26px] rounded-md text-dim hover:text-ink hover:bg-line-2 transition-colors"
            title="Version history"
            aria-label="Version history"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M8 3v5l3 2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5"/></svg>
          </button>
        )}
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            handleDownload(f);
          }}
          className="inline-flex items-center justify-center w-[26px] h-[26px] rounded-md text-dim hover:text-ink hover:bg-line-2 transition-colors"
          title="Download"
          aria-label="Download"
        >
          <DownloadIcon />
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            handleDelete(f);
          }}
          className="inline-flex items-center justify-center w-[26px] h-[26px] rounded-md text-dim hover:text-accent hover:bg-accent-soft transition-colors"
          title="Delete"
          aria-label="Delete"
        >
          <TrashIcon />
        </button>
      </div>
    </div>
  );

  const renderNode = (node: TreeNode, depth: number): ReactNode => {
    const indent = 22 + depth * 18;
    if (node.type === 'folder') {
      const isOpen = expandedSet.has(node.path);
      const isActiveFolder = currentFolder === node.path;
      return (
        <Fragment key={`folder:${node.path}`}>
          <div
            onClick={() => toggleExpand(node.path)}
            className={`group grid py-[10px] gap-[10px] items-center border-b border-line-2 hover:bg-paper-2 transition-colors cursor-pointer ${
              isActiveFolder ? 'bg-paper-2' : ''
            }`}
            style={{ paddingLeft: indent, paddingRight: 22, gridTemplateColumns: '1fr 88px 64px 72px 56px' }}
          >
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="font-mono text-[10px] text-dim w-[10px] shrink-0" aria-hidden>
                {isOpen ? '▾' : '▸'}
              </span>
              <span className="font-mono text-dim text-[12px] shrink-0" aria-hidden>▣</span>
              <span className="font-mono text-[12.5px] text-ink font-medium truncate">{node.name}</span>
              <span className="font-mono text-[11px] text-dim shrink-0">({node.fileCount})</span>
            </div>
            <span aria-hidden />
            <span aria-hidden />
            <span aria-hidden />
            <div className="flex items-center justify-end gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  navigateInto(node.path);
                }}
                className="inline-flex items-center justify-center w-[26px] h-[26px] rounded-md text-dim hover:text-ink hover:bg-line-2 transition-colors"
                title="Set as upload folder"
                aria-label="Set as upload folder"
              >
                <EnterFolderIcon />
              </button>
            </div>
          </div>
          {isOpen && node.children.map((c) => renderNode(c, depth + 1))}
        </Fragment>
      );
    }
    return renderFileRow(node.file, node.name, indent);
  };

  // Content-source override for the preview panel: workspace files live under
  // /workspaces/{id}/files/{fileId}/content, not the session path. Only built
  // when a workspace is active; undefined otherwise (panel not rendered then).
  const getSource = activeWorkspace
    ? (fileId: string): FileContentSource => ({
        url: api.workspaces.fileContentUrl(activeWorkspace.id, fileId),
        getContent: () => api.workspaces.getContent(activeWorkspace.id, fileId),
      })
    : undefined;

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead crumbs={[{ label: 'Home', to: '/home' }, { label: 'Workspace' }]} />

      <div
        ref={splitContainerRef}
        className="grid min-h-0 flex-1 overflow-hidden"
        style={{
          gridTemplateColumns:
            previewFile && activeWorkspace
              ? `1fr 6px ${previewWidth}px`
              : '1fr',
          // Explicit row size so the preview panel's `h-full` resolves to the
          // full grid height (a stretched `auto` row isn't definite enough for
          // percentage heights, so the panel would otherwise collapse to content).
          gridTemplateRows: 'minmax(0, 1fr)',
        }}
      >
        {/* ── CENTER: header + base files ─────────────────────────────── */}
        <section className="flex flex-col min-w-0 overflow-hidden">
          {/* Workspace header bar with 设置 dropdown */}
          <div className="px-[22px] py-3 border-b border-line-2 flex items-center gap-3">
            {activeWorkspace ? (
              <span
                className="inline-flex w-[34px] h-[34px] items-center justify-center rounded-md font-mono text-[15px] shrink-0 border"
                style={{
                  background: toWorkspaceView(activeWorkspace).accent + '14',
                  borderColor: toWorkspaceView(activeWorkspace).accent + '55',
                  color: toWorkspaceView(activeWorkspace).accent,
                }}
              >
                {toWorkspaceView(activeWorkspace).glyph}
              </span>
            ) : (
              <span className="inline-flex w-[34px] h-[34px] items-center justify-center rounded-md font-mono text-[15px] shrink-0 border border-line-2 text-dim">
                ◇
              </span>
            )}
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <div ref={wsMenuRef} className="relative min-w-0">
                  <button
                    type="button"
                    onClick={() => setWsMenuOpen((o) => !o)}
                    aria-haspopup="menu"
                    aria-expanded={wsMenuOpen}
                    title="Switch workspace"
                    className="group inline-flex items-center gap-1.5 min-w-0 max-w-full -mx-1 px-1 py-0.5 rounded-sm hover:bg-paper-2 transition-colors cursor-pointer"
                  >
                    <span className="font-display text-[18px] font-normal tracking-[-0.02em] truncate">
                      {activeWorkspace?.name ?? 'Workspace'}
                    </span>
                    <span
                      aria-hidden
                      className="font-mono text-[10px] text-dim shrink-0 group-hover:text-ink-2"
                    >
                      ▾
                    </span>
                  </button>

                  {wsMenuOpen && (
                    <div
                      role="menu"
                      className="absolute top-full left-0 mt-1 z-40 bg-surface border border-line rounded-md shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 w-[280px] max-h-[60vh] overflow-auto cw-scroll"
                    >
                      {workspaces.map((w) => {
                        const view = toWorkspaceView(w);
                        const active = w.id === activeWorkspace?.id;
                        return (
                          <button
                            key={w.id}
                            type="button"
                            role="menuitemradio"
                            aria-checked={active}
                            onClick={() => {
                              setActiveWorkspaceId(w.id);
                              setWsMenuOpen(false);
                            }}
                            className={`w-full flex items-center gap-2.5 px-2 py-1.5 rounded-md text-left transition-colors ${
                              active ? 'bg-paper-2' : 'hover:bg-paper-2'
                            }`}
                          >
                            <span
                              className="inline-flex w-[22px] h-[22px] items-center justify-center rounded-sm font-mono text-[11px] shrink-0 border"
                              style={{
                                background: view.accent + '14',
                                borderColor: view.accent + '55',
                                color: view.accent,
                              }}
                            >
                              {view.glyph}
                            </span>
                            <div className="flex-1 min-w-0">
                              <div
                                className="text-[12.5px] tracking-[-0.01em] truncate flex items-center gap-1.5"
                                style={{ fontWeight: active ? 600 : 500 }}
                              >
                                <span className="truncate">{w.name}</span>
                                {w.is_default && (
                                  <span className="font-mono text-[9px] text-dim shrink-0">
                                    Default
                                  </span>
                                )}
                                <LevelBadge level={w.sensitivity_level} small />
                              </div>
                              <div className="font-mono text-[10px] text-dim truncate">
                                {w.file_count} {w.file_count === 1 ? 'file' : 'files'}
                              </div>
                            </div>
                            {active && (
                              <span
                                aria-hidden
                                className="font-mono text-[10px] text-accent shrink-0"
                              >
                                ✓
                              </span>
                            )}
                          </button>
                        );
                      })}
                      <div className="my-1 border-t border-line-2" />
                      <button
                        type="button"
                        role="menuitem"
                        onClick={() => {
                          setWsMenuOpen(false);
                          setShowCreate(true);
                        }}
                        className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[12.5px] text-ink-2 hover:bg-paper-2 hover:text-ink transition-colors"
                      >
                        <span className="font-mono">+</span> New workspace
                      </button>
                    </div>
                  )}
                </div>
                {activeWorkspace?.is_default && (
                  <span className="font-mono text-[9.5px] tracking-[0.06em] px-1.5 py-0.5 rounded border border-line-2 text-dim shrink-0">
                    Default
                  </span>
                )}
                <LevelBadge level={activeWorkspace?.sensitivity_level} />
                {loading && (
                  <span className="font-mono text-[10px] text-dim animate-pulse shrink-0">
                    Loading…
                  </span>
                )}
              </div>
              <div className="font-mono text-[11px] text-dim">
                {activeWorkspace?.file_count ?? 0} files total
              </div>
            </div>

            {/* Settings dropdown */}
            <div ref={menuRef} className="relative shrink-0">
              <button
                type="button"
                onClick={() => setMenuOpen((o) => !o)}
                aria-haspopup="menu"
                aria-expanded={menuOpen}
                disabled={!activeWorkspace}
                title={activeWorkspace ? 'Workspace settings' : 'No active workspace'}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 border border-line bg-surface rounded-sm text-[12.5px] text-ink-2 hover:border-ink-2 hover:text-ink hover:bg-paper-2 transition-colors cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Settings <span aria-hidden className="font-mono text-[9px]">▾</span>
              </button>

              {menuOpen && (
                <div
                  role="menu"
                  className="absolute top-full right-0 mt-1 z-40 bg-surface border border-line rounded-md shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 w-[200px]"
                >
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => openDialog('detail')}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[12.5px] text-ink-2 hover:bg-paper-2 hover:text-ink transition-colors"
                  >
                    Workspace details
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => openDialog('instructions')}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[12.5px] text-ink-2 hover:bg-paper-2 hover:text-ink transition-colors"
                  >
                    Workspace instructions
                  </button>
                  <div className="my-1 border-t border-line-2" />
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => openDialog('delete')}
                    disabled={activeWorkspace?.is_default}
                    title={activeWorkspace?.is_default ? 'The default workspace can’t be deleted' : 'Delete this workspace'}
                    className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-[12.5px] text-accent hover:bg-accent-soft transition-colors disabled:opacity-40 disabled:cursor-not-allowed disabled:text-dim disabled:hover:bg-transparent"
                  >
                    Delete workspace
                  </button>
                </div>
              )}
            </div>
          </div>

          <div className="px-[22px] py-3.5 border-b border-line-2">
            <div className="flex items-baseline gap-2.5 mb-1.5 flex-wrap">
              <h1 className="m-0 font-display text-[22px] font-normal tracking-[-0.02em] whitespace-nowrap">
                My workspace
              </h1>
              <Chip dashed>Always loaded as context</Chip>
            </div>
            <p className="m-0 text-[12.5px] text-dim leading-[1.5]">
              Every task started in <b className="text-ink-2">{activeWorkspace?.name ?? 'Workspace'}</b>{' '}
              gets these files as base context — plus any files uploaded when the task starts.
            </p>
          </div>

          {/* Drop-zone / upload — kept above the list so it stays reachable
              even when the file list grows long. */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => e.target.files && doUpload(e.target.files)}
            disabled={uploading || !activeWorkspace}
          />
          <div
            onClick={() => !uploading && activeWorkspace && fileInputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOver(true);
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragOver(false);
              if (e.dataTransfer.files?.length) doUpload(e.dataTransfer.files);
            }}
            className={`mx-[22px] my-3 border border-dashed rounded-[var(--r-sm)] px-4 py-3 flex items-center justify-center gap-2 text-[12px] font-mono select-none cursor-pointer transition-colors ${
              dragOver
                ? 'border-accent bg-accent-soft text-accent'
                : 'border-line text-dim hover:border-ink-2 hover:text-ink-2'
            } ${uploading || !activeWorkspace ? 'opacity-60 cursor-not-allowed' : ''}`}
          >
            {uploading ? (
              <span className="animate-pulse">Uploading…</span>
            ) : (
              <span className="flex items-center gap-2">
                Drop files into “{currentFolder || 'Root'}”, or click to upload
                {currentFolder && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setCurrentFolder('');
                    }}
                    className="text-dim hover:text-accent transition-colors"
                    title="Back to root"
                  >
                    ✕
                  </button>
                )}
              </span>
            )}
          </div>

          {/* Toolbar: search · type filter · current-folder breadcrumb + 新建文件夹 */}
          <div className="mx-[22px] mb-3 flex items-center gap-2 flex-wrap">
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search files…"
              className="flex-1 min-w-[140px] border border-line bg-paper rounded-sm px-2.5 py-1.5 text-[12.5px] font-mono text-ink placeholder:text-dim focus:outline-none focus:border-accent"
            />
            <select
              value={typeFilter}
              onChange={(e) => setTypeFilter(e.target.value)}
              className="border border-line bg-paper rounded-sm px-2 py-1.5 text-[12px] font-mono text-ink-2 focus:outline-none focus:border-accent cursor-pointer"
              title="Filter by file type"
            >
              <option value="all">All types</option>
              {extensions.map(([ext, count]) => (
                <option key={ext} value={ext}>
                  {ext} ({count})
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={handleCreateFolder}
              disabled={!activeWorkspace}
              title="Create an empty folder in the current folder"
              className="inline-flex items-center px-2.5 py-1.5 border border-line bg-surface rounded-sm text-[12.5px] text-ink-2 hover:border-ink-2 hover:text-ink hover:bg-paper-2 transition-colors shrink-0 disabled:opacity-40 disabled:cursor-not-allowed"
            >
              New folder
            </button>
            <div className="min-w-[120px] shrink-0">
              <Dropdown
                value={uploadLevel}
                options={uploadLevelOptions}
                onChange={(id) => setUploadLevel(id as DlpSensitivity)}
                disabled={!activeWorkspace}
                title="Sensitivity level for uploaded files (can’t exceed the workspace level)"
                dropDirection="down"
                fullWidth
              />
            </div>
          </div>

          <div className="flex-1 overflow-auto cw-scroll">
            {fileError && (
              <div className="mx-[22px] mt-3 px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
                {fileError}
              </div>
            )}

            {/* Column header (4-col grid matches rows: name · size · created · actions) */}
            <div
              className="grid px-[22px] py-1.5 border-b border-line-2 gap-[10px] items-center sticky top-0 bg-paper z-10"
              style={{ gridTemplateColumns: '1fr 88px 64px 72px 56px' }}
            >
              <span className="cw-label">Name · Description</span>
              <span className="cw-label text-right">Size</span>
              <span className="cw-label text-right">Level</span>
              <span className="cw-label text-right">Updated</span>
              <span />
            </div>

            {searchResults ? (
              // ── Search active: flat list of matches with full path ──
              searchResults.length === 0 ? (
                <div className="px-[22px] py-8 text-center text-dim text-[12.5px] font-mono">
                  No matching files
                </div>
              ) : (
                searchResults.map((f) => renderFileRow(f, f.filename, 22))
              )
            ) : files.length === 0 && effectiveEmptyFolders.length === 0 && !uploading ? (
              <div className="px-[22px] py-8 text-center text-dim text-[12.5px] font-mono">
                No files in this workspace yet
              </div>
            ) : (
              // ── Tree view (folders expand/collapse; files are leaves) ──
              tree.map((node) => renderNode(node, 0))
            )}

            {/* Drop-zone moved above the list (stays visible when list is long) */}
          </div>

          <div className="px-[22px] py-2 border-t border-line-2 bg-paper">
            <span className="cw-label">My workspace · {files.length}</span>
          </div>
        </section>

        {previewFile && activeWorkspace && (
          <>
            {/* Draggable divider */}
            <div
              onMouseDown={startPreviewDrag}
              role="separator"
              aria-orientation="vertical"
              aria-label="Drag to resize the preview pane"
              title="Drag to resize the preview pane"
              className="relative flex items-center justify-center"
              style={{ cursor: 'col-resize', userSelect: 'none' }}
              onMouseEnter={(e) => {
                const bar = e.currentTarget.querySelector('[data-bar]') as HTMLElement | null;
                if (bar) { bar.style.width = '3px'; bar.style.background = 'var(--accent)'; }
              }}
              onMouseLeave={(e) => {
                if (draggingRef.current) return;
                const bar = e.currentTarget.querySelector('[data-bar]') as HTMLElement | null;
                if (bar) { bar.style.width = '1px'; bar.style.background = 'var(--line)'; }
              }}
            >
              <span
                data-bar
                className="h-full transition-all"
                style={{ width: 1, background: 'var(--line)' }}
              />
            </div>
            <FilePreviewPanel
              getSource={getSource}
              file={previewFile}
              onClose={() => setPreviewFile(null)}
            />
          </>
        )}
      </div>

      {/* ── Editor dialogs (launched from 设置) ────────────────────────── */}
      {dialog === 'detail' && activeWorkspace && (
        <Dialog title="Workspace details" onClose={() => setDialog(null)} className="w-full max-w-[520px]">
          <DetailRail
            key={activeWorkspace.id}
            name={activeWorkspace.name}
            description={activeWorkspace.description ?? ''}
            isDefault={activeWorkspace.is_default}
            wsId={activeWorkspace.id}
            accent={toWorkspaceView(activeWorkspace).accent}
            glyph={toWorkspaceView(activeWorkspace).glyph}
            sensitivityLevel={activeWorkspace.sensitivity_level}
            onSave={async (body) => {
              await updateWorkspace(activeWorkspace.id, body);
              setDialog(null);
            }}
          />
        </Dialog>
      )}

      {dialog === 'instructions' && activeWorkspace && (
        <Dialog title="Workspace instructions" onClose={() => setDialog(null)} className="w-full max-w-[560px]">
          <InstructionsCard
            key={activeWorkspace.id}
            initial={activeWorkspace.instructions ?? ''}
            onSave={async (instructions) => {
              await updateWorkspace(activeWorkspace.id, { instructions });
              setDialog(null);
            }}
          />
        </Dialog>
      )}

      {dialog === 'delete' && activeWorkspace && (
        <DeleteWorkspaceDialog
          key={activeWorkspace.id}
          name={activeWorkspace.name}
          onClose={() => setDialog(null)}
          onConfirm={async () => {
            try {
              await deleteWorkspace(activeWorkspace.id);
              return true;
            } catch (err) {
              toast.error(
                err instanceof ApiError ? err.message : 'Failed to delete the workspace. Please try again later.',
              );
              return false;
            }
          }}
        />
      )}

      {showCreate && (
        <CreateWorkspaceDialog
          onClose={() => setShowCreate(false)}
          onCreate={async (name, description, level) => {
            await createWorkspace({ name, description, sensitivity_level: level });
            setShowCreate(false);
          }}
        />
      )}

      {newFolderOpen && activeWorkspace && (
        <Dialog title="New folder" onClose={() => setNewFolderOpen(false)} className="w-full max-w-[420px]">
          <div className="flex flex-col gap-3">
            <input
              autoFocus
              value={newFolderName}
              onChange={(e) => setNewFolderName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && newFolderName.trim()) confirmNewFolder();
              }}
              placeholder="Folder name (use / for nesting, e.g. docs/subfolder)"
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
            />
            <div className="flex justify-end gap-2">
              <button type="button" onClick={() => setNewFolderOpen(false)} className="cw-btn cw-btn-sm">
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmNewFolder}
                disabled={!newFolderName.trim()}
                className="cw-btn cw-btn-primary cw-btn-sm disabled:opacity-50"
              >
                Create
              </button>
            </div>
          </div>
        </Dialog>
      )}

      {versionFile && activeWorkspace && (
        <VersionHistoryDialog
          workspaceId={activeWorkspace.id}
          file={versionFile}
          onClose={() => setVersionFile(null)}
          onRestored={() => {
            setVersionFile(null);
            toast.success('Version restored');
          }}
        />
      )}
    </div>
  );
}

// ─── 工作区指令 editor (dialog body) ───────────────────────────────────────
function InstructionsCard({
  initial,
  onSave,
}: {
  initial: string;
  onSave: (value: string) => Promise<unknown>;
}) {
  const [value, setValue] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const dirty = value !== initial;

  const save = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await onSave(value);
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-2.5">
      <Chip dashed>Injected at the start of every task</Chip>
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="e.g. Reply in English, comment all code, prefer reusing existing components…"
        className="w-full min-h-[140px] resize-y border border-line rounded-sm px-2.5 py-2 text-[13px] leading-[1.55] bg-paper focus:outline-none focus:border-accent"
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={save}
          disabled={saving || !dirty}
          className="cw-btn cw-btn-primary cw-btn-sm disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save instructions'}
        </button>
        {saved && <span className="font-mono text-[11px] text-dim">Saved ✓</span>}
      </div>
    </div>
  );
}

// ─── 工作区详情 editor (dialog body) ───────────────────────────────────────
function DetailRail({
  name,
  description,
  isDefault,
  wsId,
  accent,
  glyph,
  sensitivityLevel,
  onSave,
}: {
  name: string;
  description: string;
  isDefault: boolean;
  wsId: string;
  accent: string;
  glyph: string;
  sensitivityLevel?: DlpSensitivity;
  onSave: (body: {
    name?: string;
    description?: string;
    sensitivity_level?: DlpSensitivity;
  }) => Promise<unknown>;
}) {
  const clearance = useChatStore((s) => s.userProfile?.clearance);
  const [n, setN] = useState(name);
  const [d, setD] = useState(description);
  const [level, setLevel] = useState<DlpSensitivity>(sensitivityLevel ?? DEFAULT_SENSITIVITY);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initialLevel = sensitivityLevel ?? DEFAULT_SENSITIVITY;
  const dirty = n !== name || d !== description || level !== initialLevel;

  // Sensitivity levels this user may set on a workspace (≤ their clearance).
  // When clearance is undefined, allow all (gating happens server-side).
  const levelOptions = useMemo(() => {
    const maxRank = clearance ? sensitivityRank(clearance) : Infinity;
    return SENSITIVITY_ORDER.filter((lvl) => sensitivityRank(lvl) <= maxRank).map((lvl) => ({
      id: lvl,
      label: `${lvl} ${SENSITIVITY_LABELS[lvl]}`,
    }));
  }, [clearance]);

  const save = async () => {
    if (!n.trim()) return;
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      await onSave({ name: n.trim(), description: d, sensitivity_level: level });
      setSaved(true);
      setTimeout(() => setSaved(false), 1800);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed. Please try again later.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      {error && (
        <div className="border border-accent-line bg-accent-soft rounded-md px-3 py-2 text-[13px] text-accent">
          {error}
        </div>
      )}
      {/* Identity */}
      <div className="flex items-center gap-3">
        <span
          className="inline-flex w-[38px] h-[38px] items-center justify-center rounded-md font-mono text-[17px] shrink-0 border"
          style={{ background: accent + '14', borderColor: accent + '55', color: accent }}
        >
          {glyph}
        </span>
        <div className="min-w-0 flex items-center gap-2">
          <div className="font-display text-[17px] tracking-[-0.02em] truncate">{n || name}</div>
          {isDefault && (
            <span className="font-mono text-[9.5px] tracking-[0.06em] px-1.5 py-0.5 rounded border border-line-2 text-dim shrink-0">
              Default
            </span>
          )}
        </div>
      </div>

      {/* Editable name/description */}
      <div className="flex flex-col gap-2">
        <div className="flex flex-col gap-1">
          <label className="cw-label">Name</label>
          <input
            value={n}
            onChange={(e) => setN(e.target.value)}
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="cw-label">Description</label>
          <textarea
            value={d}
            onChange={(e) => setD(e.target.value)}
            className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper min-h-[72px] resize-y focus:outline-none focus:border-accent"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="cw-label">Sensitivity level</label>
          <Dropdown
            value={level}
            options={levelOptions}
            onChange={(id) => setLevel(id as DlpSensitivity)}
            title="Workspace sensitivity level"
            dropDirection="down"
            fullWidth
          />
          {clearance && (
            <span className="font-mono text-[10px] text-dim">Available levels are limited by your clearance</span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={save}
            disabled={saving || !dirty || !n.trim()}
            className="cw-btn cw-btn-primary cw-btn-sm disabled:opacity-50"
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
          {saved && <span className="font-mono text-[11px] text-dim">Saved ✓</span>}
        </div>
      </div>

      {/* Workspace id (read-only) */}
      <div className="pt-3 border-t border-line-2">
        <div className="cw-label mb-2">Workspace ID</div>
        <code className="font-mono text-[11px] text-dim bg-paper-2 border border-line rounded px-1.5 py-0.5 select-all break-all">
          {wsId}
        </code>
      </div>
    </div>
  );
}

// ─── Delete-workspace confirm dialog ───────────────────────────────────────
function DeleteWorkspaceDialog({
  name,
  onClose,
  onConfirm,
}: {
  name: string;
  onClose: () => void;
  /** Resolve true if the workspace was deleted (close on success); false on error (keep open). */
  onConfirm: () => Promise<boolean>;
}) {
  const [deleting, setDeleting] = useState(false);

  const confirm = async () => {
    setDeleting(true);
    try {
      const ok = await onConfirm();
      if (ok) onClose();
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Dialog
      title="Delete workspace"
      onClose={() => !deleting && onClose()}
      dismissable={!deleting}
      className="w-full max-w-[420px]"
    >
      <div className="text-[13px] text-ink leading-relaxed">
        Delete workspace “<b>{name}</b>”? This can’t be undone.
      </div>
      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onClose}
          disabled={deleting}
          className="cw-btn cw-btn-sm"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={confirm}
          disabled={deleting}
          className="cw-btn cw-btn-accent cw-btn-sm"
        >
          {deleting ? 'Deleting…' : 'Delete'}
        </button>
      </div>
    </Dialog>
  );
}

// ─── Create-workspace dialog ───────────────────────────────────────────────
function CreateWorkspaceDialog({
  onClose,
  onCreate,
}: {
  onClose: () => void;
  onCreate: (name: string, description: string, level?: DlpSensitivity) => Promise<void>;
}) {
  const clearance = useChatStore((s) => s.userProfile?.clearance);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [level, setLevel] = useState<DlpSensitivity>(DEFAULT_SENSITIVITY);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Sensitivity levels this user may set on a workspace (≤ their clearance).
  // When clearance is undefined, allow all (gating happens server-side).
  const levelOptions = useMemo(() => {
    const maxRank = clearance ? sensitivityRank(clearance) : Infinity;
    return SENSITIVITY_ORDER.filter((lvl) => sensitivityRank(lvl) <= maxRank).map((lvl) => ({
      id: lvl,
      label: `${lvl} ${SENSITIVITY_LABELS[lvl]}`,
    }));
  }, [clearance]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [saving, onClose]);

  const create = async () => {
    if (!name.trim()) {
      setError('Please enter a workspace name');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onCreate(name.trim(), description.trim(), level);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create workspace');
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => {
        if (!saving) onClose();
      }}
    >
      <div
        className="w-full max-w-[440px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">New workspace</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors disabled:opacity-40"
            onClick={onClose}
            disabled={saving}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-4 px-4 py-4">
          {error && (
            <div className="border border-accent-line bg-accent-soft rounded-md px-3 py-2 text-[13px] text-accent">
              {error}
            </div>
          )}
          <div className="flex flex-col gap-1">
            <label className="cw-label">Name *</label>
            <input
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Contract review"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !saving) create();
              }}
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="cw-label">Description</label>
            <textarea
              className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper min-h-[60px] resize-y focus:outline-none focus:border-accent"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="One line on what this workspace is for"
            />
          </div>
          <div className="flex flex-col gap-1">
            <label className="cw-label">Sensitivity level</label>
            <Dropdown
              value={level}
              options={levelOptions}
              onChange={(id) => setLevel(id as DlpSensitivity)}
              title="Workspace sensitivity level"
              dropDirection="down"
              fullWidth
            />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            className="cw-btn cw-btn-primary cw-btn-sm"
            onClick={create}
            disabled={saving || !name.trim()}
          >
            {saving ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  );
}

function VersionHistoryDialog({
  workspaceId,
  file,
  onClose,
  onRestored,
}: {
  workspaceId: string;
  file: WorkspaceFile;
  onClose: () => void;
  onRestored: () => void;
}) {
  const [versions, setVersions] = useState<WorkspaceFileVersion[]>([]);
  const [loading, setLoading] = useState(true);
  const [restoring, setRestoring] = useState<string | null>(null);
  const confirm = useConfirm();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const resp = await api.workspaces.listVersions(workspaceId, file.id);
        if (!cancelled) setVersions(resp.items ?? []);
      } catch {
        /* best-effort */
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [workspaceId, file.id]);

  const handleRestore = async (ver: WorkspaceFileVersion) => {
    const ok = await confirm({ message: `Restore version ${ver.version}? The current content will be saved as a new version.` });
    if (!ok) return;
    setRestoring(ver.id);
    try {
      await api.workspaces.restoreVersion(workspaceId, file.id, ver.id);
      onRestored();
    } catch {
      /* best-effort */
    } finally {
      setRestoring(null);
    }
  };

  // Retention hint: versions older than 90 days may be cleaned up.
  const retentionDays = 90;
  const expiryHint = (createdAt: string) => {
    const expiry = new Date(new Date(createdAt).getTime() + retentionDays * 86400000);
    return `Expires ${expiry.toLocaleDateString('en-US')}`;
  };

  return (
    <Dialog title={`Version history · ${file.filename}`} onClose={onClose} className="w-full max-w-[520px]">
      {loading ? (
        <div className="text-center text-dim text-[13px] py-6">Loading…</div>
      ) : versions.length === 0 ? (
        <div className="text-center text-dim text-[13px] py-6">No version history</div>
      ) : (
        <div className="flex flex-col gap-0 max-h-[400px] overflow-y-auto">
          {versions.map((ver) => (
            <div
              key={ver.id}
              className="flex items-center justify-between px-3 py-2.5 border-b border-line-2 hover:bg-paper-2"
            >
              <div className="min-w-0 flex-1">
                <div className="font-mono text-[12.5px] text-ink">
                  v{ver.version}
                  <span className="text-dim ml-2">{formatBytes(ver.size_bytes)}</span>
                </div>
                <div className="font-mono text-[11px] text-dim mt-0.5">
                  {formatRelativeTime(ver.created_at)}
                  {ver.created_by && <span className="ml-1.5">· {ver.created_by}</span>}
                </div>
                <div className="font-mono text-[10px] text-dim mt-0.5 opacity-60">
                  {expiryHint(ver.created_at)}
                </div>
              </div>
              <button
                type="button"
                onClick={() => handleRestore(ver)}
                disabled={restoring === ver.id}
                className="cw-btn cw-btn-sm text-[11px] ml-2 shrink-0"
              >
                {restoring === ver.id ? 'Restoring…' : 'Restore'}
              </button>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
