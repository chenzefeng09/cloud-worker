import { useState, type ReactElement } from 'react';
import { fireEvent, render as rtlRender, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CanvasMessageCard } from './CanvasMessageCard';
import { DialogProvider } from '../admin/components/dialogs';
import {
  applyCanvasReplacement,
  type CanvasDocumentDraft,
  type CanvasEditRequest,
  type CanvasEditSuggestion,
} from '../lib/canvasObjects';
import { useCanvasStore } from '../stores/canvasStore';

/** The card calls useConfirm() (version revert), which needs the shell provider. */
const render = (ui: ReactElement) => rtlRender(<DialogProvider>{ui}</DialogProvider>);

function makeDraft(content: string): CanvasDocumentDraft {
  return {
    id: 'canvas_test',
    kind: 'article',
    title: '测试文章',
    content,
    updatedAt: '2026-06-14T00:00:00.000Z',
  };
}

function rect(): DOMRect {
  return {
    x: 120,
    y: 120,
    left: 120,
    top: 120,
    right: 180,
    bottom: 140,
    width: 60,
    height: 20,
    toJSON: () => ({}),
  };
}

function findTextNode(root: Node, text: string): Text | null {
  for (const child of Array.from(root.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE && child.textContent?.includes(text)) {
      return child as Text;
    }
    const nested = findTextNode(child, text);
    if (nested) return nested;
  }
  return null;
}

function selectText(root: HTMLElement, text: string): void {
  const textNode = findTextNode(root, text);
  if (!textNode || !textNode.textContent) throw new Error(`Text not found: ${text}`);
  const start = textNode.textContent.indexOf(text);
  const range = document.createRange();
  range.setStart(textNode, start);
  range.setEnd(textNode, start + text.length);

  const selection = window.getSelection();
  if (!selection) throw new Error('Selection API unavailable');
  selection.removeAllRanges();
  selection.addRange(range);
}

describe('CanvasMessageCard', () => {
  beforeEach(() => {
    Object.defineProperty(Range.prototype, 'getBoundingClientRect', {
      configurable: true,
      value: vi.fn(rect),
    });
    Object.defineProperty(window, 'requestAnimationFrame', {
      configurable: true,
      value: (callback: FrameRequestCallback) => window.setTimeout(callback, 0),
    });
  });

  afterEach(() => {
    useCanvasStore.getState().reset();
  });

  it('renders markdown as editable rich text', async () => {
    const draft = makeDraft('# 一级标题\n\n这是一段正文。');
    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
      />,
    );

    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });
    await waitFor(() => expect(editor.querySelector('h1')).toHaveTextContent('一级标题'));
    expect(editor).toHaveTextContent('这是一段正文。');
  });

  it('pastes markdown blocks as rich text instead of visible markdown source', async () => {
    const changes: CanvasDocumentDraft[] = [];

    function ControlledCard() {
      const [draft, setDraft] = useState(makeDraft(''));
      return (
        <CanvasMessageCard
          sessionId="ses_test"
          draft={draft}
          onDraftChange={(next) => {
            changes.push(next);
            setDraft(next);
          }}
          onRequestEdit={vi.fn()}
        />
      );
    }

    render(<ControlledCard />);
    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });

    fireEvent.paste(editor, {
      clipboardData: {
        getData: (type: string) =>
          type === 'text/plain' ? '# 粘贴标题\n\n- 第一项\n- 第二项' : '',
      },
    });

    await waitFor(() => expect(editor.querySelector('h1')).toHaveTextContent('粘贴标题'));
    expect(editor).not.toHaveTextContent('# 粘贴标题');
    expect(editor.querySelectorAll('li')).toHaveLength(2);
    expect(changes[changes.length - 1]?.content).toBe('# 粘贴标题\n\n- 第一项\n- 第二项');
  });

  it('creates a selection edit request from the floating toolbar', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。\n\n这一句需要修改，保持其他内容不变。');
    const onRequestEdit = vi.fn<(_: CanvasEditRequest) => Promise<void>>().mockResolvedValue();

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        onDraftChange={vi.fn()}
        onRequestEdit={onRequestEdit}
      />,
    );

    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });
    await waitFor(() => expect(editor).toHaveTextContent('这一句需要修改'));

    selectText(editor, '这一句需要修改');
    fireEvent.mouseUp(editor);

    await user.click(await screen.findByText('Request changes'));
    await user.type(screen.getByPlaceholderText('Edit this'), '改得更口语');
    await user.click(screen.getByTitle('Send edit request'));

    await waitFor(() => expect(onRequestEdit).toHaveBeenCalledTimes(1));
    // The revision no longer produces a chat prompt — the callback receives the
    // structured request only, and the BFF endpoint does the rest silently.
    expect(onRequestEdit.mock.calls[0]).toHaveLength(1);
    const [request] = onRequestEdit.mock.calls[0];
    expect(request.scope).toBe('selection');
    expect(request.selection?.text).toBe('这一句需要修改');
    expect(request.instruction).toBe('改得更口语');
  });

  it('keeps the floating-toolbar instruction separate from the footer instruction', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。\n\n这一句需要修改，保持其他内容不变。');

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
      />,
    );

    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });
    await waitFor(() => expect(editor).toHaveTextContent('这一句需要修改'));

    selectText(editor, '这一句需要修改');
    fireEvent.mouseUp(editor);
    await user.click(await screen.findByText('Request changes'));

    const popupInput = screen.getByPlaceholderText('Edit this');
    await user.type(popupInput, '改得更有力');

    expect(popupInput).toHaveValue('改得更有力');
    // The footer instruction input (separate from the popup) stays empty.
    expect(screen.getByPlaceholderText('Describe the changes you want…')).toHaveValue('');
  });

  it('dismisses the floating toolbar with Escape or an outside click', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。\n\n这一句需要修改，保持其他内容不变。');

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
      />,
    );

    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });
    await waitFor(() => expect(editor).toHaveTextContent('这一句需要修改'));

    selectText(editor, '这一句需要修改');
    fireEvent.mouseUp(editor);
    await user.click(await screen.findByText('Request changes'));
    const input = screen.getByPlaceholderText('Edit this');

    fireEvent.keyDown(input, { key: 'Escape' });
    await waitFor(() =>
      expect(screen.queryByPlaceholderText('Edit this')).not.toBeInTheDocument(),
    );

    selectText(editor, '这一句需要修改');
    fireEvent.mouseUp(editor);
    await user.click(await screen.findByText('Request changes'));
    expect(screen.getByPlaceholderText('Edit this')).toBeInTheDocument();

    fireEvent.pointerDown(document.body);
    await waitFor(() =>
      expect(screen.queryByPlaceholderText('Edit this')).not.toBeInTheDocument(),
    );
  });

  it('shows a revising indicator and blocks re-submission while a revision is in flight', async () => {
    const draft = makeDraft('第一段文字。\n\n这一句需要修改。');
    const onRequestEdit = vi.fn<(_: CanvasEditRequest) => Promise<void>>().mockResolvedValue();

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        revising
        onDraftChange={vi.fn()}
        onRequestEdit={onRequestEdit}
      />,
    );

    // Visual "Revising…" feedback appears (status role + input placeholder).
    expect(screen.getByRole('status')).toHaveTextContent('Revising…');
    expect(screen.getByPlaceholderText('Revising…')).toBeDisabled();

    // The whole-document request button is disabled — no double submit.
    expect(screen.getByRole('button', { name: 'Revise' })).toBeDisabled();
    expect(onRequestEdit).not.toHaveBeenCalled();
  });

  it('surfaces a revision error inside the card and lets the user dismiss it', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。');
    const onDismissEditError = vi.fn();

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        editError="模型暂时不可用，请稍后重试。"
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
        onDismissEditError={onDismissEditError}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('模型暂时不可用，请稍后重试。');
    await user.click(screen.getByRole('button', { name: 'Dismiss error' }));
    expect(onDismissEditError).toHaveBeenCalledTimes(1);
  });

  it('shows an AI suggestion before applying it', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。\n\n这一句需要修改，保持其他内容不变。');
    const request: CanvasEditRequest = {
      documentId: draft.id,
      kind: draft.kind,
      title: draft.title,
      scope: 'selection',
      instruction: '改得更口语',
      content: draft.content,
      selection: {
        text: '这一句需要修改',
        start: draft.content.indexOf('这一句需要修改'),
        end: draft.content.indexOf('这一句需要修改') + '这一句需要修改'.length,
      },
    };
    const onAcceptSuggestion = vi.fn();
    const onRejectSuggestion = vi.fn();

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        suggestion={{
          request,
          replacement: '这句话可以说得更自然一点',
          receivedAt: '2026-06-14T00:01:00.000Z',
        }}
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
        onAcceptSuggestion={onAcceptSuggestion}
        onRejectSuggestion={onRejectSuggestion}
      />,
    );

    expect(screen.getByLabelText('Suggested edit (selection)')).toHaveTextContent('改得更口语');
    expect(screen.getByText('第一段文字。')).toBeInTheDocument();
    expect(screen.getByText(/保持其他内容不变/)).toBeInTheDocument();
    expect(screen.getByText('这一句需要修改')).toHaveClass('canvas-inline-suggestion-old');
    expect(screen.getByText('这句话可以说得更自然一点')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Accept' }));
    await user.click(screen.getByRole('button', { name: 'Discard' }));

    expect(onAcceptSuggestion).toHaveBeenCalledTimes(1);
    expect(onRejectSuggestion).toHaveBeenCalledTimes(1);
  });

  it('renders a whole-document suggestion (apply_to=document) without splicing the selection', async () => {
    const user = userEvent.setup();
    const draft = makeDraft('第一段文字。\n\n这是文末的结尾句需要改。');
    // AgentPage overrides scope to 'document' when the BFF returns apply_to==='document'
    // (model echoed the whole doc), keeping the original selection for display only.
    const request: CanvasEditRequest = {
      documentId: draft.id,
      kind: draft.kind,
      title: draft.title,
      scope: 'document',
      instruction: '把结尾改得更有力',
      content: draft.content,
      selection: {
        text: '这是文末的结尾句需要改',
        start: draft.content.indexOf('这是文末的结尾句需要改'),
        end: draft.content.indexOf('这是文末的结尾句需要改') + '这是文末的结尾句需要改'.length,
      },
    };
    const replacement = '第一段文字。\n\n结尾改写成了一句更有力的收束。';
    const onAcceptSuggestion = vi.fn();

    render(
      <CanvasMessageCard
        sessionId="ses_test"
        draft={draft}
        suggestion={{
          request,
          replacement,
          receivedAt: '2026-06-14T00:01:00.000Z',
        }}
        onDraftChange={vi.fn()}
        onRequestEdit={vi.fn()}
        onAcceptSuggestion={onAcceptSuggestion}
        onRejectSuggestion={vi.fn()}
      />,
    );

    // Whole-document card form, not the inline selection diff.
    expect(screen.getByLabelText('Suggested edit (whole document)')).toHaveTextContent('把结尾改得更有力');
    // The inline selection diff card ("Suggested edit (selection)") must NOT render.
    expect(screen.queryByLabelText('Suggested edit (selection)')).not.toBeInTheDocument();
    // The whole-document replacement is previewed as-is (no duplicated body).
    expect(screen.getByText('结尾改写成了一句更有力的收束。')).toBeInTheDocument();

    // Applying the document suggestion cleanly replaces the whole content.
    const applied = applyCanvasReplacement(draft, request, replacement);
    expect(applied.content).toBe(replacement);
    // No duplication of the surrounding body.
    expect(applied.content.match(/第一段文字。/g)).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Accept' }));
    expect(onAcceptSuggestion).toHaveBeenCalledTimes(1);
  });

  it('records accepted AI suggestions in the local undo stack', async () => {
    const user = userEvent.setup();
    const initialDraft = makeDraft('第一段文字。\n\n这一句需要修改，保持其他内容不变。');
    const request: CanvasEditRequest = {
      documentId: initialDraft.id,
      kind: initialDraft.kind,
      title: initialDraft.title,
      scope: 'selection',
      instruction: '改得更口语',
      content: initialDraft.content,
      selection: {
        text: '这一句需要修改',
        start: initialDraft.content.indexOf('这一句需要修改'),
        end: initialDraft.content.indexOf('这一句需要修改') + '这一句需要修改'.length,
      },
    };

    function ControlledCard() {
      const [draft, setDraft] = useState(initialDraft);
      const [suggestion, setSuggestion] = useState<CanvasEditSuggestion | null>({
        request,
        replacement: '这句话可以说得更自然一点',
        receivedAt: '2026-06-14T00:01:00.000Z',
      });

      return (
        <CanvasMessageCard
          sessionId="ses_test"
          draft={draft}
          suggestion={suggestion}
          onDraftChange={setDraft}
          onRequestEdit={vi.fn()}
          onAcceptSuggestion={() => {
            setDraft((current) => applyCanvasReplacement(current, request, '这句话可以说得更自然一点'));
            setSuggestion(null);
          }}
          onRejectSuggestion={() => setSuggestion(null)}
        />
      );
    }

    render(<ControlledCard />);

    await user.click(screen.getByRole('button', { name: 'Accept' }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Inline canvas body' })).toHaveTextContent('这句话可以说得更自然一点'));
    expect(screen.queryByText('这一句需要修改')).not.toBeInTheDocument();

    await user.click(screen.getByTitle('Undo'));

    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Inline canvas body' })).toHaveTextContent('这一句需要修改'));
    expect(screen.queryByText('这句话可以说得更自然一点')).not.toBeInTheDocument();
  });

  it('renders in canvasId mode by binding to a server canvas from canvasStore', async () => {
    useCanvasStore.getState().setCanvas({
      id: 'cvs_1',
      session_id: 'ses_test',
      kind: 'article',
      title: '测试',
      current_version: 1,
      blocks: [
        { id: 'blk_1', type: 'heading', text: '# 标题' },
        { id: 'blk_2', type: 'paragraph', text: '正文内容' },
      ],
      created_at: '2026-06-14T00:00:00.000Z',
      updated_at: '2026-06-14T00:00:00.000Z',
    });

    render(<CanvasMessageCard sessionId="ses_test" canvasId="cvs_1" />);

    expect(screen.getByDisplayValue('测试')).toBeInTheDocument();
    const editor = screen.getByRole('textbox', { name: 'Inline canvas body' });
    await waitFor(() => expect(editor.querySelector('h1')).toHaveTextContent('标题'));
    expect(editor).toHaveTextContent('正文内容');
  });
});
