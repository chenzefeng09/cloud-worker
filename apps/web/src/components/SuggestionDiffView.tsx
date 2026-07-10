/**
 * SuggestionDiffView — per-block orig/sugg review for a canvas v2 suggestion.
 *
 * Renders the suggestion's diffs block-by-block:
 *   - replace       → orig (struck-through, muted) above sugg (highlighted)
 *   - delete        → orig (struck-through) with a "删除" marker
 *   - insert_after  → sugg only, marked as a new block (highlighted)
 *
 * The change_summary sits at the top. Accept / discard operate on the WHOLE
 * suggestion (the orchestrator accept is a single transaction over all diffs);
 * per-block partial accept is intentionally NOT offered — offering it here
 * would let the UI imply a granularity the backend can't honour, creating a
 * front/back inconsistency.
 *
 * Pure presentational: it takes a suggestion + callbacks and holds no store
 * wiring, so it renders identically in tests and in the card.
 */
import type { CanvasSuggestion } from '../api/types';

interface SuggestionDiffViewProps {
  suggestion: CanvasSuggestion;
  /** Accept the whole suggestion (single orchestrator transaction). */
  onAccept: () => void;
  /** Discard the whole suggestion. */
  onReject: () => void;
  /** Disable both buttons while an accept/reject is in flight. */
  busy?: boolean;
}

const OP_LABEL: Record<CanvasSuggestion['diffs'][number]['op'], string> = {
  replace: '修改',
  delete: '删除',
  insert_after: '新增',
};

export function SuggestionDiffView({
  suggestion,
  onAccept,
  onReject,
  busy = false,
}: SuggestionDiffViewProps) {
  const summary = suggestion.change_summary?.trim() || suggestion.instruction;
  const diffs = suggestion.diffs;

  return (
    <section className="canvas-suggestion-diff" aria-label="修改建议">
      <header className="canvas-suggestion-diff-head">
        <div className="min-w-0">
          <div className="canvas-suggestion-diff-kicker">修改建议</div>
          {summary && <div className="canvas-suggestion-diff-summary">{summary}</div>}
        </div>
        <div className="canvas-suggestion-diff-actions">
          <button
            type="button"
            className="canvas-suggestion-diff-reject"
            onClick={onReject}
            disabled={busy}
          >
            放弃
          </button>
          <button
            type="button"
            className="canvas-suggestion-diff-accept"
            onClick={onAccept}
            disabled={busy}
          >
            接受
          </button>
        </div>
      </header>

      <div className="canvas-suggestion-diff-body">
        {diffs.length === 0 && (
          <div className="canvas-suggestion-diff-empty">本次建议没有可应用的变更。</div>
        )}
        {diffs.map((diff, i) => (
          <div
            key={`${diff.block_id}_${i}`}
            className="canvas-suggestion-diff-block"
            data-op={diff.op}
          >
            <span className="canvas-suggestion-diff-op-tag" data-op={diff.op}>
              {OP_LABEL[diff.op]}
            </span>
            {/* replace + delete show the original (struck-through). insert_after
                has no original text. */}
            {(diff.op === 'replace' || diff.op === 'delete') && diff.orig != null && (
              <div className="canvas-suggestion-diff-old">{diff.orig}</div>
            )}
            {/* replace + insert_after show the suggested text (highlighted).
                delete has no suggested text. */}
            {(diff.op === 'replace' || diff.op === 'insert_after') && diff.sugg != null && (
              <div className="canvas-suggestion-diff-new">{diff.sugg}</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}
