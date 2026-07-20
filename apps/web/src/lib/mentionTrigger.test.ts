import { describe, expect, it } from 'vitest';
import {
  detectMentionTrigger,
  filterMentionCandidates,
  activeWorkspaceFileIds,
  type MentionCandidate,
  type MentionReference,
} from './mentionTrigger';

describe('detectMentionTrigger', () => {
  it('triggers on an "@" at the start of the text', () => {
    expect(detectMentionTrigger('@', 1)).toEqual({ at: 0, query: '' });
    expect(detectMentionTrigger('@rep', 4)).toEqual({ at: 0, query: 'rep' });
  });

  it('triggers on an "@" preceded by a space', () => {
    expect(detectMentionTrigger('see @doc', 8)).toEqual({ at: 4, query: 'doc' });
  });

  it('triggers on an "@" preceded by a tab', () => {
    expect(detectMentionTrigger('\t@x', 3)).toEqual({ at: 1, query: 'x' });
  });

  it('uses only the text between "@" and the cursor as the query', () => {
    // cursor sits after "re", not at end of "report"
    expect(detectMentionTrigger('@report', 3)).toEqual({ at: 0, query: 're' });
  });

  it('does NOT trigger for an email-like "@" glued to a word', () => {
    expect(detectMentionTrigger('foo@bar.com', 11)).toBeNull();
  });

  it('does NOT trigger when a newline sits between "@" and the cursor', () => {
    expect(detectMentionTrigger('@foo\nbar', 8)).toBeNull();
  });

  it('picks the nearest "@" before the cursor', () => {
    // "@a @b|" -> nearest is the second "@"
    expect(detectMentionTrigger('@a @b', 5)).toEqual({ at: 3, query: 'b' });
  });

  it('returns null when there is no "@" before the cursor', () => {
    expect(detectMentionTrigger('hello', 5)).toBeNull();
  });

  it('does not trigger past a newline back to an earlier "@"', () => {
    // "@first\nsecond|": the "@" is on the previous line -> no trigger
    expect(detectMentionTrigger('@first\nsecond', 13)).toBeNull();
  });
});

describe('filterMentionCandidates', () => {
  const cands: MentionCandidate[] = [
    { id: 'wsf_1', filename: 'Report.pdf', kind: 'workspace' },
    { id: 'att_1', filename: 'notes.txt', kind: 'attachment' },
    { id: 'wsf_2', filename: 'data report.csv', kind: 'workspace' },
  ];

  it('returns all candidates for an empty query', () => {
    expect(filterMentionCandidates(cands, '')).toHaveLength(3);
  });

  it('filters case-insensitively by substring', () => {
    const out = filterMentionCandidates(cands, 'report');
    expect(out.map((c) => c.id)).toEqual(['wsf_1', 'wsf_2']);
  });
});

describe('activeWorkspaceFileIds', () => {
  const refs: MentionReference[] = [
    { id: 'wsf_1', filename: 'Report.pdf', kind: 'workspace' },
    { id: 'att_1', filename: 'notes.txt', kind: 'attachment' },
    { id: 'wsf_2', filename: 'gone.md', kind: 'workspace' },
  ];

  it('returns only workspace ids whose "@filename" survives in the text', () => {
    const text = 'please read @Report.pdf and @notes.txt';
    expect(activeWorkspaceFileIds(refs, text)).toEqual(['wsf_1']);
  });

  it('excludes references the user deleted from the text', () => {
    expect(activeWorkspaceFileIds(refs, 'nothing here')).toEqual([]);
  });

  it('dedupes repeated ids', () => {
    const dupe: MentionReference[] = [
      { id: 'wsf_1', filename: 'Report.pdf', kind: 'workspace' },
      { id: 'wsf_1', filename: 'Report.pdf', kind: 'workspace' },
    ];
    expect(activeWorkspaceFileIds(dupe, '@Report.pdf @Report.pdf')).toEqual(['wsf_1']);
  });
});
