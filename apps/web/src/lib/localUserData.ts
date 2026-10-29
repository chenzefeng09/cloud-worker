/**
 * Browser-side copies of what a user wrote: canvas drafts (CanvasMessageCard /
 * ArticleCanvasPanel, `article-canvas-draft-v1:<session>`) and empty workspace
 * folder names (WorkspacePage, `ws-empty-folders:<workspace>`). They are
 * cleared on sign-out and after "Delete my data", so none of it outlives the
 * user on a shared device. UI settings (language, collapsed sidebar) stay.
 */
const USER_CONTENT_PREFIXES = ['article-canvas-draft-v1:', 'ws-empty-folders:'];

export function clearLocalUserContent(): void {
  try {
    const doomed: string[] = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (key && USER_CONTENT_PREFIXES.some((prefix) => key.startsWith(prefix))) doomed.push(key);
    }
    for (const key of doomed) localStorage.removeItem(key);
  } catch {
    // Storage is unavailable (private mode, blocked site data): nothing was kept.
  }
}
