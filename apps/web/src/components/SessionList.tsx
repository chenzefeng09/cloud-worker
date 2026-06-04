import { useSessions } from '../hooks/useSessions';

export function SessionList() {
  const { sessions, loading, createSession, setActiveSession, activeSessionId } = useSessions();

  return (
    <div className="p-2">
      <button
        onClick={() => createSession()}
        disabled={loading}
        className="w-full py-2 mb-2 text-sm font-medium bg-primary-50 text-primary-700 rounded-lg hover:bg-primary-100 disabled:opacity-50"
      >
        + New Session
      </button>
      {loading && <p className="text-xs text-gray-400 p-2">Loading...</p>}
      {sessions.map((session) => (
        <button
          key={session.id}
          onClick={() => setActiveSession(session.id)}
          className={`w-full text-left px-3 py-2 rounded-lg mb-1 text-sm truncate ${
            session.id === activeSessionId
              ? 'bg-primary-50 text-primary-700'
              : 'text-gray-600 hover:bg-surface-100'
          }`}
        >
          <div className="font-medium truncate">{session.title || 'New Session'}</div>
          <div className="text-xs text-gray-400">{session.status}</div>
        </button>
      ))}
      {sessions.length === 0 && !loading && (
        <p className="text-xs text-gray-400 p-2">No sessions yet</p>
      )}
    </div>
  );
}
