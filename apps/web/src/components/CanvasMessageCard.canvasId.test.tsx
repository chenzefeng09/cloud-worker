/**
 * CanvasMessageCard — canvas.v2 (canvasId mode) suggestion + version history.
 *
 * The card, when bound to a server canvas via `canvasId`, renders the
 * SuggestionDiffView for an active suggestion and a "历史" dropdown that lists
 * versions and reverts. `api.canvas.*` is mocked module-wide so the store's
 * actions resolve without real network I/O.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CanvasMessageCard } from './CanvasMessageCard';
import { useCanvasStore } from '../stores/canvasStore';
import type { CanvasDocument, CanvasSuggestion, CanvasVersionSummary } from '../api/types';

const mockVersions = vi.fn();
const mockRevert = vi.fn();
const mockAccept = vi.fn();
const mockReject = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    canvas: {
      versions: (...a: unknown[]) => mockVersions(...a),
      revert: (...a: unknown[]) => mockRevert(...a),
      accept: (...a: unknown[]) => mockAccept(...a),
      reject: (...a: unknown[]) => mockReject(...a),
      get: vi.fn(),
      create: vi.fn(),
      edit: vi.fn(),
      saveVersion: vi.fn(),
    },
  },
}));

function seedCanvas(overrides: Partial<CanvasDocument> = {}) {
  useCanvasStore.getState().setCanvas({
    id: 'cvs_1',
    session_id: 'ses_test',
    kind: 'article',
    title: '测试',
    current_version: 3,
    source_message_id: null,
    source_run_id: null,
    blocks: [
      { id: 'blk_1', type: 'heading', text: '# 标题' },
      { id: 'blk_2', type: 'paragraph', text: '正文内容' },
    ],
    created_at: '2026-07-10T00:00:00Z',
    updated_at: '2026-07-10T00:00:00Z',
    ...overrides,
  });
}

function seedSuggestion(diffs: CanvasSuggestion['diffs']) {
  useCanvasStore.getState().setSuggestion('cvs_1', {
    id: 'csg_1',
    canvas_id: 'cvs_1',
    base_version: 3,
    status: 'proposed',
    instruction: '精简正文',
    diffs,
    change_summary: '精简了正文',
    created_at: '2026-07-10T00:00:00Z',
    resolved_at: null,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, 'requestAnimationFrame', {
    configurable: true,
    value: (cb: FrameRequestCallback) => window.setTimeout(cb, 0),
  });
});

afterEach(() => {
  useCanvasStore.getState().reset();
});

describe('CanvasMessageCard canvasId mode — SuggestionDiffView', () => {
  it('renders the per-block diff view for an active suggestion', async () => {
    seedCanvas();
    seedSuggestion([
      { block_id: 'blk_2', op: 'replace', orig: '正文内容', sugg: '精简后的正文' },
      { block_id: 'blk_2', op: 'insert_after', orig: null, sugg: '新增的一段' },
    ]);
    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    const diff = screen.getByLabelText('修改建议');
    expect(diff).toBeInTheDocument();
    expect(within(diff).getByText('精简了正文')).toBeInTheDocument();
    // "正文内容" also appears in the editor body, so scope to the diff region.
    expect(within(diff).getByText('正文内容')).toHaveClass('canvas-suggestion-diff-old');
    expect(within(diff).getByText('精简后的正文')).toHaveClass('canvas-suggestion-diff-new');
    expect(within(diff).getByText('新增的一段')).toHaveClass('canvas-suggestion-diff-new');
  });

  it('accepts the whole suggestion via the store', async () => {
    const user = userEvent.setup();
    seedCanvas();
    seedSuggestion([{ block_id: 'blk_2', op: 'replace', orig: '正文内容', sugg: '新正文' }]);
    mockAccept.mockResolvedValueOnce({
      id: 'cvs_1',
      session_id: 'ses_test',
      kind: 'article',
      title: '测试',
      current_version: 4,
      source_message_id: null,
      source_run_id: null,
      blocks: [
        { id: 'blk_1', type: 'heading', text: '# 标题' },
        { id: 'blk_2', type: 'paragraph', text: '新正文' },
      ],
      created_at: '2026-07-10T00:00:00Z',
      updated_at: '2026-07-10T00:01:00Z',
    } satisfies CanvasDocument);

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '接受' }));

    await waitFor(() => expect(mockAccept).toHaveBeenCalledWith('cvs_1', 'csg_1'));
  });
});

describe('CanvasMessageCard canvasId mode — version history', () => {
  const versionList: CanvasVersionSummary[] = [
    {
      id: 'cvv_3',
      canvas_id: 'cvs_1',
      version: 3,
      change_summary: '第三版改动',
      created_by: 'model',
      base_version: 2,
      created_at: '2026-07-10T00:02:00Z',
    },
    {
      id: 'cvv_1',
      canvas_id: 'cvs_1',
      version: 1,
      change_summary: null,
      created_by: 'user',
      base_version: null,
      created_at: '2026-07-10T00:00:00Z',
    },
  ];

  it('opens the history dropdown and lists versions', async () => {
    const user = userEvent.setup();
    seedCanvas();
    mockVersions.mockResolvedValueOnce({ items: versionList, next_cursor: null });

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '版本历史' }));

    await waitFor(() => expect(mockVersions).toHaveBeenCalledWith('cvs_1'));
    expect(await screen.findByText('第三版改动')).toBeInTheDocument();
    expect(screen.getByText('v3')).toBeInTheDocument();
    expect(screen.getByText('v1')).toBeInTheDocument();
    // created_by=user with no summary falls back to "手动编辑".
    expect(screen.getByText('手动编辑')).toBeInTheDocument();
  });

  it('reverts to a chosen version after confirmation', async () => {
    const user = userEvent.setup();
    seedCanvas();
    mockVersions.mockResolvedValueOnce({ items: versionList, next_cursor: null });
    mockRevert.mockResolvedValueOnce({
      id: 'cvs_1',
      session_id: 'ses_test',
      kind: 'article',
      title: '测试',
      current_version: 4,
      source_message_id: null,
      source_run_id: null,
      blocks: [{ id: 'blk_1', type: 'paragraph', text: '回退后的内容' }],
      created_at: '2026-07-10T00:00:00Z',
      updated_at: '2026-07-10T00:03:00Z',
    } satisfies CanvasDocument);
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '版本历史' }));
    await screen.findByText('第三版改动');

    await user.click(screen.getByTitle('回退到版本 1'));

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() => expect(mockRevert).toHaveBeenCalledWith('cvs_1', 1));
    confirmSpy.mockRestore();
  });

  it('does NOT revert when the user cancels the confirmation', async () => {
    const user = userEvent.setup();
    seedCanvas();
    mockVersions.mockResolvedValueOnce({ items: versionList, next_cursor: null });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '版本历史' }));
    await screen.findByText('第三版改动');

    await user.click(screen.getByTitle('回退到版本 3'));

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockRevert).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });
});
