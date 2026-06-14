export interface TranscriptScrollMetrics {
  scrollTop: number;
  scrollHeight: number;
  clientHeight: number;
  previousScrollTop: number;
  wasSticking: boolean;
  wasDetached: boolean;
  bottomThreshold?: number;
}

export interface TranscriptScrollState {
  shouldStickToBottom: boolean;
  isAwayFromBottom: boolean;
  nextPreviousScrollTop: number;
}

export const TRANSCRIPT_BOTTOM_THRESHOLD = 96;

export function resolveTranscriptScrollState({
  scrollTop,
  scrollHeight,
  clientHeight,
  previousScrollTop,
  wasSticking,
  wasDetached,
  bottomThreshold = TRANSCRIPT_BOTTOM_THRESHOLD,
}: TranscriptScrollMetrics): TranscriptScrollState {
  const distanceFromBottom = Math.max(0, scrollHeight - scrollTop - clientHeight);
  const isNearBottom = distanceFromBottom < bottomThreshold;
  if (isNearBottom) {
    return {
      shouldStickToBottom: true,
      isAwayFromBottom: false,
      nextPreviousScrollTop: scrollTop,
    };
  }

  const movedUp = scrollTop < previousScrollTop - 1;
  if (wasDetached || movedUp) {
    return {
      shouldStickToBottom: false,
      isAwayFromBottom: true,
      nextPreviousScrollTop: scrollTop,
    };
  }

  return {
    shouldStickToBottom: wasSticking,
    isAwayFromBottom: !wasSticking,
    nextPreviousScrollTop: scrollTop,
  };
}

export function isUserScrollingAwayFromBottom(deltaY: number): boolean {
  return deltaY < 0;
}
