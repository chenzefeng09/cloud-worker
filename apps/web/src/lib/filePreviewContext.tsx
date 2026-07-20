import { createContext, useContext } from 'react';

// ── File-preview opener context ──────────────────────────────────────────────
//
// Lets deep-tree renderers (e.g. the markdown `a` component that intercepts
// `[name](#file:<id>)` links in assistant replies) open a file in the page's
// preview panel without threading an `onOpen` callback through every layer.
//
// The Provider is mounted by the page that owns the preview panel (AgentPage);
// the value is the opener that panel wires up (setPreviewFile). Consumers that
// render outside a Provider (e.g. AppResultPage) get `null` and fall back to
// their default behaviour.

export interface FilePreviewTarget {
  id: string;
  filename: string;
  mime?: string;
}

export type FilePreviewOpener = (file: FilePreviewTarget) => void;

const FilePreviewContext = createContext<FilePreviewOpener | null>(null);

export const FilePreviewProvider = FilePreviewContext.Provider;

/** The opener supplied by the nearest FilePreviewProvider, or null if none. */
export function useFilePreview(): FilePreviewOpener | null {
  return useContext(FilePreviewContext);
}
