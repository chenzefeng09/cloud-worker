/**
 * ProfilePage — "Privacy & data" tab wiring: the tab exists, `?tab=privacy`
 * deep-links to it (the privacy notice links here), and the notice can be
 * reread / acknowledged from the tab.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { DialogProvider } from '../admin/components/dialogs';

vi.mock('../lib/workspaceContext', () => ({ useWorkspace: () => ({ allWs: [] }) }));
vi.mock('../i18n', () => ({ useTranslation: () => ({ lang: 'en', setLang: vi.fn(), t: (k: string) => k }) }));

// Everything the other tabs load on mount resolves to "empty".
vi.mock('../api/client', () => ({
  api: {
    auth: {
      me: () => Promise.resolve({ id: 'usr_1', email: 'a@b.c', display_name: 'Ada', tenant_id: 't', roles: [] }),
      mfaMethods: () => Promise.resolve([]),
      sessions: () => Promise.resolve([]),
      logout: () => Promise.resolve(),
      updateProfile: () => Promise.resolve({}),
    },
    billing: { me: () => Promise.resolve({ tenant_id: 't', plan: null, usage: {} }) },
    runs: {
      count: () => Promise.resolve({ count: 0 }),
      tokenUsage: () => Promise.resolve({ input_tokens: 0, output_tokens: 0, total_tokens: 0 }),
      tokenUsageBreakdown: () => Promise.resolve({ days_window: 30, daily: [], by_source: [] }),
    },
    apiKeys: { list: () => Promise.resolve([]) },
    notifications: { preferences: () => Promise.resolve({ items: [] }), updatePreferences: () => Promise.resolve({ items: [] }) },
    modelPreferences: {
      resolve: () => Promise.resolve({ purpose: 'llm', model_id: 'm', provider_id: 'p', source: 'system' }),
      list: () => Promise.resolve([]),
      listModels: () => Promise.resolve([]),
    },
    featureFlags: { list: () => Promise.resolve([]) },
    userFlagOverrides: { put: () => Promise.resolve({}), delete: () => Promise.resolve() },
  },
  ApiError: class ApiError extends Error {
    status: number;
    code: string;
    constructor(status: number, code: string, detail: string) {
      super(detail);
      this.status = status;
      this.code = code;
    }
  },
}));

const privacy = vi.hoisted(() => ({
  notice: vi.fn(),
  latestAcknowledgement: vi.fn(),
  acknowledgeNotice: vi.fn(),
  settings: vi.fn(),
  updateSettings: vi.fn(),
  memories: vi.fn(),
  deleteMemory: vi.fn(),
  deleteAllMemories: vi.fn(),
  exportMyData: vi.fn(),
  eraseMyData: vi.fn(),
}));
vi.mock('../api/privacy', () => ({
  privacyApi: privacy,
  ERASE_CONFIRMATION_PHRASE: 'DELETE MY DATA',
  errorDetails: () => undefined,
}));

import { ProfilePage } from './ProfilePage';

const NOTICE = {
  version: '2026-10',
  controller: { name: 'Acme', contact_email: 'privacy@acme.example' },
  dpo_email: null,
  purposes: [],
  retention: { session_retention_days: 30, trash_grace_days: 7 },
  memory: { enabled: true },
  processors: [],
  rights: ['access'],
};

function renderPage() {
  return render(
    <DialogProvider>
      <MemoryRouter>
        <ProfilePage />
      </MemoryRouter>
    </DialogProvider>,
  );
}

beforeEach(() => {
  privacy.notice.mockResolvedValue(NOTICE);
  privacy.latestAcknowledgement.mockResolvedValue({ notice_version: '2026-10', acknowledged_at: '2026-10-20T10:00:00Z' });
  privacy.acknowledgeNotice.mockResolvedValue(undefined);
  privacy.settings.mockResolvedValue({ memory_enabled: true, updated_at: null });
  privacy.memories.mockResolvedValue([]);
});

afterEach(() => {
  window.history.replaceState({}, '', '/');
  vi.clearAllMocks();
});

describe('ProfilePage — Privacy & data tab', () => {
  it('adds a "Privacy & data" tab with notice, memory, download and delete sections', async () => {
    renderPage();
    expect(screen.queryByRole('region', { name: 'Memory' })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Privacy & data' }));

    for (const name of ['Privacy notice', 'Memory', 'Download your data', 'Delete your data']) {
      expect(screen.getByRole('region', { name })).toBeInTheDocument();
    }
    await waitFor(() => expect(privacy.settings).toHaveBeenCalled());
    expect(privacy.memories).toHaveBeenCalled();
  });

  it('opens straight on the tab from /profile?tab=privacy', async () => {
    window.history.replaceState({}, '', '/profile?tab=privacy');
    renderPage();
    expect(await screen.findByRole('region', { name: 'Delete your data' })).toBeInTheDocument();
  });

  it('lets the user reread an acknowledged notice (read-only)', async () => {
    window.history.replaceState({}, '', '/profile?tab=privacy');
    renderPage();
    const section = await screen.findByRole('region', { name: 'Privacy notice' });
    expect(await within(section).findByText(/Version 2026-10 — acknowledged/)).toBeInTheDocument();

    await userEvent.click(within(section).getByRole('button', { name: 'Read notice' }));
    const dialog = await screen.findByRole('dialog', { name: 'Privacy notice' });
    expect(within(dialog).queryByRole('button', { name: 'Got it' })).not.toBeInTheDocument();
    // Already on the Privacy tab, so no link back to it.
    expect(within(dialog).queryByRole('link', { name: /Privacy & data/ })).not.toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('asks to acknowledge a new notice version and records it', async () => {
    privacy.latestAcknowledgement.mockResolvedValue({ notice_version: '2026-09', acknowledged_at: '2026-09-01T00:00:00Z' });
    window.history.replaceState({}, '', '/profile?tab=privacy');
    renderPage();
    const section = await screen.findByRole('region', { name: 'Privacy notice' });
    expect(await within(section).findByText(/haven't acknowledged this version/)).toBeInTheDocument();

    await userEvent.click(within(section).getByRole('button', { name: 'Review notice' }));
    const dialog = await screen.findByRole('dialog', { name: 'Privacy notice' });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Got it' }));

    await waitFor(() => expect(privacy.acknowledgeNotice).toHaveBeenCalledWith('2026-10'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(within(section).getByRole('button', { name: 'Read notice' })).toBeInTheDocument();
  });
});
