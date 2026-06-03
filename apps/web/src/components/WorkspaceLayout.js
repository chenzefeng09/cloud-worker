import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { useSessions } from '../hooks/useSessions';
import { useChatStore } from '../stores/chatStore';
import { WorkspaceProvider } from '../lib/workspaceContext';
import { Sidebar } from './shell/Sidebar';
import { HomePage } from '../pages/HomePage';
import { TasksPage } from '../pages/TasksPage';
import { AgentPage } from '../pages/AgentPage';
import { FilesPage } from '../pages/FilesPage';
import { LibraryPage } from '../pages/LibraryPage';
import { FleetPage } from '../pages/FleetPage';
import { WorkspacePage } from '../pages/WorkspacePage';
import { ProfilePage } from '../pages/ProfilePage';
import { AdminLayout } from '../admin/AdminLayout';
/**
 * Authenticated app shell: sidebar + routed main view. WorkspaceProvider lives
 * here (and not at App root) so the workspace context is only initialised once
 * the user is past AuthGate — that way the login screen stays free of any
 * cloud-worker-specific state.
 *
 * The route table is kept flat and mirrors the cloud-worker hash routes:
 *   /home /tasks /tasks/:id /files /library /fleet /workspace
 * Anything else falls back to /home.
 */
export function WorkspaceLayout() {
    const userProfile = useChatStore((s) => s.userProfile);
    const isSuperAdmin = userProfile?.roles?.includes('super_admin') ?? false;
    return (_jsx(WorkspaceProvider, { children: _jsxs("div", { className: "h-screen flex bg-paper", children: [_jsx(SessionsBootstrap, {}), _jsxs(Routes, { children: [_jsx(Route, { path: "/admin/*", element: isSuperAdmin ? (_jsx("div", { className: "flex-1 flex min-w-0 h-full", children: _jsx(AdminLayout, {}) })) : (_jsx(Navigate, { to: "/home", replace: true })) }), _jsx(Route, { path: "*", element: _jsxs("div", { className: "flex-1 flex min-w-0 h-full", children: [_jsx(Sidebar, {}), _jsx("main", { className: "flex-1 flex flex-col min-w-0", children: _jsxs(Routes, { children: [_jsx(Route, { path: "/home", element: _jsx(HomePage, {}) }), _jsx(Route, { path: "/tasks", element: _jsx(TasksPage, {}) }), _jsx(Route, { path: "/tasks/:id", element: _jsx(AgentPage, {}) }), _jsx(Route, { path: "/files", element: _jsx(FilesPage, {}) }), _jsx(Route, { path: "/library", element: _jsx(LibraryPage, {}) }), _jsx(Route, { path: "/fleet", element: _jsx(FleetPage, {}) }), _jsx(Route, { path: "/workspace", element: _jsx(WorkspacePage, {}) }), _jsx(Route, { path: "/profile", element: _jsx(ProfilePage, {}) }), _jsx(Route, { path: "*", element: _jsx(Navigate, { to: "/home", replace: true }) })] }) })] }) })] })] }) }));
}
/**
 * Triggers the initial sessions fetch exactly once when the workspace mounts.
 * Kept as a child component so the layout itself doesn't need to re-render
 * on every store update — `useSessions` subscribes via zustand selectors.
 */
function SessionsBootstrap() {
    const { fetchSessions } = useSessions();
    useEffect(() => {
        fetchSessions();
    }, [fetchSessions]);
    return null;
}
