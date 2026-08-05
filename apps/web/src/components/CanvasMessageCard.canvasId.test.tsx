/**
 * CanvasMessageCard — canvas.v2 (canvasId mode) suggestion + version history.
 *
 * The card, when bound to a server canvas via `canvasId`, renders an active
 * suggestion in place (InlineCanvasSuggestion — the full document with the
 * changed blocks highlighted) and a "历史" dropdown that lists versions and
 * reverts. `api.canvas.*` is mocked module-wide so the store's actions resolve
 * without real network I/O.
 */
import type { ReactElement } from 'react';
import { act, fireEvent, render as rtlRender, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CanvasMessageCard } from './CanvasMessageCard';
import { DialogProvider } from '../admin/components/dialogs';
import { useCanvasStore } from '../stores/canvasStore';
import type { CanvasDocument, CanvasSuggestion, CanvasVersionSummary } from '../api/types';

const mockVersions = vi.fn();
const mockRevert = vi.fn();
const mockAccept = vi.fn();
const mockReject = vi.fn();
const mockEdit = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    canvas: {
      versions: (...a: unknown[]) => mockVersions(...a),
      revert: (...a: unknown[]) => mockRevert(...a),
      accept: (...a: unknown[]) => mockAccept(...a),
      reject: (...a: unknown[]) => mockReject(...a),
      get: vi.fn(),
      create: vi.fn(),
      edit: (...a: unknown[]) => mockEdit(...a),
      saveVersion: vi.fn(),
    },
  },
}));

/** Revert confirmation goes through the shell's in-app dialog, not window.confirm. */
const render = (ui: ReactElement) => rtlRender(<DialogProvider>{ui}</DialogProvider>);

function seedCanvas(overrides: Partial<CanvasDocument> = {}) {
  act(() => {
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
  });
}

function seedSuggestion(diffs: CanvasSuggestion['diffs']) {
  act(() => {
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
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  Object.defineProperty(window, 'requestAnimationFrame', {
    configurable: true,
    value: (cb: FrameRequestCallback) => window.setTimeout(cb, 0),
  });
});

function selectText(root: HTMLElement, value: string): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node && !node.textContent?.includes(value)) node = walker.nextNode();
  if (!(node instanceof Text)) throw new Error(`Text not found: ${value}`);
  const start = node.data.indexOf(value);
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, start + value.length);
  Object.defineProperty(range, 'getBoundingClientRect', {
    value: () => ({ left: 100, top: 100, width: 50, height: 20 }),
  });
  window.getSelection()?.removeAllRanges();
  window.getSelection()?.addRange(range);
}

afterEach(() => {
  act(() => useCanvasStore.getState().reset());
});

describe('CanvasMessageCard canvasId mode — inline suggestion', () => {
  it('sends a selected fragment as an explicit block-local scope', async () => {
    const user = userEvent.setup();
    seedCanvas();
    mockEdit.mockResolvedValueOnce({
      suggestion: {
        id: 'csg_2',
        canvas_id: 'cvs_1',
        base_version: 3,
        status: 'proposed',
        instruction: '改得更有力',
        diffs: [],
        created_at: '2026-07-10T00:00:00Z',
      },
    });
    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    const editor = screen.getByRole('textbox', { name: '页内画布正文' });
    await waitFor(() => expect(editor).toHaveTextContent('正文内容'));
    selectText(editor, '内容');
    fireEvent.mouseUp(editor);
    await user.click(await screen.findByText('要求更改'));
    await user.type(screen.getByPlaceholderText('修改一下'), '改得更有力');
    await user.click(screen.getByTitle('发送修改要求'));

    await waitFor(() => expect(mockEdit).toHaveBeenCalledTimes(1));
    expect(mockEdit).toHaveBeenCalledWith(
      'ses_test',
      'cvs_1',
      expect.objectContaining({
        instruction: '改得更有力',
        base_version: 3,
        scope: {
          type: 'selection',
          segments: [{ block_id: 'blk_2', start: 2, end: 4, text: '内容' }],
        },
      }),
    );
  });

  it('renders block-level diffs in place inside the full document', async () => {
    seedCanvas();
    seedSuggestion([
      { block_id: 'blk_2', op: 'replace', orig: '正文内容', sugg: '精简后的正文' },
      { block_id: 'blk_2', op: 'insert_after', orig: null, sugg: '新增的一段' },
    ]);
    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    const review = screen.getByLabelText('修改建议');
    expect(review).toBeInTheDocument();
    expect(within(review).getByText('精简了正文')).toBeInTheDocument();
    // The suggestion renders the WHOLE document in context — the unchanged
    // heading block is still shown, not just the changed blocks.
    expect(within(review).getByText('标题')).toBeInTheDocument();
    // The replaced block shows its original struck through and the suggested
    // text highlighted; the inserted block is marked 新增.
    expect(within(review).getByText('正文内容')).toHaveClass('canvas-magic-edit-orig');
    const suggs = Array.from(review.querySelectorAll('.canvas-magic-edit-sugg'));
    expect(suggs.some((el) => el.textContent === '精简后的正文')).toBe(true);
    expect(suggs.some((el) => el.textContent === '新增的一段')).toBe(true);
    expect(within(review).getByText('新增')).toBeInTheDocument();
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

  it('renders character suggestions inline with orig/sugg scope markers', () => {
    seedCanvas();
    seedSuggestion([
      {
        block_id: 'blk_2',
        op: 'replace',
        orig: '正文内容',
        sugg: '正文更有力',
        selection_start: 2,
        selection_end: 4,
        selected_orig: '内容',
        selected_sugg: '更有力',
      },
    ]);

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    const review = screen.getByLabelText('选区修改建议');
    const orig = review.querySelector('[data-magic-edit-block="true"][data-me-role="orig"]');
    const sugg = review.querySelector('[data-magic-edit-block="true"][data-me-role="sugg"]');
    expect(orig).toHaveTextContent('内容');
    expect(orig).toHaveAttribute('data-me-id', 'csg_1');
    expect(sugg).toHaveTextContent('更有力');
    expect(screen.queryByLabelText('修改建议')).not.toBeInTheDocument();
  });
});

describe('CanvasMessageCard canvasId mode — executable HTML canvas', () => {
  const HTML = '<!DOCTYPE html><html><body><canvas id="g"></canvas><script>1</script></body></html>';

  it('renders a sandboxed iframe (not the prose editor) for an html canvas', () => {
    seedCanvas({
      content_type: 'html',
      title: '打砖块游戏',
      blocks: [{ id: 'blk_h', type: 'html', text: HTML }],
    });
    const { container } = render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    // The executable branch renders an iframe with the HTML as srcDoc, sandboxed
    // WITHOUT allow-same-origin.
    const iframe = container.querySelector('iframe');
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute('sandbox')).toBe('allow-scripts');
    expect(iframe?.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(iframe?.getAttribute('srcdoc')).toBe(HTML);

    // 预览 / 代码 toggle present; the prose editor is NOT rendered.
    expect(screen.getByText('预览')).toBeInTheDocument();
    expect(screen.getByText('代码')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '页内画布正文' })).not.toBeInTheDocument();
  });

  it('shows the raw source (not an iframe) in code mode', async () => {
    const user = userEvent.setup();
    seedCanvas({
      content_type: 'html',
      title: '打砖块游戏',
      blocks: [{ id: 'blk_h', type: 'html', text: HTML }],
    });
    const { container } = render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    await user.click(screen.getByText('代码'));
    expect(container.querySelector('iframe')).toBeNull();
    expect(container.querySelector('pre code')?.textContent).toBe(HTML);
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

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '版本历史' }));
    await screen.findByText('第三版改动');

    await user.click(screen.getByTitle('回退到版本 1'));

    await screen.findByText('回退版本');
    await user.click(screen.getByRole('button', { name: '回退' }));
    await waitFor(() => expect(mockRevert).toHaveBeenCalledWith('cvs_1', 1));
  });

  it('does NOT revert when the user cancels the confirmation', async () => {
    const user = userEvent.setup();
    seedCanvas();
    mockVersions.mockResolvedValueOnce({ items: versionList, next_cursor: null });

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);
    await user.click(screen.getByRole('button', { name: '版本历史' }));
    await screen.findByText('第三版改动');

    await user.click(screen.getByTitle('回退到版本 3'));

    await screen.findByText('回退版本');
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(mockRevert).not.toHaveBeenCalled();
  });
});
