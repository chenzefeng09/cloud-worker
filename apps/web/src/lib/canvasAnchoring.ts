import type { Message } from '../api/types';

export function getCanvasVisibleMessages(
  messages: Message[],
  hiddenMessageIds: string[] | undefined,
  canHideBehindCanvas: boolean,
  inlineCanvasAnchorMessageId?: string | null,
): Message[] {
  if (!hiddenMessageIds || hiddenMessageIds.length === 0) return messages;
  // Structural backstop: a message is only ever hidden because its content has
  // been lifted into an inline canvas card. If the canvas is not actually
  // rendering that content right now — draft cleared, canvas closed, empty
  // draft, or a cross-page mount race that left `hiddenMessageIds` populated
  // while the canvas was torn down — hiding the message would create a black
  // hole: the body vanishes from the transcript with nothing rendering it
  // anywhere. In that case keep every message visible. Better to show the body
  // as a plain bubble (or even twice) than to lose it entirely.
  if (!canHideBehindCanvas) return messages;
  const hidden = new Set(hiddenMessageIds);
  return messages.filter(
    (message) =>
      !hidden.has(message.id) || inlineCanvasAnchorMessageId === message.id,
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
