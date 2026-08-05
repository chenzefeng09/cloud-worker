import type { CanvasDocument } from '../api/types';

/**
 * The raw self-contained HTML of an executable canvas, or `null` for a prose
 * (markdown) canvas.
 *
 * Single detection point for the frontend (per the canvas-html contract): it
 * prefers the server-derived `content_type` and falls back to structural
 * detection (exactly one block of type `html`) so it also works for a version's
 * blocks that carry no `content_type` field. The returned string is handed
 * verbatim to a sandboxed iframe — never injected into the parent DOM.
 */
export function canvasHtmlSource(
  canvas: Pick<CanvasDocument, 'content_type' | 'blocks'> | null | undefined,
): string | null {
  if (!canvas) return null;
  const blocks = canvas.blocks ?? [];
  const isHtml =
    canvas.content_type === 'html' ||
    (blocks.length === 1 && blocks[0]?.type === 'html');
  if (!isHtml) return null;
  const htmlBlock = blocks.find((b) => b.type === 'html') ?? blocks[0];
  return htmlBlock?.text ?? '';
}
