import type { Message } from '../api/types';

export function getCanvasVisibleMessages(
  messages: Message[],
  hiddenMessageIds: string[] | undefined,
  hasInlineCanvas: boolean,
  inlineCanvasAnchorMessageId?: string | null,
): Message[] {
  if (!hiddenMessageIds || hiddenMessageIds.length === 0) return messages;
  const hidden = new Set(hiddenMessageIds);
  return messages.filter(
    (message) =>
      !hidden.has(message.id) ||
      (hasInlineCanvas && inlineCanvasAnchorMessageId === message.id),
  );
}

export function hasVisibleCanvasAnchor(
  messages: Message[],
  hasInlineCanvas: boolean,
  inlineCanvasAnchorMessageId?: string | null,
): boolean {
  return Boolean(
    hasInlineCanvas &&
      inlineCanvasAnchorMessageId &&
      messages.some((message) => message.id === inlineCanvasAnchorMessageId),
  );
}
