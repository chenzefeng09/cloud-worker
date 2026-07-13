import type { Message } from '../api/types';

// NOTE: message hiding has been retired (design §7.1). The "put in canvas"
// action no longer removes the source bubble — the card and the bubble
// coexist, so content is always reachable in the transcript. This module now
// only answers "is the inline canvas anchored to a message that's on screen",
// which decides whether the card renders at its anchor or at the bottom
// fallback slot.

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
