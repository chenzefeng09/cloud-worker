import { useEffect } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { useSessions } from '../hooks/useSessions';
import { useChatStore } from '../stores/chatStore';
import { WorkspaceProvider } from '../lib/workspaceContext';
import { Sidebar } from './shell/Sidebar';
import { HomePage } from '../pages/HomePage';
import { AgentPage } from '../pages/AgentPage';
import { TasksPage } from '../pages/TasksPage';
import { AppRunPage } from '../pages/AppRunPage';
import { AppResultPage } from '../pages/AppResultPage';
import { AppEditorPage } from '../pages/AppEditorPage';
import { AppListPage } from '../pages/AppListPage';
import { AppDetailPage } from '../pages/AppDetailPage';
import { AppHistoryPage } from '../pages/AppHistoryPage';
import { FilesPage } from '../pages/FilesPage';
import { LibraryPage } from '../pages/LibraryPage';
import { WorkspacePage } from '../pages/WorkspacePage';
import { ArtifactsPage } from '../pages/ArtifactsPage';
import { ProfilePage } from '../pages/ProfilePage';
import { AdminLayout, canAccessAdmin } from '../admin/AdminLayout';
import { DialogProvider } from '../admin/components/dialogs';

/**
 * Authenticated app shell: sidebar + routed main view. WorkspaceProvider lives
 * here (and not at App root) so the workspace context is only initialised once
 * the user is past AuthGate — that way the login screen stays free of any
 * cloud-worker-specific state.
 *
 * The route table is kept flat and mirrors the cloud-worker hash routes:
 *   /home /tasks /tasks/:id /files /library /workspace
 * Anything else falls back to /home.
 */
export function WorkspaceLayout() {
  const userProfile = useChatStore((s) => s.userProfile);
  const canAdmin = canAccessAdmin(userProfile?.permissions);

  return (
    <WorkspaceProvider>
      <div className="h-screen flex bg-paper">
        <SessionsBootstrap />
        <Routes>
          {/* Admin shell — accessible to anyone holding at least one admin
              module permission (platform admins see everything). */}
          <Route
            path="/admin/*"
            element={
              canAdmin ? (
                <div className="flex-1 flex min-w-0 h-full">
                  <AdminLayout />
                </div>
              ) : (
                <Navigate to="/home" replace />
              )
            }
          />
          {/* Standard workspace shell. DialogProvider is mounted here (the admin
              shell mounts its own) so workspace pages get in-app confirm/toast
              instead of the unstyled, thread-blocking native dialogs. */}
          <Route
            path="*"
            element={
              <DialogProvider>
                <div className="flex-1 flex min-w-0 h-full">
                  <Sidebar />
                  <main className="flex-1 flex flex-col min-w-0">
                    <Routes>
                      <Route path="/home" element={<HomePage />} />
                      <Route path="/tasks" element={<TasksPage />} />
                      <Route path="/tasks/:id" element={<AgentPage />} />
                      {/* App routes — static/longer paths first so react-router v6
                          ranking never lets `/apps/:id` shadow `/apps/new` etc. */}
                      <Route path="/apps/new" element={<Navigate to="/home" replace />} />
                      <Route path="/apps/runs/:sessionId/:runId" element={<AppResultPage />} />
                      <Route path="/apps/:id/edit" element={<AppEditorPage />} />
                      <Route path="/apps/:id/run" element={<AppRunPage />} />
                      <Route path="/apps/:id/history" element={<AppHistoryPage />} />
                      <Route path="/apps/:id" element={<AppDetailPage />} />
                      <Route path="/apps" element={<AppListPage />} />
                      {/* 定时任务 folded into the app surface as a tab. */}
                      <Route path="/schedules" element={<Navigate to="/apps?tab=schedules" replace />} />
                      <Route path="/files" element={<FilesPage />} />
                      <Route path="/library" element={<LibraryPage />} />
                      <Route path="/workspace" element={<WorkspacePage />} />
                      <Route path="/artifacts" element={<ArtifactsPage />} />
                      <Route path="/profile" element={<ProfilePage />} />
                      <Route path="*" element={<Navigate to="/home" replace />} />
                    </Routes>
                  </main>
                </div>
              </DialogProvider>
            }
          />
        </Routes>
      </div>
    </WorkspaceProvider>
  );
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
