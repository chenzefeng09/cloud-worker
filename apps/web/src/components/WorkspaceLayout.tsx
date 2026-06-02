import { useEffect } from 'react';
import { useSessions } from '../hooks/useSessions';
import { SessionList } from './SessionList';
import { ChatWindow } from './ChatWindow';
import { useChatStore } from '../stores/chatStore';
import { api } from '../api/client';

export function WorkspaceLayout() {
  const { fetchSessions } = useSessions();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const userProfile = useChatStore((s) => s.userProfile);
  const logout = useChatStore((s) => s.logout);

  useEffect(() => {
    fetchSessions();
  }, [fetchSessions]);

  const handleLogout = async () => {
    await api.auth.logout();
    logout();
  };

  return (
    <div className="h-screen flex">
      <aside className="w-64 bg-white border-r border-surface-200 flex flex-col">
        <div className="p-4 border-b border-surface-200">
          <h1 className="text-lg font-semibold text-gray-800">Agent Platform</h1>
          {userProfile && <p className="text-xs text-gray-500 mt-1">{userProfile.email}</p>}
        </div>
        <div className="flex-1 overflow-y-auto">
          <SessionList />
        </div>
        <div className="p-4 border-t border-surface-200">
          <button
            onClick={handleLogout}
            className="w-full py-2 text-sm text-gray-500 hover:text-gray-700 hover:bg-surface-100 rounded-lg"
          >
            Sign Out
          </button>
        </div>
      </aside>
      <main className="flex-1 flex flex-col">
        {activeSessionId ? (
          <ChatWindow sessionId={activeSessionId} />
        ) : (
          <div className="flex-1 flex items-center justify-center text-gray-400">
            <div className="text-center">
              <p className="text-lg mb-2">Select a session or create a new one</p>
              <p className="text-sm">Click + in the sidebar to get started</p>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}
