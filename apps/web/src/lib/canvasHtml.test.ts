import { describe, expect, it } from 'vitest';
import { canvasHtmlSource } from './canvasHtml';
import type { CanvasDocument } from '../api/types';

function doc(partial: Partial<CanvasDocument>): CanvasDocument {
  return {
    id: 'cvs_1',
    session_id: 'ses_1',
    kind: 'article',
    title: 't',
    current_version: 1,
    blocks: [],
    created_at: '',
    updated_at: '',
    ...partial,
  };
}

describe('canvasHtmlSource', () => {
  it('returns the raw html for an html canvas (by content_type)', () => {
    const html = '<!doctype html><html><body>hi</body></html>';
    expect(
      canvasHtmlSource(
        doc({ content_type: 'html', blocks: [{ id: 'b1', type: 'html', text: html }] }),
      ),
    ).toBe(html);
  });

  it('falls back to structural detection when content_type is absent', () => {
    const html = '<html><body>x</body></html>';
    expect(
      canvasHtmlSource(doc({ blocks: [{ id: 'b1', type: 'html', text: html }] })),
    ).toBe(html);
  });

  it('returns null for a prose (markdown) canvas', () => {
    expect(
      canvasHtmlSource(
        doc({
          content_type: 'markdown',
          blocks: [
            { id: 'b1', type: 'heading', text: '# T' },
            { id: 'b2', type: 'paragraph', text: 'body' },
          ],
        }),
      ),
    ).toBeNull();
  });

  it('returns null for null/undefined input', () => {
    expect(canvasHtmlSource(null)).toBeNull();
    expect(canvasHtmlSource(undefined)).toBeNull();
  });
});
