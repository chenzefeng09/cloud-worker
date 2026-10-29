/** What an erasure removed, and which steps it skipped (admin requests skip model preferences). */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ErasureResult } from '../../api/privacy';
import { ErasureSummary, erasureGroups, skippedSteps } from './ErasureSummary';

const ADMIN_RESULT: ErasureResult = {
  schedules: { deleted: 3 },
  platform: { deleted: { sessions: 2 }, runs_cancelled: 0 },
  memories_deleted: 1,
  llm_preferences: { skipped: 'not_supported' },
  bff: { api_keys: 1 },
  account: 'pending_admin_closure',
};

describe('erasureGroups', () => {
  it('counts triggers with the content and model preferences with the account settings', () => {
    const groups = erasureGroups({ ...ADMIN_RESULT, llm_preferences: { deleted: 2 } });
    const rows = Object.fromEntries(groups.map((g) => [g.title, g.rows.map((r) => [r.label, r.count])]));
    expect(rows['Tasks and content']).toEqual([['Tasks and conversations', 2], ['Running tasks stopped', 0], ['Triggers', 3]]);
    expect(rows['Account settings']).toEqual([['API keys', 1], ['Model preferences', 2]]);
  });

  it('never counts a skipped step', () => {
    const rows = erasureGroups(ADMIN_RESULT).flatMap((g) => g.rows.map((r) => r.key));
    expect(rows).not.toContain('llm_preferences');
  });
});

describe('skippedSteps', () => {
  it('reads both the skipped map and skipped step results, once per step', () => {
    expect(
      skippedSteps(
        { skipped: { schedules: 'not_configured' } },
        { schedules: { skipped: 'not_configured' }, llm_preferences: { skipped: 'not_supported' } },
      ),
    ).toEqual([
      { step: 'schedules', reason: 'not_configured' },
      { step: 'llm_preferences', reason: 'not_supported' },
    ]);
  });

  it('ignores shapes that are not skips', () => {
    expect(skippedSteps(null, 'x', [], { platform: { deleted: {} }, skipped: ['schedules'] })).toEqual([]);
  });
});

describe('ErasureSummary', () => {
  it('says plainly when a step was skipped rather than done', () => {
    render(<ErasureSummary result={ADMIN_RESULT} />);
    expect(screen.getByText('Skipped:')).toBeInTheDocument();
    expect(
      screen.getByText('Model preferences: not deleted — only the user can clear these, by deleting their own data'),
    ).toBeInTheDocument();
    expect(screen.getByText('Triggers').nextElementSibling).toHaveTextContent('3');
  });
});
