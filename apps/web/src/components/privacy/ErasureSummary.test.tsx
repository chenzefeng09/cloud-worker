/** What an erasure removed, and which steps it skipped. */
import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import type { ErasureResult } from '../../api/privacy';
import { ErasureSummary, erasureGroups, skippedSteps } from './ErasureSummary';

const ADMIN_RESULT: ErasureResult = {
  schedules: { skipped: 'not_configured' },
  platform: { deleted: { sessions: 2 }, runs_cancelled: 0 },
  memories_deleted: 1,
  llm_preferences: { deleted: 2 },
  apps: { apps_deleted: 1, apps_deattributed: 2, versions_deattributed: 3 },
  bff: { api_keys: 1 },
  account: 'pending_admin_closure',
};

describe('erasureGroups', () => {
  it('counts triggers with the content, apps on their own and model preferences with the account settings', () => {
    const groups = erasureGroups({ ...ADMIN_RESULT, schedules: { deleted: 3 } });
    const rows = Object.fromEntries(groups.map((g) => [g.title, g.rows.map((r) => [r.label, r.count])]));
    expect(rows['Tasks and content']).toEqual([['Tasks and conversations', 2], ['Running tasks stopped', 0], ['Triggers', 3]]);
    expect(rows.Apps).toEqual([
      ['Apps deleted', 1],
      ['Shared apps kept, author removed', 2],
      ['App versions no longer naming you', 3],
    ]);
    expect(rows['Account settings']).toEqual([['API keys', 1], ['Model preferences', 2]]);
  });

  it('counts the sign-ins the erasure ended', () => {
    const groups = erasureGroups({ ...ADMIN_RESULT, bff: { api_keys: 1, web_sessions: 2 } });
    const account = groups.find((g) => g.title === 'Account settings');
    expect(account?.rows.map((r) => [r.label, r.count])).toContainEqual(['Signed-in sessions ended', 2]);
  });

  it('never counts a skipped step', () => {
    const rows = erasureGroups(ADMIN_RESULT).flatMap((g) => g.rows.map((r) => r.key));
    expect(rows).not.toContain('schedules');
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
    expect(screen.getByText('Triggers: not part of this deployment')).toBeInTheDocument();
    expect(screen.getByText('Model preferences').nextElementSibling).toHaveTextContent('2');
  });
});
