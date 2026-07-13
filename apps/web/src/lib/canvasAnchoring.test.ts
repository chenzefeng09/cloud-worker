import { describe, expect, it } from 'vitest';
import type { Message } from '../api/types';
import { hasVisibleCanvasAnchor } from './canvasAnchoring';

function message(id: string, role: Message['role'] = 'ASSISTANT'): Message {
  return {
    id,
    session_id: 'ses_test',
    role,
    content: `${id} content`,
    tool_calls: null,
    run_id: null,
    sequence: Number(id.replace(/\D/g, '')) || 0,
    created_at: '2026-06-14T00:00:00.000Z',
  };
}

describe('canvas anchoring', () => {
  it('reports a visible anchor when the anchor message is present and the canvas can render', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2'), message('assistant_3')];

    expect(hasVisibleCanvasAnchor(messages, true, 'assistant_2')).toBe(true);
  });

  it('reports no visible anchor when the anchor message has not arrived yet', () => {
    const messages = [message('user_1', 'USER')];

    expect(hasVisibleCanvasAnchor(messages, true, 'assistant_2')).toBe(false);
  });

  it('reports no visible anchor when the canvas cannot render', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2')];

    expect(hasVisibleCanvasAnchor(messages, false, 'assistant_2')).toBe(false);
  });

  it('reports no visible anchor when no anchor id is set', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2')];

    expect(hasVisibleCanvasAnchor(messages, true, null)).toBe(false);
  });
});
