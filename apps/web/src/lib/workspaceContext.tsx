import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { DEFAULT_WORKSPACE, type BaseFile, type Workspace } from './workspaceData';

interface WorkspaceContextValue {
  wsId: string;
  /** No-op: workspace switching was removed with the mock data. Kept so the
   *  context shape is stable for consumers until phase-2 real workspaces land. */
  setWsId: (id: string) => void;
  ws: Workspace;
  allWs: Workspace[];
  files: BaseFile[];
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  // Single static default workspace — no switching, no fake file lists. When
  // phase 2 makes "workspace" a real backend entity, this provider becomes the
  // place that fetches and exposes it.
  const value = useMemo<WorkspaceContextValue>(
    () => ({
      wsId: DEFAULT_WORKSPACE.id,
      setWsId: () => {},
      ws: DEFAULT_WORKSPACE,
      allWs: [DEFAULT_WORKSPACE],
      files: [],
    }),
    [],
  );

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceContextValue {
  const ctx = useContext(WorkspaceContext);
  if (!ctx) throw new Error('useWorkspace must be used within <WorkspaceProvider>');
  return ctx;
}
