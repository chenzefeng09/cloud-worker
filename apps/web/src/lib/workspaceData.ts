/**
 * Workspace shell types + the single static default workspace.
 *
 * The four hardcoded mock workspaces (工程/运营/市场/客户) and their fake base
 * files have been removed — "workspace" is not yet a real backend entity. Until
 * phase 2 makes it one (a reusable, cross-session container of mounted file
 * trees with read/write flags), the app runs with ONE neutral default
 * workspace: no switching, no fake file lists. The `Workspace`/`BaseFile` shapes
 * are kept so consumers can swap to real data without restructuring.
 */

export interface Workspace {
  id: string;
  name: string;
  glyph: string;
  desc: string;
  accent: string;
}

export interface BaseFile {
  n: string;
  k: string;
  size: string;
  updated: string;
  pinned?: boolean;
  desc?: string;
  children?: BaseFile[];
}

/**
 * The single default workspace. `accent` drives the theme stripe across the
 * shell (Sidebar / FilesPage / AgentPage). Keep it as the brand accent so the
 * UI looks unchanged after the fake switcher is removed.
 */
export const DEFAULT_WORKSPACE: Workspace = {
  id: 'default',
  name: '工作区',
  glyph: '◇',
  desc: '上下文 · 文件 · 技能',
  accent: '#d23a1f',
};
