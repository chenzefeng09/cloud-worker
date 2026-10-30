/**
 * AgentPage header — the title of a session opened from outside the sidebar's
 * loaded page, and a status that never contradicts the task's own lifecycle.
 */
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Session } from '../api/types';
import { useChatStore } from '../stores/chatStore';
import { AgentPage } from './AgentPage';

const mocks = vi.hoisted(() => ({ getSession: vi.fn() }));

vi.mock('../api/client', () => ({
  api: { sessions: { get: mocks.getSession, list: vi.fn() } },
}));
vi.mock('../hooks/useChat', () => ({ useChat: () => ({ sendMessage: vi.fn() }) }));
vi.mock('../components/ChatWindow', () => ({ ChatWindow: () => null }));
vi.mock('../components/tasks/TaskBar', () => ({ TaskBar: () => null }));
vi.mock('../components/FilePreviewPanel', () => ({ FilePreviewPanel: () => null }));

function session(overrides: Partial<Session> = {}): Session {
  return {
    id: 'ses_1',
    tenant_id: 'tnt_1',
    user_id: 'usr_1',
    title: 'Quarterly report',
    status: 'ACTIVE',
    current_run_id: null,
    metadata: null,
    workspace_id: null,
    created_at: '2026-10-01T00:00:00Z',
    updated_at: '2026-10-01T00:00:00Z',
    ...overrides,
  };
}

function renderAt(id: string) {
  return render(
    <MemoryRouter initialEntries={[`/tasks/${id}`]}>
      <Routes>
        <Route path="/tasks/:id" element={<AgentPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  useChatStore.setState({ sessions: [], activeSessionId: null, streamingSessions: {} });
});

describe('AgentPage header', () => {
  it('fetches a session the sidebar has not loaded and shows its title', async () => {
    mocks.getSession.mockResolvedValue(session({ metadata: { kind: 'task' } }));

    renderAt('ses_1');

    expect(await screen.findByText('Quarterly report')).toBeInTheDocument();
    expect(mocks.getSession).toHaveBeenCalledWith('ses_1');
    expect(useChatStore.getState().sessions.map((s) => s.id)).toEqual(['ses_1']);
  });

  it('does not refetch a session already in the list', async () => {
    useChatStore.setState({ sessions: [session()] });

    renderAt('ses_1');

    expect(await screen.findByText('Quarterly report')).toBeInTheDocument();
    expect(mocks.getSession).not.toHaveBeenCalled();
  });

  it('leaves a task’s status to its task bar', async () => {
    useChatStore.setState({
      sessions: [session({ status: 'COMPLETED', metadata: { kind: 'task' } })],
    });

    renderAt('ses_1');

    await screen.findByText('Quarterly report');
    expect(screen.queryByText(/Completed/)).not.toBeInTheDocument();
  });

  it('does not call an open chat “in progress” when nothing is running', async () => {
    useChatStore.setState({ sessions: [session({ status: 'ACTIVE' })] });

    renderAt('ses_1');

    await screen.findByText('Quarterly report');
    expect(screen.queryByText(/In progress/)).not.toBeInTheDocument();
  });

  it('shows a chat’s status once it is no longer open', async () => {
    useChatStore.setState({ sessions: [session({ status: 'ARCHIVED' })] });

    renderAt('ses_1');

    await waitFor(() => expect(screen.getByText(/Archived/)).toBeInTheDocument());
  });
});
