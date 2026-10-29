import type { ReactNode } from 'react';
import type { CanvasDiff, CanvasDocument, CanvasSuggestion } from '../api/types';
import { markdownToCanvasHtml } from '../lib/canvasRichText';

interface InlineCanvasSuggestionProps {
  canvas: CanvasDocument;
  suggestion: CanvasSuggestion;
  onAccept: () => void;
  onReject: () => void;
  busy?: boolean;
}

/** True for a character-selection magic-edit diff (a delta inside one block). */
function isSelectionDiff(diff: CanvasDiff): boolean {
  return (
    diff.op === 'replace' &&
    typeof diff.selection_start === 'number' &&
    typeof diff.selection_end === 'number' &&
    typeof diff.selected_orig === 'string' &&
    typeof diff.selected_sugg === 'string'
  );
}

/**
 * A suggestion is "selection-scoped" only when every diff is a character-level
 * delta.  Those render the changed fragment in place (原文划掉 / 新文高亮) while
 * the rest of the block stays as context.  Otherwise the suggestion is a
 * block-level document edit (replace / delete / insert_after on whole blocks).
 */
function isSelectionScopedSuggestion(suggestion: CanvasSuggestion): boolean {
  return suggestion.diffs.length > 0 && suggestion.diffs.every(isSelectionDiff);
}

/** Full document with the selection deltas rendered in place at their block. */
function renderSelectionDocument(canvas: CanvasDocument, suggestion: CanvasSuggestion): ReactNode[] {
  const diffByBlock = new Map(suggestion.diffs.map((diff) => [diff.block_id, diff]));
  return canvas.blocks.map((block, index) => {
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
  });
}

/** A block whose content is being replaced, or a plain unchanged block. */
function renderBlockRow(
  key: string,
  text: string,
  diff: CanvasDiff | undefined,
  suggestionId: string,
): ReactNode {
  if (!diff) {
    return (
      <div
        key={key}
        data-canvas-block-id={key}
        dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(text) }}
      />
    );
  }

  if (diff.op === 'delete') {
    return (
      <div
        key={key}
        className="canvas-magic-edit-segment"
        data-canvas-block-id={key}
        data-op="delete"
      >
        <span className="canvas-magic-edit-tag" data-op="delete">
          Deleted
        </span>
        {diff.orig != null && (
          <div className="canvas-magic-edit-orig" data-me-role="orig" data-me-id={suggestionId}>
            {diff.orig}
          </div>
        )}
      </div>
    );
  }

  // replace: original block struck through, suggested block highlighted below.
  return (
    <div key={key} className="canvas-magic-edit-segment" data-canvas-block-id={key} data-op="replace">
      {diff.orig != null && (
        <div className="canvas-magic-edit-orig" data-me-role="orig" data-me-id={suggestionId}>
          {diff.orig}
        </div>
      )}
      {diff.sugg != null && (
        <div
          className="canvas-magic-edit-sugg"
          data-me-role="sugg"
          data-me-id={suggestionId}
          dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(diff.sugg) }}
        />
      )}
    </div>
  );
}

/** A brand-new block inserted after its anchor (highlighted, marked "Added"). */
function renderInsertRow(key: string, diff: CanvasDiff, suggestionId: string): ReactNode {
  return (
    <div
      key={key}
      className="canvas-magic-edit-segment"
      data-canvas-block-id={key}
      data-op="insert_after"
    >
      <span className="canvas-magic-edit-tag" data-op="insert_after">
        Added
      </span>
      {diff.sugg != null && (
        <div
          className="canvas-magic-edit-sugg"
          data-me-role="sugg"
          data-me-id={suggestionId}
          dangerouslySetInnerHTML={{ __html: markdownToCanvasHtml(diff.sugg) }}
        />
      )}
    </div>
  );
}

/** Full document with block-level diffs (replace / delete / insert_after) in place. */
function renderBlockDocument(canvas: CanvasDocument, suggestion: CanvasSuggestion): ReactNode[] {
  // replace / delete operate ON a block; insert_after appends a new block AFTER
  // its anchor. Group them so the document renders top-to-bottom in order.
  const onBlock = new Map<string, CanvasDiff>();
  const insertAfter = new Map<string, CanvasDiff[]>();
  for (const diff of suggestion.diffs) {
    if (diff.op === 'insert_after') {
      const list = insertAfter.get(diff.block_id) ?? [];
      list.push(diff);
      insertAfter.set(diff.block_id, list);
    } else {
      onBlock.set(diff.block_id, diff);
    }
  }

  const knownIds = new Set(canvas.blocks.map((block) => block.id));
  const nodes: ReactNode[] = [];
  canvas.blocks.forEach((block) => {
    nodes.push(renderBlockRow(block.id, block.text, onBlock.get(block.id), suggestion.id));
    (insertAfter.get(block.id) ?? []).forEach((diff, i) => {
      nodes.push(renderInsertRow(`${block.id}:insert:${i}`, diff, suggestion.id));
    });
  });

  // Orphan diffs (block_id not in the current canvas) — surface them at the end
  // so no proposed change is silently dropped.
  suggestion.diffs.forEach((diff, i) => {
    if (knownIds.has(diff.block_id)) return;
    if (diff.op === 'insert_after') {
      nodes.push(renderInsertRow(`orphan:${i}`, diff, suggestion.id));
    } else {
      nodes.push(renderBlockRow(`orphan:${i}`, diff.orig ?? '', diff, suggestion.id));
    }
  });

  return nodes;
}

/**
 * Renders a pending canvas suggestion in place, inside a copy of the document,
 * so the reader sees the change in its real context.  Both the character-scoped
 * magic edit (selection delta) and the whole-document edit (block replace /
 * delete / insert_after) render here; unchanged blocks stay as plain context.
 * The accepted canvas remains untouched until the user confirms.
 */
export function InlineCanvasSuggestion({
  canvas,
  suggestion,
  onAccept,
  onReject,
  busy = false,
}: InlineCanvasSuggestionProps) {
  const selectionScoped = isSelectionScopedSuggestion(suggestion);
  const label = selectionScoped ? 'Suggested edit (selection)' : 'Suggested edit';
  const summary = suggestion.change_summary?.trim() || suggestion.instruction;

  return (
    <section className="canvas-magic-edit" aria-label={label}>
      <header className="canvas-magic-edit-head">
        <div className="min-w-0">
          <div className="canvas-suggestion-kicker">{label}</div>
          <div className="canvas-suggestion-instruction">{summary}</div>
        </div>
        <div className="canvas-suggestion-actions">
          <button type="button" className="canvas-suggestion-reject" onClick={onReject} disabled={busy}>
            Discard
          </button>
          <button type="button" className="canvas-suggestion-accept" onClick={onAccept} disabled={busy}>
            Accept
          </button>
        </div>
      </header>

      <div className="canvas-rich-editor canvas-magic-edit-document">
        {selectionScoped
          ? renderSelectionDocument(canvas, suggestion)
          : renderBlockDocument(canvas, suggestion)}
      </div>
    </section>
  );
}
