import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { api } from '../api/client';
import type {
  DlpSensitivity,
  Workspace,
  WorkspaceCreate,
  WorkspaceFile,
  WorkspaceUpdate,
} from '../api/types';
import {
  DEFAULT_WORKSPACE,
  toWorkspaceView,
  type WorkspaceView,
} from './workspaceData';

const ACTIVE_KEY = 'cw.activeWorkspaceId';

interface WorkspaceContextValue {
  // ── Real data (new API) ────────────────────────────────────────────────
  /** All workspaces owned by the current user (>= 1 — backend lazy-creates default). */
  workspaces: Workspace[];
  /** The currently-active workspace (real object), or null until loaded. */
  activeWorkspace: Workspace | null;
  setActiveWorkspaceId: (id: string) => void;
  /** Base files of the active workspace. */
  files: WorkspaceFile[];
  loading: boolean;

  refreshWorkspaces: () => Promise<void>;
  refreshFiles: () => Promise<void>;
  createWorkspace: (body: WorkspaceCreate) => Promise<Workspace>;
  updateWorkspace: (id: string, body: WorkspaceUpdate) => Promise<Workspace>;
  deleteWorkspace: (id: string) => Promise<void>;
  uploadFile: (file: File, description?: string, folder?: string, level?: DlpSensitivity) => Promise<WorkspaceFile>;
  deleteFile: (fileId: string) => Promise<void>;

  // ── Legacy presentational projection (kept for existing consumers) ─────
  /** Active workspace's id (falls back to DEFAULT_WORKSPACE.id before load). */
  wsId: string;
  setWsId: (id: string) => void;
  /** Active workspace as a presentational view. */
  ws: WorkspaceView;
  /** All workspaces as presentational views. */
  allWs: WorkspaceView[];
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeId, setActiveIdState] = useState<string | null>(() => {
    try {
      return localStorage.getItem(ACTIVE_KEY);
    } catch {
      return null;
    }
  });

  // Resolve the active workspace: honour the stored id when it points at a real
  // workspace, otherwise fall back to the `is_default` one (or the first).
  const activeWorkspace = useMemo<Workspace | null>(() => {
    if (workspaces.length === 0) return null;
    const stored = activeId ? workspaces.find((w) => w.id === activeId) : undefined;
    if (stored) return stored;
    return workspaces.find((w) => w.is_default) ?? workspaces[0];
  }, [workspaces, activeId]);

  const setActiveWorkspaceId = useCallback((id: string) => {
    setActiveIdState(id);
    try {
      localStorage.setItem(ACTIVE_KEY, id);
    } catch {
      /* ignore storage failures (private mode / quota) */
    }
  }, []);

  const refreshWorkspaces = useCallback(async () => {
    try {
      const items = await api.workspaces.list();
      setWorkspaces(items);
    } catch {
      // fail-open: leave whatever we had; the fallback view keeps the UI usable
    }
  }, []);

  // Load the list once on mount.
  useEffect(() => {
    let alive = true;
    setLoading(true);
    api.workspaces
      .list()
      .then((items) => {
        if (alive) setWorkspaces(items);
      })
      .catch(() => {/* fail-open */})
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  // ── Files of the active workspace ──────────────────────────────────────
  // Track the last id we fetched files for so refreshFiles has a stable target
  // even inside callbacks created before activeWorkspace settled.
  const activeWsId = activeWorkspace?.id ?? null;
  const activeWsIdRef = useRef<string | null>(activeWsId);
  activeWsIdRef.current = activeWsId;

  const refreshFiles = useCallback(async () => {
    const id = activeWsIdRef.current;
    if (!id) {
      setFiles([]);
      return;
    }
    try {
      const items = await api.workspaces.listFiles(id);
      // Guard against a race where the active workspace changed mid-fetch.
      if (activeWsIdRef.current === id) setFiles(items);
    } catch {
      if (activeWsIdRef.current === id) setFiles([]);
    }
  }, []);

  // Re-pull files whenever the active workspace changes.
  useEffect(() => {
    if (!activeWsId) {
      setFiles([]);
      return;
    }
    let alive = true;
    api.workspaces
      .listFiles(activeWsId)
      .then((items) => {
        if (alive && activeWsIdRef.current === activeWsId) setFiles(items);
      })
      .catch(() => {
        if (alive && activeWsIdRef.current === activeWsId) setFiles([]);
      });
    return () => {
      alive = false;
    };
  }, [activeWsId]);

  const createWorkspace = useCallback(
    async (body: WorkspaceCreate) => {
      const created = await api.workspaces.create(body);
      await refreshWorkspaces();
      setActiveWorkspaceId(created.id);
      return created;
    },
    [refreshWorkspaces, setActiveWorkspaceId],
  );

  const updateWorkspace = useCallback(
    async (id: string, body: WorkspaceUpdate) => {
      const updated = await api.workspaces.update(id, body);
      // Patch in place so callers see the new fields without a full refetch.
      setWorkspaces((prev) => prev.map((w) => (w.id === id ? updated : w)));
      return updated;
    },
    [],
  );

  const deleteWorkspace = useCallback(
    async (id: string) => {
      await api.workspaces.remove(id);
      await refreshWorkspaces();
      // If we deleted the active one, drop the stored id so the resolver falls
      // back to the default workspace.
      if (activeWsIdRef.current === id) {
        setActiveIdState(null);
        try {
          localStorage.removeItem(ACTIVE_KEY);
        } catch {
          /* ignore */
        }
      }
    },
    [refreshWorkspaces],
  );

  const uploadFile = useCallback(
    async (file: File, description?: string, folder?: string, level?: DlpSensitivity) => {
      const id = activeWsIdRef.current;
      if (!id) throw new Error('No workspace available');
      const uploaded = await api.workspaces.uploadFile(id, file, description, folder, level);
      await refreshFiles();
      // file_count on the workspace is derived server-side; refresh the list so
      // the summary count stays in sync.
      await refreshWorkspaces();
      return uploaded;
    },
    [refreshFiles, refreshWorkspaces],
  );

  const deleteFile = useCallback(
    async (fileId: string) => {
      const id = activeWsIdRef.current;
      if (!id) return;
      await api.workspaces.deleteFile(id, fileId);
      await refreshFiles();
      await refreshWorkspaces();
    },
    [refreshFiles, refreshWorkspaces],
  );

  // ── Legacy presentational projection ───────────────────────────────────
  const allWs = useMemo<WorkspaceView[]>(
    () =>
      workspaces.length > 0
        ? workspaces.map(toWorkspaceView)
        : [DEFAULT_WORKSPACE],
    [workspaces],
  );

  const ws = useMemo<WorkspaceView>(
    () => (activeWorkspace ? toWorkspaceView(activeWorkspace) : DEFAULT_WORKSPACE),
    [activeWorkspace],
  );

  const wsId = activeWorkspace?.id ?? DEFAULT_WORKSPACE.id;

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspaces,
      activeWorkspace,
      setActiveWorkspaceId,
      files,
      loading,
      refreshWorkspaces,
      refreshFiles,
      createWorkspace,
      updateWorkspace,
      deleteWorkspace,
      uploadFile,
      deleteFile,
      wsId,
      setWsId: setActiveWorkspaceId,
      ws,
      allWs,
    }),
    [
      workspaces,
      activeWorkspace,
      setActiveWorkspaceId,
      files,
      loading,
      refreshWorkspaces,
      refreshFiles,
      createWorkspace,
      updateWorkspace,
      deleteWorkspace,
      uploadFile,
      deleteFile,
      wsId,
      ws,
      allWs,
    ],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within <WorkspaceProvider>');
  return ctx;
}
