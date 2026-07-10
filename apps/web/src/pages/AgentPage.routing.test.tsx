/**
 * AgentPage — canvas.v2 three-state intent routing wiring (design §6.3 Phase A).
 *
 * These tests exercise the send-path interception in
 * `sendMessageWithCanvasDetection`:
 *   1. flag ON + active canvas + modify-intent → requestEdit called, NO chat
 *      message sent.
 *   2. flag ON + active canvas + NON-modify (chat) message → normal sendMessage,
 *      requestEdit NOT called.
 *   3. flag ON + NO active canvas → normal sendMessage.
 *   4. flag OFF → zero change: normal sendMessage even for a modify message.
 *   5. requestEdit failure (returns null) → the typed text is refilled into the
 *      composer (fail-open, nothing lost).
 *
 * ChatWindow is stubbed to a thin harness that surfaces the two callbacks the
 * routing depends on (`onOpenCanvas` to seed an active canvas, `onSendMessage`
 * to trigger a send) plus the current `externalPrefill` value, so the tests
 * drive real AgentPage state without the full SSE/composer surface.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AgentPage } from './AgentPage';
import { useCanvasStore } from '../stores/canvasStore';
import type { CanvasDocument, CanvasSuggestion } from '../api/types';

// ── api.canvas mock (drives the real canvasStore actions) ────────────────────
const mockCreateCanvas = vi.fn();
const mockEditCanvas = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    canvas: {
      create: (...a: unknown[]) => mockCreateCanvas(...a),
      edit: (...a: unknown[]) => mockEditCanvas(...a),
      get: vi.fn(),
      versions: vi.fn(),
      accept: vi.fn(),
      reject: vi.fn(),
      revert: vi.fn(),
      saveVersion: vi.fn(),
    },
    sessions: { canvasEdit: vi.fn() },
  },
}));

// ── useChat mock — capture sendMessage, feed empty transcript ────────────────
const mockSendMessage = vi.fn().mockResolvedValue(undefined);
vi.mock('../hooks/useChat', () => ({
  useChat: () => ({
    sendMessage: mockSendMessage,
    isStreaming: false,
    messages: [],
  }),
}));

// ── feature flag mock — toggled per-test via a mutable ref ───────────────────
const flagState = { canvasV2: true };
vi.mock('../hooks/useFeatureFlags', () => ({
  useFeatureFlag: (key: string) => (key === 'canvas.v2' ? flagState.canvasV2 : false),
}));

// ── useSessions mock ─────────────────────────────────────────────────────────
vi.mock('../hooks/useSessions', () => ({
  useSessions: () => ({ sessions: [{ id: 'ses_1', title: 'T', status: 'ACTIVE' }] }),
}));

// ── ArticleCanvasPanel is unused in these tests but imported by AgentPage ─────
vi.mock('../components/ArticleCanvasPanel', () => ({
  ArticleCanvasPanel: () => null,
  loadStoredCanvasDraft: () => null,
}));

// ── ChatWindow harness — surfaces onOpenCanvas + onSendMessage + prefill ─────
interface HarnessProps {
  onOpenCanvas?: (seed: { content: string; sourceMessageId?: string }) => void;
  onSendMessage?: (text: string) => void | Promise<void>;
  inlineCanvasV2Id?: string | null;
  externalPrefill?: string | null;
}
vi.mock('../components/ChatWindow', () => ({
  ChatWindow: (props: HarnessProps) => (
    <div>
      <button onClick={() => props.onOpenCanvas?.({ content: '一些正文内容用来物化画布。' })}>
        seed-canvas
      </button>
      <button onClick={() => props.onSendMessage?.('把这段改简洁')}>send-modify</button>
      <button onClick={() => props.onSendMessage?.('这段写得怎么样')}>send-chat</button>
      <div data-testid="active-canvas">{props.inlineCanvasV2Id ?? ''}</div>
      <div data-testid="prefill">{props.externalPrefill ?? ''}</div>
    </div>
  ),
}));

function canvas(overrides: Partial<CanvasDocument> = {}): CanvasDocument {
  return {
    id: 'cvs_1',
    session_id: 'ses_1',
    kind: 'article',
    title: '测试',
    current_version: 1,
    source_message_id: null,
    source_run_id: null,
    blocks: [{ id: 'blk_1', type: 'paragraph', text: '正文' }],
    created_at: '2026-07-10T00:00:00Z',
    updated_at: '2026-07-10T00:00:00Z',
    ...overrides,
  };
}

function suggestion(): CanvasSuggestion {
  return {
    id: 'csg_1',
    canvas_id: 'cvs_1',
    base_version: 1,
    status: 'proposed',
    instruction: '把这段改简洁',
    diffs: [{ block_id: 'blk_1', op: 'replace', orig: '正文', sugg: '精简正文' }],
    change_summary: '精简',
    created_at: '2026-07-10T00:00:00Z',
    resolved_at: null,
  };
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/tasks/ses_1']}>
      <Routes>
        <Route path="/tasks/:id" element={<AgentPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

async function seedActiveCanvas(user: ReturnType<typeof userEvent.setup>) {
  mockCreateCanvas.mockResolvedValueOnce(canvas());
  await user.click(screen.getByText('seed-canvas'));
  await waitFor(() => expect(screen.getByTestId('active-canvas')).toHaveTextContent('cvs_1'));
}

describe('AgentPage — canvas.v2 intent routing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    flagState.canvasV2 = true;
    useCanvasStore.getState().reset();
  });

  it('routes a modify-intent message to requestEdit and does NOT send a chat message', async () => {
    const user = userEvent.setup();
    mockEditCanvas.mockResolvedValueOnce({ suggestion: suggestion() });
    renderPage();
    await seedActiveCanvas(user);

    await user.click(screen.getByText('send-modify'));

    await waitFor(() => expect(mockEditCanvas).toHaveBeenCalledTimes(1));
    // requestEdit hit the canvas edit endpoint with the user's instruction …
    const [sessionId, canvasId, body] = mockEditCanvas.mock.calls[0];
    expect(sessionId).toBe('ses_1');
    expect(canvasId).toBe('cvs_1');
    expect(body.instruction).toBe('把这段改简洁');
    expect(body.scope).toEqual({ type: 'document' });
    // … and NO chat message was sent.
    expect(mockSendMessage).not.toHaveBeenCalled();
  });

  it('routes a non-modify message to the normal chat path (requestEdit NOT called)', async () => {
    const user = userEvent.setup();
    renderPage();
    await seedActiveCanvas(user);

    await user.click(screen.getByText('send-chat'));

    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledTimes(1));
    expect(mockSendMessage).toHaveBeenCalledWith('这段写得怎么样', undefined, undefined);
    expect(mockEditCanvas).not.toHaveBeenCalled();
  });

  it('does NOT hijack when there is no active canvas', async () => {
    const user = userEvent.setup();
    renderPage();
    // No seed-canvas click → inlineCanvasV2Id stays null.
    await user.click(screen.getByText('send-modify'));

    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledTimes(1));
    expect(mockEditCanvas).not.toHaveBeenCalled();
  });

  it('flag OFF → zero change: modify message goes to normal chat', async () => {
    const user = userEvent.setup();
    flagState.canvasV2 = false;
    renderPage();
    // With the flag off, seed-canvas takes the legacy draft path (no v2 id).
    await user.click(screen.getByText('send-modify'));

    await waitFor(() => expect(mockSendMessage).toHaveBeenCalledTimes(1));
    expect(mockEditCanvas).not.toHaveBeenCalled();
  });

  it('refills the composer when the silent edit fails (fail-open)', async () => {
    const user = userEvent.setup();
    // 409 → store.requestEdit returns null after recording the error.
    mockEditCanvas.mockRejectedValueOnce({ status: 409, message: '画布已被更新' });
    renderPage();
    await seedActiveCanvas(user);

    await user.click(screen.getByText('send-modify'));

    await waitFor(() =>
      expect(screen.getByTestId('prefill')).toHaveTextContent('把这段改简洁'),
    );
    expect(mockSendMessage).not.toHaveBeenCalled();
  });
});
