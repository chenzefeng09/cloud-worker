import { createContext, useContext } from 'react';

// ── Markdown image → real attachment resolution ──────────────────────────────
//
// The model writes image references using the path it saw *inside the sandbox*
// (`![图](outputs/sales_trend.png)`). Rendered verbatim the browser resolves
// that against the page URL and 404s, so the user gets a broken-image glyph.
// The file itself does exist — it was uploaded as an output attachment and is
// reachable at /api/bff/v1/sessions/{sid}/files/{attId}/content (same-origin,
// cookie auth).
//
// So: the page that knows the session's attachments mounts a provider, and the
// shared markdown `img` renderer asks it to map a sandbox path onto a real
// content URL by filename. Consumers rendered outside a provider get `null` and
// fall back to the "not found" placeholder — never a broken image.
//
// This is a pure front-end fallback, which is why it also repairs historical
// rows: nothing about the stored markdown changes.

export interface MarkdownImageTarget {
  /** Same-origin content URL usable directly as an <img src>. */
  url: string;
  filename: string;
  /** Attachment id — lets the page open its preview panel on click. */
  id?: string;
  mime?: string;
}

export interface MarkdownAssets {
  /** Map a markdown image src onto a real attachment, or null if unknown. */
  resolveImage: (src: string) => MarkdownImageTarget | null;
  /** Optional click handler (e.g. open the page's preview panel). */
  onOpen?: (target: MarkdownImageTarget) => void;
}

const MarkdownAssetsContext = createContext<MarkdownAssets | null>(null);

export const MarkdownAssetsProvider = MarkdownAssetsContext.Provider;

/** Assets supplied by the nearest provider, or null when there is none. */
export function useMarkdownAssets(): MarkdownAssets | null {
  return useContext(MarkdownAssetsContext);
}

/** True for sources the browser can already fetch on its own. */
export function isDirectImageSrc(src: string): boolean {
  return /^(https?:\/\/|data:|blob:|\/\/)/i.test(src.trim());
}

/**
 * Last path segment of a sandbox-ish reference, percent-decoded.
 *
 * Handles `outputs/x.png`, `./x.png`, `/workspace/outputs/x.png`, Windows-style
 * separators, and `%E6%9C%88...png` (CJK filenames survive a markdown round
 * trip percent-encoded). Query/hash tails are dropped first so
 * `x.png?v=2` still matches the stored `x.png`.
 */
export function assetBasename(src: string): string {
  let s = src.trim();
  const hash = s.indexOf('#');
  if (hash >= 0) s = s.slice(0, hash);
  const query = s.indexOf('?');
  if (query >= 0) s = s.slice(0, query);
  s = s.replace(/\\/g, '/');
  const last = s.slice(s.lastIndexOf('/') + 1);
  try {
    return decodeURIComponent(last);
  } catch {
    // Malformed escape sequence — the raw segment is the best we have.
    return last;
  }
}

/** The attachment shape this module needs (a structural subset of `Attachment`). */
export interface ResolvableAttachment {
  id: string;
  filename: string;
  mime?: string;
}

/**
 * Build a resolver that matches markdown image paths to `attachments` on
 * case-insensitive basename. Returns a resolver that always yields null when
 * there's no session to build a content URL against.
 */
export function makeAttachmentImageResolver(
  attachments: ResolvableAttachment[],
  sessionId: string | undefined,
): (src: string) => MarkdownImageTarget | null {
  if (!sessionId) return () => null;

  // Earliest attachment wins on a duplicate filename — matching the order the
  // "产出文件" list renders, so the inline image and the list agree.
  const byName = new Map<string, ResolvableAttachment>();
  for (const att of attachments) {
    const key = assetBasename(att.filename).toLowerCase();
    if (!byName.has(key)) byName.set(key, att);
  }

  return (src: string) => {
    const name = assetBasename(src).toLowerCase();
    if (!name) return null;
    const att = byName.get(name);
    if (!att) return null;
    return {
      url: `/api/bff/v1/sessions/${sessionId}/files/${att.id}/content`,
      filename: att.filename,
      id: att.id,
      mime: att.mime,
    };
  };
}
