/**
 * Workspace presentation helpers.
 *
 * The real `Workspace` / `WorkspaceFile` wire types now live in `api/types.ts`
 * and are fetched by `WorkspaceProvider` (see workspaceContext.tsx). This module
 * only holds the small *presentation* view model that legacy consumers
 * (FilesPage, ProfilePage, WorkspacePage) read — `{ id, name,
 * glyph, desc, accent }` — plus the mappers that derive it from a real
 * `Workspace`, and the neutral default view used before data has loaded.
 */
import type { Workspace } from '../api/types';

/** Presentational projection of a workspace used across the shell. */
export interface WorkspaceView {
  id: string;
  name: string;
  glyph: string;
  desc: string;
  accent: string;
}

/** A base file row as rendered in the files list (presentation shape). */
export interface BaseFile {
  n: string;
  k: string;
  size: string;
  updated: string;
  pinned?: boolean;
  desc?: string;
  children?: BaseFile[];
}

/** Brand accent — the fallback stripe colour when a workspace sets no meta.accent. */
export const DEFAULT_ACCENT = '#d23a1f';
/** Fallback glyph when a workspace sets no meta.glyph. */
export const DEFAULT_GLYPH = '◇';

/**
 * The neutral default view, shown while the real list is still loading (or if
 * the fetch fails). `accent` drives the theme stripe across the shell.
 */
export const DEFAULT_WORKSPACE: WorkspaceView = {
  id: 'default',
  name: '工作区',
  glyph: DEFAULT_GLYPH,
  desc: '上下文 · 文件 · 技能',
  accent: DEFAULT_ACCENT,
};

/** Project a real `Workspace` down to the presentational `WorkspaceView`. */
export function toWorkspaceView(ws: Workspace): WorkspaceView {
  const glyph = ws.meta?.glyph?.trim() || DEFAULT_GLYPH;
  const accent = ws.meta?.accent?.trim() || DEFAULT_ACCENT;
  const desc =
    ws.description?.trim() ||
    (ws.file_count > 0 ? `${ws.file_count} 个基础文件` : '上下文 · 文件 · 技能');
  return { id: ws.id, name: ws.name, glyph, desc, accent };
}
