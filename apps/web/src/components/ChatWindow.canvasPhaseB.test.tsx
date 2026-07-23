/**
 * ChatWindow — canvas.v2 Phase B message-anchored canvas card.
 *
 * When the canvas.v2 flag is ON and an assistant message row carries a
 * `canvas_id` (set live by the `canvas_created` SSE handler, or read straight
 * off the durable DB row on hydration), ChatWindow renders a CanvasMessageCard
 * anchored under that message's bubble. The card is ADDITIVE: the assistant
 * bubble always renders (its content is never hidden behind the card), and the
 * same canvas is never drawn twice alongside the Phase A inlineCanvasV2Id path.
 *
 * Heavy children (MessageBubble/InputBar/OutputFileCards/CanvasMessageCard) are
 * stubbed to thin harnesses so the test isolates the render-branch decision.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { Message } from '../api/types';

// ── feature flag mock — toggled per test via a mutable ref ───────────────────
const flagState = { canvasV2: true, thinkingUi: false };
vi.mock('../hooks/useFeatureFlags', () => ({
  useFeatureFlag: (key: string) =>
    key === 'canvas.v2'
      ? flagState.canvasV2
      : key === 'ai.thinking_ui'
        ? flagState.thinkingUi
        : false,
}));

// ── useChat mock — feed a fixed transcript, no SSE/composer surface ──────────
const chatState: { messages: Message[]; isStreaming: boolean } = {
  messages: [],
  isStreaming: false,
};
vi.mock('../hooks/useChat', () => ({
  useChat: () => ({
    messages: chatState.messages,
    isStreaming: chatState.isStreaming,
    sendMessage: vi.fn(),
    cancel: vi.fn(),
    prefillText: '',
    clearPrefill: vi.fn(),
    rewindTo: vi.fn(),
    pendingRewind: null,
    confirmRewind: vi.fn(),
    cancelRewind: vi.fn(),
    rewindBusy: false,
    rewindError: '',
  }),
}));

vi.mock('../hooks/useOutputAttachments', () => ({
  useOutputAttachments: () => ({ byRunId: new Map(), refresh: vi.fn() }),
}));

vi.mock('../hooks/useInstalledSkills', () => ({
  useInstalledSkills: () => Object.assign([], { refresh: vi.fn() }),
}));

vi.mock('../hooks/useMcpServers', () => ({
  useMcpServers: () => ({ servers: [], refresh: vi.fn() }),
}));

vi.mock('../hooks/useDefaultModel', () => ({
  useDefaultModel: () => ({ modelId: 'm1', loading: false, refetch: vi.fn() }),
}));

// ── Thin child stubs ─────────────────────────────────────────────────────────
vi.mock('./MessageBubble', () => ({
  MessageBubble: ({ message }: { message: Message }) => (
    <div data-testid="bubble">{message.content}</div>
  ),
  WorkingOnItPill: () => null,
}));

vi.mock('./CanvasMessageCard', () => ({
  CanvasMessageCard: ({ canvasId }: { canvasId?: string }) => (
    <div data-testid="canvas-card" data-canvas-id={canvasId} />
  ),
}));

vi.mock('./OutputFileCards', () => ({ OutputFileCards: () => null }));
vi.mock('./composer/Composer', () => ({ Composer: () => <div data-testid="input-bar" /> }));
vi.mock('./PlanBlock', () => ({ PlanBlock: () => null }));
vi.mock('./RewindConfirmModal', () => ({ RewindConfirmModal: () => null }));
vi.mock('./DataNoticeModal', () => ({ DataNoticeModal: () => null }));
vi.mock('./hitl/ConfirmCard', () => ({ ConfirmCard: () => null }));
vi.mock('./hitl/QuestionCard', () => ({ QuestionCard: () => null }));
vi.mock('./StepGroup', () => ({ StepGroup: () => null }));

import { ChatWindow } from './ChatWindow';
import { useChatStore } from '../stores/chatStore';
import { useCanvasStore } from '../stores/canvasStore';

function assistantMsg(over: Partial<Message>): Message {
  return {
    id: 'msg_a',
    session_id: 'ses_1',
    role: 'ASSISTANT',
    content: '给你写了一份初稿，见下方画布。',
    tool_calls: null,
    run_id: 'run_1',
    sequence: 1,
    created_at: '2026-01-01',
    ...over,
  };
}

beforeEach(() => {
  flagState.canvasV2 = true;
  flagState.thinkingUi = false;
  chatState.isStreaming = false;
  chatState.messages = [];
  // Reset stores; localStorage-backed data notice never shows in jsdom default.
  useChatStore.setState({ activeSessionId: 'ses_1' });
  useCanvasStore.getState().reset();
  // Fire-and-forget fetchCanvas warm — stub so the hydration warm effect never
  // hits the network. setState (not vi.spyOn) sidesteps the spyOn typed overload
  // constraint that rejects a store-action key.
  useCanvasStore.setState({ fetchCanvas: vi.fn().mockResolvedValue(null) });
});

describe('ChatWindow — canvas.v2 Phase B anchored card', () => {
  it('renders a canvas card under an assistant bubble that carries canvas_id (live + hydration source)', () => {
    chatState.messages = [assistantMsg({ canvas_id: 'cvs_1' })];
    render(<ChatWindow sessionId="ses_1" />);

    // Bubble content stays visible (card is additive, never hides the body).
    expect(screen.getByTestId('bubble')).toHaveTextContent('给你写了一份初稿，见下方画布。');
    const card = screen.getByTestId('canvas-card');
    expect(card).toHaveAttribute('data-canvas-id', 'cvs_1');
  });

  it('renders NO card when the message has no canvas_id', () => {
    chatState.messages = [assistantMsg({ canvas_id: null })];
    render(<ChatWindow sessionId="ses_1" />);

    expect(screen.getByTestId('bubble')).toBeInTheDocument();
    expect(screen.queryByTestId('canvas-card')).not.toBeInTheDocument();
  });

  it('renders NO card when the canvas.v2 flag is OFF (even with a canvas_id)', () => {
    flagState.canvasV2 = false;
    chatState.messages = [assistantMsg({ canvas_id: 'cvs_1' })];
    render(<ChatWindow sessionId="ses_1" />);

    // Body still visible; the Phase B card is gated off.
    expect(screen.getByTestId('bubble')).toBeInTheDocument();
    expect(screen.queryByTestId('canvas-card')).not.toBeInTheDocument();
  });

  it('does NOT double-render when the canvas is already shown by the Phase A inlineCanvasV2Id path', () => {
    // Seed the store so the Phase A trailing/anchor render is active for cvs_dup.
    useCanvasStore.getState().setCanvas({
      id: 'cvs_dup',
      session_id: 'ses_1',
      kind: 'article',
      title: 't',
      current_version: 1,
      source_message_id: null,
      source_run_id: null,
      blocks: [{ id: 'b1', type: 'paragraph', text: '正文' }],
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    });
    chatState.messages = [assistantMsg({ canvas_id: 'cvs_dup' })];

    render(<ChatWindow sessionId="ses_1" inlineCanvasV2Id="cvs_dup" />);

    // Exactly one card for cvs_dup — the Phase B branch skips it because the
    // Phase A path (inlineCanvasV2Id) already renders this same canvas.
    const cards = screen.getAllByTestId('canvas-card');
    const dupCards = cards.filter((c) => c.getAttribute('data-canvas-id') === 'cvs_dup');
    expect(dupCards).toHaveLength(1);
  });

  it('renders a distinct Phase B card when its canvas differs from the Phase A one', () => {
    useCanvasStore.getState().setCanvas({
      id: 'cvs_phaseA',
      session_id: 'ses_1',
      kind: 'article',
      title: 't',
      current_version: 1,
      source_message_id: null,
      source_run_id: null,
      blocks: [{ id: 'b1', type: 'paragraph', text: '正文' }],
      created_at: '2026-01-01',
      updated_at: '2026-01-01',
    });
    chatState.messages = [assistantMsg({ canvas_id: 'cvs_phaseB' })];

    render(<ChatWindow sessionId="ses_1" inlineCanvasV2Id="cvs_phaseA" />);

    const ids = screen
      .getAllByTestId('canvas-card')
      .map((c) => c.getAttribute('data-canvas-id'));
    expect(ids).toContain('cvs_phaseB');
  });
});
