import { jsx as _jsx } from "react/jsx-runtime";
import { createContext, useContext, useMemo, useState } from 'react';
import { WORKSPACES, WORKSPACE_FILES } from './workspaceData';
const WorkspaceContext = createContext(null);
const STORAGE_KEY = 'cw-ws';
export function WorkspaceProvider({ children }) {
    const initial = (() => {
        try {
            return localStorage.getItem(STORAGE_KEY) ?? 'eng';
        }
        catch {
            return 'eng';
        }
    })();
    const [wsId, setWsIdState] = useState(initial);
    const setWsId = (id) => {
        setWsIdState(id);
        try {
            localStorage.setItem(STORAGE_KEY, id);
        }
        catch {
            // localStorage may be unavailable (private mode / SSR); silently ignore.
        }
    };
    const value = useMemo(() => {
        const ws = WORKSPACES.find((w) => w.id === wsId) ?? WORKSPACES[0];
        const files = WORKSPACE_FILES[ws.id] ?? [];
        return { wsId, setWsId, ws, allWs: WORKSPACES, files };
    }, [wsId]);
    return _jsx(WorkspaceContext.Provider, { value: value, children: children });
}
export function useWorkspace() {
    const ctx = useContext(WorkspaceContext);
    if (!ctx)
        throw new Error('useWorkspace must be used within <WorkspaceProvider>');
    return ctx;
}
