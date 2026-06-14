import { describe, expect, it } from 'vitest';
import type { Message } from '../api/types';
import { findCanvasCreationReply } from './canvasCreation';

function message(partial: Partial<Message>): Message {
  return {
    id: partial.id ?? 'msg',
    session_id: 'ses_test',
    role: partial.role ?? 'ASSISTANT',
    content: partial.content ?? '',
    tool_calls: partial.tool_calls ?? null,
    run_id: partial.run_id ?? 'run_test',
    sequence: partial.sequence ?? 0,
    created_at: partial.created_at ?? '2026-06-14T00:00:00.000Z',
    attachments: partial.attachments,
    streaming: partial.streaming,
    blockKind: partial.blockKind,
    streamRunId: partial.streamRunId,
  };
}

describe('findCanvasCreationReply', () => {
  it('uses streaming assistant text while the document is still being written', () => {
    const result = findCanvasCreationReply(
      [
        message({ id: 'user_1', role: 'USER', content: '写一篇文章' }),
        message({ id: 'think_1', content: 'planning', blockKind: 'thinking' }),
        message({ id: 'stream_1', content: '# 标题\n\n正文正在生成', streaming: true, blockKind: 'text' }),
      ],
      1,
    );

    expect(result?.id).toBe('stream_1');
    expect(result?.content).toContain('正文正在生成');
  });

  it('skips tool and thinking blocks', () => {
    const result = findCanvasCreationReply(
      [
        message({ id: 'tool_1', role: 'TOOL', content: '', blockKind: 'tool' }),
        message({ id: 'think_1', content: '思考', blockKind: 'thinking' }),
      ],
      0,
    );

    expect(result).toBeNull();
  });

  it('can require a completed assistant message for finalization', () => {
    const messages = [
      message({ id: 'stream_1', content: '临时文本', streaming: true, blockKind: 'text' }),
      message({ id: 'final_1', content: '最终正文', streaming: false }),
    ];

    expect(findCanvasCreationReply(messages, 0, { completeOnly: true })?.id).toBe('final_1');
  });

  it('returns null when only streaming text exists but completeOnly is required', () => {
    const result = findCanvasCreationReply(
      [message({ id: 'stream_1', content: '临时文本', streaming: true, blockKind: 'text' })],
      0,
      { completeOnly: true },
    );

    expect(result).toBeNull();
  });
});
