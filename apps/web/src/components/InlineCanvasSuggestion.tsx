import type { CanvasDocument, CanvasSuggestion } from '../api/types';
import { markdownToCanvasHtml } from '../lib/canvasRichText';

interface InlineCanvasSuggestionProps {
  canvas: CanvasDocument;
  suggestion: CanvasSuggestion;
  onAccept: () => void;
  onReject: () => void;
  busy?: boolean;
}

/** True only for suggestions produced by the selection-scoped magic-edit path. */
export function isInlineCanvasSuggestion(suggestion: CanvasSuggestion | null): boolean {
  return Boolean(
    suggestion &&
      suggestion.diffs.length > 0 &&
      suggestion.diffs.every(
        (diff) =>
          diff.op === 'replace' &&
          typeof diff.selection_start === 'number' &&
          typeof diff.selection_end === 'number' &&
          typeof diff.selected_orig === 'string' &&
          typeof diff.selected_sugg === 'string',
      ),
  );
}

/**
 * Selection suggestions are rendered inside the document at their real block
 * position.  The accepted canvas remains untouched until the user confirms.
 */
export function InlineCanvasSuggestion({
  canvas,
  suggestion,
  onAccept,
  onReject,
  busy = false,
}: InlineCanvasSuggestionProps) {
  const diffByBlock = new Map(suggestion.diffs.map((diff) => [diff.block_id, diff]));
  const summary = suggestion.change_summary?.trim() || suggestion.instruction;

  return (
    <section className="canvas-magic-edit" aria-label="选区修改建议">
      <header className="canvas-magic-edit-head">
        <div className="min-w-0">
          <div className="canvas-suggestion-kicker">选区修改建议</div>
          <div className="canvas-suggestion-instruction">{summary}</div>
        </div>
        <div className="canvas-suggestion-actions">
          <button type="button" className="canvas-suggestion-reject" onClick={onReject} disabled={busy}>
            撤销
          </button>
          <button type="button" className="canvas-suggestion-accept" onClick={onAccept} disabled={busy}>
            接受
          </button>
        </div>
      </header>

      <div className="canvas-rich-editor canvas-magic-edit-document">
        {canvas.blocks.map((block, index) => {
          const diff = diffByBlock.get(block.id);
          if (
            !diff ||
            typeof diff.selection_start !== 'number' ||
            typeof diff.selection_end !== 'number' ||
            typeof diff.selected_orig !== 'string' ||
            typeof diff.selected_sugg !== 'string'
          ) {
            return (
              <div
                key={block.id}
                data-canvas-block-id={block.id}
                dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(block.text) }}
              />
            );
          }

          const before = block.text.slice(0, diff.selection_start);
          const after = block.text.slice(diff.selection_end);
          return (
            <div key={block.id} className="canvas-magic-edit-segment" data-canvas-block-id={block.id}>
              {before && (
                <div
                  className="canvas-magic-edit-context"
                  dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(before) }}
                />
              )}
              <div
                data-magic-edit-block="true"
                data-me-role="orig"
                data-me-id={suggestion.id}
                data-me-segment={index}
                className="canvas-magic-edit-orig"
              >
                {diff.selected_orig}
              </div>
              <div
                data-magic-edit-block="true"
                data-me-role="sugg"
                data-me-id={suggestion.id}
                data-me-segment={index}
                className="canvas-magic-edit-sugg"
                dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(diff.selected_sugg) }}
              />
              {after && (
                <div
                  className="canvas-magic-edit-context"
                  dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(after) }}
                />
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
