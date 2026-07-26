import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Message } from '../api/types';
import { MessageBubble } from './MessageBubble';

vi.mock('../hooks/useFeatureFlags', () => ({
  useFeatureFlag: () => false,
}));

function userMessage(content: string): Message {
  return {
    id: 'msg_long_user_content',
    session_id: 'ses_test',
    role: 'USER',
    content,
    tool_calls: null,
    run_id: null,
    sequence: 1,
    created_at: '2026-07-26T00:00:00Z',
  };
}

describe('MessageBubble long-content containment', () => {
  it('allows an unbroken user token to wrap inside the message column', () => {
    const token = 'A'.repeat(2048);
    const { container } = render(<MessageBubble message={userMessage(token)} />);

    expect(container.firstElementChild).toHaveClass('w-full', 'min-w-0');

    const bubble = screen.getByText(token);
    expect(bubble).toHaveClass('min-w-0', 'whitespace-pre-wrap', 'break-words');
    expect(bubble).toHaveStyle({ overflowWrap: 'anywhere' });
  });
});
