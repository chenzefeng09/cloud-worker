import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi } from 'vitest';
import { SuggestionDiffView } from './SuggestionDiffView';
import type { CanvasSuggestion, CanvasDiff } from '../api/types';

function makeSuggestion(diffs: CanvasDiff[], overrides: Partial<CanvasSuggestion> = {}): CanvasSuggestion {
  return {
    id: 'csg_1',
    canvas_id: 'cvs_1',
    base_version: 2,
    status: 'proposed',
    instruction: '把第二段改简洁',
    diffs,
    change_summary: '精简了第二段',
    created_at: '2026-07-10T00:00:00Z',
    resolved_at: null,
    ...overrides,
  };
}

describe('SuggestionDiffView', () => {
  it('renders the change summary at the top', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([{ block_id: 'b1', op: 'replace', orig: '旧文', sugg: '新文' }])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByText('精简了第二段')).toBeInTheDocument();
  });

  it('falls back to the instruction when change_summary is empty', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion(
          [{ block_id: 'b1', op: 'replace', orig: '旧文', sugg: '新文' }],
          { change_summary: null },
        )}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(screen.getByText('把第二段改简洁')).toBeInTheDocument();
  });

  it('renders a replace op as orig (old) above sugg (new)', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([
          { block_id: 'b1', op: 'replace', orig: '这是原来的句子。', sugg: '这是改写后的句子。' },
        ])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const old = screen.getByText('这是原来的句子。');
    const neu = screen.getByText('这是改写后的句子。');
    expect(old).toHaveClass('canvas-suggestion-diff-old');
    expect(neu).toHaveClass('canvas-suggestion-diff-new');
  });

  it('renders a delete op with the orig text and no suggested text', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([
          { block_id: 'b2', op: 'delete', orig: '要删掉的整段。', sugg: null },
        ])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const old = screen.getByText('要删掉的整段。');
    expect(old).toHaveClass('canvas-suggestion-diff-old');
    // No "new" highlighted block for a pure deletion.
    expect(document.querySelector('.canvas-suggestion-diff-new')).toBeNull();
    // The op tag marks it as a deletion.
    const block = old.closest('.canvas-suggestion-diff-block');
    expect(block).toHaveAttribute('data-op', 'delete');
    expect(screen.getByText('删除')).toBeInTheDocument();
  });

  it('renders an insert_after op as a new highlighted block with no orig', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([
          { block_id: 'b1', op: 'insert_after', orig: null, sugg: '这是新插入的一段。' },
        ])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    const neu = screen.getByText('这是新插入的一段。');
    expect(neu).toHaveClass('canvas-suggestion-diff-new');
    expect(document.querySelector('.canvas-suggestion-diff-old')).toBeNull();
    const block = neu.closest('.canvas-suggestion-diff-block');
    expect(block).toHaveAttribute('data-op', 'insert_after');
    expect(screen.getByText('新增')).toBeInTheDocument();
  });

  it('renders all three op kinds together', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([
          { block_id: 'b1', op: 'replace', orig: '原一', sugg: '新一' },
          { block_id: 'b2', op: 'delete', orig: '原二', sugg: null },
          { block_id: 'b2', op: 'insert_after', orig: null, sugg: '新三' },
        ])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
      />,
    );
    expect(document.querySelectorAll('.canvas-suggestion-diff-block')).toHaveLength(3);
    expect(screen.getByText('原一')).toHaveClass('canvas-suggestion-diff-old');
    expect(screen.getByText('新一')).toHaveClass('canvas-suggestion-diff-new');
    expect(screen.getByText('原二')).toHaveClass('canvas-suggestion-diff-old');
    expect(screen.getByText('新三')).toHaveClass('canvas-suggestion-diff-new');
  });

  it('calls onAccept / onReject for the whole suggestion (no per-block buttons)', async () => {
    const user = userEvent.setup();
    const onAccept = vi.fn();
    const onReject = vi.fn();
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([
          { block_id: 'b1', op: 'replace', orig: '原一', sugg: '新一' },
          { block_id: 'b2', op: 'replace', orig: '原二', sugg: '新二' },
        ])}
        onAccept={onAccept}
        onReject={onReject}
      />,
    );
    // Exactly one accept + one reject button for the whole suggestion.
    expect(screen.getAllByRole('button', { name: '接受' })).toHaveLength(1);
    expect(screen.getAllByRole('button', { name: '放弃' })).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: '接受' }));
    await user.click(screen.getByRole('button', { name: '放弃' }));
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it('disables both buttons while busy', () => {
    render(
      <SuggestionDiffView
        suggestion={makeSuggestion([{ block_id: 'b1', op: 'replace', orig: '原', sugg: '新' }])}
        onAccept={vi.fn()}
        onReject={vi.fn()}
        busy
      />,
    );
    expect(screen.getByRole('button', { name: '接受' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '放弃' })).toBeDisabled();
  });
});
