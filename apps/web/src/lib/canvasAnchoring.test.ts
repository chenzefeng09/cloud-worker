import { describe, expect, it } from 'vitest';
import type { Message } from '../api/types';
import { getCanvasVisibleMessages, hasVisibleCanvasAnchor } from './canvasAnchoring';

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
  it('keeps a hidden message visible when it anchors the inline canvas', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2'), message('assistant_3')];

    const visible = getCanvasVisibleMessages(messages, ['assistant_2', 'assistant_3'], true, 'assistant_2');

    expect(visible.map((item) => item.id)).toEqual(['user_1', 'assistant_2']);
    expect(hasVisibleCanvasAnchor(visible, true, 'assistant_2')).toBe(true);
  });

  it('removes hidden messages when no inline canvas is anchored there', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2'), message('assistant_3')];

    const visible = getCanvasVisibleMessages(messages, ['assistant_2'], false, 'assistant_2');

    expect(visible.map((item) => item.id)).toEqual(['user_1', 'assistant_3']);
    expect(hasVisibleCanvasAnchor(visible, false, 'assistant_2')).toBe(false);
  });

  it('reports no visible anchor when the anchor message has not arrived yet', () => {
    const messages = [message('user_1', 'USER')];

    expect(hasVisibleCanvasAnchor(messages, true, 'assistant_2')).toBe(false);
  });
});
