import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { WORKSPACES, WORKSPACE_FILES, type BaseFile, type Workspace } from './workspaceData';

interface WorkspaceContextValue {
  wsId: string;
  setWsId: (id: string) => void;
  ws: Workspace;
  allWs: Workspace[];
  files: BaseFile[];
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

const STORAGE_KEY = 'cw-ws';

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const initial = (() => {
    try {
      return localStorage.getItem(STORAGE_KEY) ?? 'eng';
    } catch {
      return 'eng';
    }
  })();
  const [wsId, setWsIdState] = useState<string>(initial);

  const setWsId = (id: string) => {
    setWsIdState(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // localStorage may be unavailable (private mode / SSR); silently ignore.
    }
  };

  const value = useMemo<WorkspaceContextValue>(() => {
    const ws = WORKSPACES.find((w) => w.id === wsId) ?? WORKSPACES[0];
    const files = WORKSPACE_FILES[ws.id] ?? [];
    return { wsId, setWsId, ws, allWs: WORKSPACES, files };
  }, [wsId]);

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within <WorkspaceProvider>');
  return ctx;
}
