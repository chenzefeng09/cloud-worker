import type { Message } from '../api/types';

export function findCanvasCreationReply(
  messages: Message[],
  fromMessageIndex: number,
  options: { completeOnly?: boolean } = {},
): Message | null {
  let assistantReply: Message | null = null;
  for (const message of messages.slice(fromMessageIndex)) {
    const role = (message.role || '').toUpperCase();
    if (
      role === 'ASSISTANT' &&
      message.blockKind !== 'tool' &&
      message.blockKind !== 'thinking' &&
      message.content.trim() &&
      (!options.completeOnly || !message.streaming)
    ) {
      assistantReply = message;
    }
  }
  return assistantReply;
}
