/**
 * WorkspaceLayout — the privacy notice gate belongs to the signed-in shell, so
 * it shows on whichever page the user opens first (not only in chat, after
 * their first prompt has already been sent).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useChatStore } from '../stores/chatStore';
import { memoryStorage, mockFetch } from './privacy/testUtils';

vi.mock('../lib/workspaceContext', () => ({
  WorkspaceProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock('../hooks/useSessions', () => {
  const fetchSessions = () => {};
  return { useSessions: () => ({ fetchSessions }) };
});
vi.mock('./shell/Sidebar', () => ({ Sidebar: () => null }));
vi.mock('../admin/AdminLayout', () => ({ AdminLayout: () => null, canAccessAdmin: () => false }));
vi.mock('../pages/HomePage', () => ({ HomePage: () => <h1>Home</h1> }));
vi.mock('../pages/AgentPage', () => ({ AgentPage: () => <h1>Task</h1> }));
vi.mock('../pages/TasksPage', () => ({ TasksPage: () => <h1>Tasks</h1> }));
vi.mock('../pages/AppRunPage', () => ({ AppRunPage: () => <h1>Run app</h1> }));
vi.mock('../pages/AppResultPage', () => ({ AppResultPage: () => null }));
vi.mock('../pages/AppEditorPage', () => ({ AppEditorPage: () => null }));
vi.mock('../pages/AppListPage', () => ({ AppListPage: () => null }));
vi.mock('../pages/AppDetailPage', () => ({ AppDetailPage: () => null }));
vi.mock('../pages/AppHistoryPage', () => ({ AppHistoryPage: () => null }));
vi.mock('../pages/FilesPage', () => ({ FilesPage: () => null }));
vi.mock('../pages/LibraryPage', () => ({ LibraryPage: () => null }));
vi.mock('../pages/WorkspacePage', () => ({ WorkspacePage: () => null }));
vi.mock('../pages/ArtifactsPage', () => ({ ArtifactsPage: () => null }));
vi.mock('../pages/ProfilePage', () => ({ ProfilePage: () => null }));

import { WorkspaceLayout } from './WorkspaceLayout';

const BASE = '/api/bff/v1';

beforeEach(() => {
  vi.stubGlobal('localStorage', memoryStorage());
  useChatStore.setState({
    userProfile: { user_id: 'usr_1', email: 'a@b.c', display_name: 'A', tenant_id: 't', roles: [] } as never,
  });
  mockFetch({
    [`GET ${BASE}/privacy/notice`]: {
      version: '2026-10',
      controller: { name: 'Acme', contact_email: 'privacy@acme.example' },
      dpo_email: null,
      purposes: [],
      retention: { session_retention_days: 30, trash_grace_days: 7 },
      memory: { enabled: true },
      processors: [],
      rights: ['access'],
    },
    [`GET ${BASE}/me/privacy/acknowledgements/latest`]: { notice_version: null, acknowledged_at: null },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('WorkspaceLayout privacy notice', () => {
  it.each([
    ['/home', 'Home'],
    ['/tasks', 'Tasks'],
    ['/tasks/ses_1', 'Task'],
    ['/apps/app_1/run', 'Run app'],
  ])('shows the notice on %s until it is acknowledged', async (path, heading) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <WorkspaceLayout />
      </MemoryRouter>,
    );
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument();
    expect(await screen.findByRole('dialog', { name: 'Privacy notice' })).toBeInTheDocument();
  });
});
