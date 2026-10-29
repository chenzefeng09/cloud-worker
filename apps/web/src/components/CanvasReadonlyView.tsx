import { useEffect, useState } from 'react';
import { api, ApiError } from '../api/client';
import type { CanvasDocument } from '../api/types';
import { canvasBlocksToHtml } from '../lib/canvasRichText';
import { canvasHtmlSource } from '../lib/canvasHtml';
import { CanvasHtmlPreview } from './CanvasHtmlPreview';

interface CanvasReadonlyViewProps {
  canvasId: string | null;
  onClose: () => void;
}

/**
 * Read-only canvas viewer for the Artifacts page. Fetches a server-owned
 * canvas via `api.canvas.get` and renders its blocks with the same rich-text
 * pipeline used by the chat editor (`canvasBlocksToHtml`), but WITHOUT any
 * editing coupling - no `useCanvasStore`, no draft state, no revision controls.
 * Mirrors `FilePreviewPanel`'s shell (header + scrollable body) so the two
 * preview kinds feel symmetrical.
 */
export function CanvasReadonlyView({ canvasId, onClose }: CanvasReadonlyViewProps) {
  const [canvas, setCanvas] = useState<CanvasDocument | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!canvasId) {
      setCanvas(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    setCanvas(null);
    api.canvas
      .get(canvasId)
      .then((doc) => {
        if (cancelled) return;
        setCanvas(doc);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : 'Couldn’t load the canvas. Please try again later.');
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [canvasId]);

  if (!canvasId) return null;

  const title = canvas?.title || 'Canvas';
  // Executable canvas → live sandbox preview; prose → rich-text render. Only
  // build the prose HTML for prose canvases (canvasBlocksToHtml would escape an
  // html block into unusable text).
  const htmlSource = canvasHtmlSource(canvas);
  const html = canvas && htmlSource === null ? canvasBlocksToHtml(canvas.blocks) : '';

  return (
    <div
      className="flex flex-col h-full bg-paper border-l border-line"
      style={{ width: '100%', minWidth: 0, overflow: 'hidden' }}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2.5 border-b border-line shrink-0">
        <button
          onClick={onClose}
          aria-label="Back"
          title="Back"
          className="h-8 inline-flex items-center gap-1.5 px-2.5 rounded-md border border-line bg-paper text-ink-2 hover:text-ink hover:bg-paper-2 hover:border-ink-2 transition-colors shrink-0 font-mono text-[12px]"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 2 L3 6 L7 10 M3 6 L11 6" />
          </svg>
        </button>
        <span
          className="font-mono text-[12px] font-semibold text-ink flex-1 truncate"
          title={title}
        >
          {title}
        </span>
      </div>

      {/* Body */}
      <div className="cw-scroll flex-1 px-4 py-4">
        {loading && (
          <div className="font-mono text-[11px] text-dim">Loading…</div>
        )}
        {error && (
          <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12px] text-accent">
            {error}
          </div>
        )}
        {!loading && !error && canvas && (
          htmlSource !== null ? (
            <CanvasHtmlPreview
              html={htmlSource}
              title={title}
              reloadKey={canvas.current_version}
            />
          ) : (
            <article
              className="canvas-rich-editor text-ink break-words"
              dangerouslySetInnerHTML={{ __html: html }}
            />
          )
        )}
      </div>
    </div>
  );
}
