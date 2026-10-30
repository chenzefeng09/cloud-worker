/**
 * SessionReplayPage — a member's session is replayed through the tenant_admin
 * proxies (api.admin.sessions.runs / .messages), which the BFF audits. The
 * owner-scoped api.runs / api.messages would 404 on a peer's session.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

const admin = vi.hoisted(() => ({ list: vi.fn(), runs: vi.fn(), messages: vi.fn() }));
const ownerScoped = vi.hoisted(() => ({ runs: vi.fn(), messages: vi.fn() }));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      admin: { sessions: admin },
      runs: { list: ownerScoped.runs },
      messages: { list: ownerScoped.messages },
    },
  };
});

vi.mock('../../hooks/useFeatureFlags', () => ({ useFeatureFlag: () => false }));

import { SessionReplayPage } from './SessionReplayPage';

const NOW = new Date().toISOString();

afterEach(() => {
  vi.clearAllMocks();
});

describe('SessionReplayPage', () => {
  it('loads runs and the transcript through the audited admin endpoints', async () => {
    admin.list.mockResolvedValue({
      items: [{ id: 'ses_peer1', title: 'Quarterly numbers', user_id: 'usr_peer', created_at: NOW }],
      next_cursor: null,
    });
    admin.runs.mockResolvedValue({
      items: [{ id: 'run_peer1', session_id: 'ses_peer1', status: 'COMPLETED', created_at: NOW }],
    });
    admin.messages.mockResolvedValue({
      items: [
        {
          id: 'msg_1', session_id: 'ses_peer1', role: 'USER', content: 'Summarise Q3',
          run_id: null, sequence: 1, created_at: NOW,
        },
      ],
      next_cursor: null,
    });
    const user = userEvent.setup();
    render(<SessionReplayPage />);

    await user.click(await screen.findByText('Quarterly numbers'));
    expect(admin.runs).toHaveBeenCalledWith('ses_peer1');

    await user.click(await screen.findByText('run_peer'));
    expect(admin.messages).toHaveBeenCalledWith('ses_peer1');
    expect(await screen.findByText('Summarise Q3')).toBeInTheDocument();

    expect(ownerScoped.runs).not.toHaveBeenCalled();
    expect(ownerScoped.messages).not.toHaveBeenCalled();
  });
});
