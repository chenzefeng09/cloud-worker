import { describe, expect, it } from 'vitest';
import {
  isUserScrollingAwayFromBottom,
  resolveTranscriptScrollState,
} from './chatScroll';

describe('resolveTranscriptScrollState', () => {
  it('keeps auto-stick enabled when content grows while the user is still following the bottom', () => {
    const result = resolveTranscriptScrollState({
      scrollTop: 900,
      scrollHeight: 1500,
      clientHeight: 500,
      previousScrollTop: 900,
      wasSticking: true,
      wasDetached: false,
    });

    expect(result.shouldStickToBottom).toBe(true);
    expect(result.isAwayFromBottom).toBe(false);
  });

  it('detaches from auto-scroll when the user has moved upward', () => {
    const result = resolveTranscriptScrollState({
      scrollTop: 640,
      scrollHeight: 1500,
      clientHeight: 500,
      previousScrollTop: 720,
      wasSticking: true,
      wasDetached: false,
    });

    expect(result.shouldStickToBottom).toBe(false);
    expect(result.isAwayFromBottom).toBe(true);
  });

  it('stays detached while the user is reading above the bottom', () => {
    const result = resolveTranscriptScrollState({
      scrollTop: 760,
      scrollHeight: 1500,
      clientHeight: 500,
      previousScrollTop: 640,
      wasSticking: false,
      wasDetached: true,
    });

    expect(result.shouldStickToBottom).toBe(false);
    expect(result.isAwayFromBottom).toBe(true);
  });

  it('reattaches when the user returns near the bottom', () => {
    const result = resolveTranscriptScrollState({
      scrollTop: 930,
      scrollHeight: 1500,
      clientHeight: 500,
      previousScrollTop: 760,
      wasSticking: false,
      wasDetached: true,
    });

    expect(result.shouldStickToBottom).toBe(true);
    expect(result.isAwayFromBottom).toBe(false);
  });
});

describe('isUserScrollingAwayFromBottom', () => {
  it('treats upward wheel movement as an explicit detach signal', () => {
    expect(isUserScrollingAwayFromBottom(-1)).toBe(true);
    expect(isUserScrollingAwayFromBottom(1)).toBe(false);
  });
});
