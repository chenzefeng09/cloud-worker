import { type CSSProperties } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWorkspace } from '../../lib/workspaceContext';

/**
 * Top-of-sidebar workspace header. Workspace SWITCHING was removed along with
 * the mock data — there is one static workspace until phase-2 real workspaces
 * land. This renders a static, clickable header (navigates to the workspace
 * detail page) with the accent stripe preserved, but no dropdown.
 */
export function WorkspaceSwitcher() {
  const { ws } = useWorkspace();
  const navigate = useNavigate();

  // Use CSS vars so the accent stripe can live in plain class-based markup.
  const accentStyle = {
    '--ws-accent': ws.accent,
    '--ws-accent-soft': ws.accent + '14',
    '--ws-accent-line': ws.accent + '55',
  } as CSSProperties;

  return (
    <div
      onClick={() => navigate('/workspace')}
      style={accentStyle}
      className="relative flex items-center gap-2.5 px-2.5 py-2 border border-line bg-surface rounded-md cursor-pointer hover:border-ink-2 hover:bg-paper-2 transition-colors"
    >
      <span
        className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-sm"
        style={{ background: 'var(--ws-accent)' }}
      />
      <span
        className="inline-flex w-[26px] h-[26px] items-center justify-center rounded-sm font-mono text-[13px] shrink-0 border"
        style={{
          background: 'var(--ws-accent-soft)',
          borderColor: 'var(--ws-accent-line)',
          color: 'var(--ws-accent)',
        }}
      >
        {ws.glyph}
      </span>
      <div className="flex-1 min-w-0">
        <div className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-dim mb-px">
          工作区
        </div>
        <div className="text-[13.5px] font-semibold tracking-[-0.01em] text-ink truncate">
          {ws.name}
        </div>
      </div>
    </div>
  );
}
