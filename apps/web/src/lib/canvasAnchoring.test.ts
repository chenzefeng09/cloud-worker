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

  it('removes non-anchor hidden messages while the canvas holds content', () => {
    const messages = [
      message('user_1', 'USER'),
      message('assistant_2'),
      message('assistant_3'),
      message('assistant_4'),
    ];

    // Canvas holds content (canHideBehindCanvas === true) and is anchored on
    // assistant_2: assistant_2 stays visible (it renders the card), the other
    // hidden message (assistant_4) is removed.
    const visible = getCanvasVisibleMessages(
      messages,
      ['assistant_2', 'assistant_4'],
      true,
      'assistant_2',
    );

    expect(visible.map((item) => item.id)).toEqual(['user_1', 'assistant_2', 'assistant_3']);
    expect(hasVisibleCanvasAnchor(visible, true, 'assistant_2')).toBe(true);
  });

  it('reports no visible anchor when the anchor message has not arrived yet', () => {
    const messages = [message('user_1', 'USER')];

    expect(hasVisibleCanvasAnchor(messages, true, 'assistant_2')).toBe(false);
  });

  // Structural backstop: hiding a message must always be paired with a canvas
  // that actually renders its content. The home → task race can leave
  // `hiddenMessageIds` populated while the canvas is torn down / its draft is
  // empty; hiding then would black-hole the body (the reported bug). When the
  // canvas can't hold the content, every message must remain visible.
  it('keeps hidden messages visible when the canvas cannot hold their content', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2'), message('assistant_3')];

    // canHideBehindCanvas === false — e.g. draft cleared, canvas closed, or a
    // cross-page mount race — even though an anchor id is still set.
    const visible = getCanvasVisibleMessages(messages, ['assistant_2'], false, 'assistant_2');

    expect(visible.map((item) => item.id)).toEqual(['user_1', 'assistant_2', 'assistant_3']);
  });

  it('does not black-hole a hidden message when no anchor id is set', () => {
    const messages = [message('user_1', 'USER'), message('assistant_2')];

    // Hidden set carries an id but the canvas is not holding content: the body
    // must survive as a plain bubble rather than disappearing entirely.
    const visible = getCanvasVisibleMessages(messages, ['assistant_2'], false, null);

    expect(visible.map((item) => item.id)).toEqual(['user_1', 'assistant_2']);
  });
});
