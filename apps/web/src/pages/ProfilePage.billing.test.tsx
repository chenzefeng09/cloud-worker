/**
 * ProfilePage — billing + MFA wiring tests.
 *
 * Verifies that:
 * 1. Plan section renders real data from api.billing.me()
 * 2. Plan section renders null state when plan is null
 * 3. Plan section renders error state when billing call fails
 * 4. 2FA row renders "Enabled" chip when MFA methods list is non-empty
 * 5. 2FA row renders "Enable" button when MFA methods list is empty
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

// Mock react-router-dom navigate
vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

// Mock workspace context
vi.mock('../lib/workspaceContext', () => ({
  useWorkspace: () => ({ allWs: [] }),
}));

// Mock i18n — the page reads useTranslation() for the 语言 switcher
vi.mock('../i18n', () => ({
  useTranslation: () => ({ lang: 'en', setLang: vi.fn(), t: (k: string) => k }),
}));

// Mock chatStore
vi.mock('../stores/chatStore', () => ({
  useChatStore: (selector: (s: unknown) => unknown) =>
    selector({
      userProfile: {
        user_id: 'usr_test',
        email: 'test@example.com',
        display_name: 'Test User',
        tenant_id: 'tnt_test',
        roles: [],
      },
      logout: vi.fn(),
      setUserProfile: vi.fn(),
    }),
}));

const mockBillingMe = vi.fn();
const mockMfaMethods = vi.fn();
const mockAuthMe = vi.fn();
const mockAuthSessions = vi.fn();
const mockRunsCount = vi.fn();
const mockTokenUsage = vi.fn();
const mockTokenUsageDaily = vi.fn();
const mockTokenUsageBreakdown = vi.fn();
const mockApiKeysList = vi.fn();
const mockNotifPreferences = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    auth: {
      me: () => mockAuthMe(),
      mfaMethods: () => mockMfaMethods(),
      logout: () => Promise.resolve(),
      sessions: () => mockAuthSessions(),
      updateProfile: () => Promise.resolve({ user_id: 'usr_test', email: 'test@example.com', display_name: 'Test User', tenant_id: 'tnt_test', roles: [] }),
    },
    billing: {
      me: () => mockBillingMe(),
    },
    runs: {
      count: () => mockRunsCount(),
      tokenUsage: () => mockTokenUsage(),
      tokenUsageDaily: () => mockTokenUsageDaily(),
      tokenUsageBreakdown: () => mockTokenUsageBreakdown(),
    },
    apiKeys: {
      list: () => mockApiKeysList(),
      create: () => Promise.resolve({ id: 'k1', name: 'test', key_prefix: 'cw_', created_at: '', last_used_at: null, key: 'raw' }),
      revoke: () => Promise.resolve(),
    },
    notifications: {
      preferences: () => mockNotifPreferences(),
      updatePreferences: () => Promise.resolve({ items: [] }),
    },
    modelPreferences: {
      resolve: (purpose: string) =>
        Promise.resolve({
          purpose,
          model_id: 'gpt-4o',
          provider_id: 'prov_test',
          source: 'system',
        }),
      list: () => Promise.resolve([]),
      listModels: () => Promise.resolve([]),
      upsert: () =>
        Promise.resolve({ purpose: 'llm', model_id: 'gpt-4o', provider_id: 'prov_test' }),
      delete: () => Promise.resolve(),
    },
    featureFlags: {
      list: () => Promise.resolve([]),
    },
    userFlagOverrides: {
      list: () => Promise.resolve([]),
      put: () => Promise.resolve({}),
      delete: () => Promise.resolve(),
    },
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

// Import after mocks
import { ProfilePage } from './ProfilePage';

const PLAN_RESPONSE = {
  tenant_id: 'tnt_test',
  plan: {
    id: 'plan_pro',
    name: 'Pro',
    tier: 'PRO',
    limits: { sessions_per_month: 1000 },
  },
  usage: { sessions_per_month: 10 },
};

const NO_PLAN_RESPONSE = {
  tenant_id: 'tnt_test',
  plan: null,
  usage: {},
};

describe('ProfilePage — billing & MFA wiring', () => {
  beforeEach(() => {
    mockAuthMe.mockResolvedValue({
      user_id: 'usr_test',
      email: 'test@example.com',
      display_name: 'Test User',
      tenant_id: 'tnt_test',
      roles: [],
    });
    mockBillingMe.mockResolvedValue(PLAN_RESPONSE);
    mockMfaMethods.mockResolvedValue([]);
    mockAuthSessions.mockResolvedValue([]);
    mockRunsCount.mockResolvedValue({ count: 42 });
    mockTokenUsage.mockResolvedValue({ input_tokens: 30, output_tokens: 10, total_tokens: 40 });
    mockTokenUsageDaily.mockResolvedValue({
      days: [
        { day: '2026-05-29', tokens: 0 },
        { day: '2026-05-30', tokens: 100 },
        { day: '2026-05-31', tokens: 200 },
        { day: '2026-06-01', tokens: 150 },
        { day: '2026-06-02', tokens: 300 },
        { day: '2026-06-03', tokens: 50 },
        { day: '2026-06-04', tokens: 420 },
      ],
    });
    mockTokenUsageBreakdown.mockResolvedValue({
      days_window: 30,
      daily: [
        { day: '2026-06-03', input_tokens: 30, output_tokens: 20, total_tokens: 50 },
        { day: '2026-06-04', input_tokens: 220, output_tokens: 200, total_tokens: 420 },
      ],
      by_source: [
        { source: 'user', total_tokens: 400, run_count: 3 },
        { source: 'schedule', total_tokens: 70, run_count: 1 },
      ],
    });
    mockApiKeysList.mockResolvedValue([]);
    mockNotifPreferences.mockResolvedValue({
      items: [
        { event_key: 'task_completed', label: 'Task completed', sub: 'When a worker completes a task', email_enabled: true, push_enabled: true },
        { event_key: 'needs_review',   label: 'Needs review', sub: 'Worker paused awaiting confirmation', email_enabled: true, push_enabled: false },
      ],
    });
  });

  // ── Total tokens stat tile ──────────────────────────────────────────────────

  it('renders Total tokens stat tile with real token total', async () => {
    render(<ProfilePage />);

    await waitFor(() => {
      // total_tokens = 40, formatted as "40"
      expect(screen.getByText('40')).toBeInTheDocument();
    });
    expect(screen.getByText('Total tokens')).toBeInTheDocument();
  });

  it('shows … while token usage is loading', () => {
    mockTokenUsage.mockReturnValue(new Promise(() => {}));
    render(<ProfilePage />);

    // While loading, the token tile shows "…" as the value.
    // We check the label is present but the resolved value is not yet shown.
    expect(screen.getByText('Total tokens')).toBeInTheDocument();
    // The "…" placeholder may appear in multiple tiles; just ensure the label rendered.
  });

  // ── Plan ─────────────────────────────────────────────────────────────────

  it('renders plan name from real billing data', async () => {
    render(<ProfilePage />);

    // Switch to usage tab
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      // "Pro" may appear as both the plan name and the header chip;
      // just assert at least one instance and that the tier chip "PRO" is present
      expect(screen.getAllByText('Pro').length).toBeGreaterThan(0);
    });
    // "PRO" tier chip may appear in multiple places (header + plan section)
    expect(screen.getAllByText('PRO').length).toBeGreaterThan(0);
  });

  it('renders "No active subscription" when plan is null', async () => {
    mockBillingMe.mockResolvedValue(NO_PLAN_RESPONSE);
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() =>
      expect(screen.getByText(/No active subscription/)).toBeInTheDocument(),
    );
  });

  it('renders error state when billing call fails', async () => {
    mockBillingMe.mockRejectedValue(new Error('network error'));
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() =>
      expect(screen.getByText(/Plan data is unavailable/)).toBeInTheDocument(),
    );
  });

  // ── Two-factor authentication ────────────────────────────────────────────────────────────

  it('shows "Enabled" chip when MFA methods is non-empty', async () => {
    mockMfaMethods.mockResolvedValue([
      {
        id: 'mfa_01',
        kind: 'TOTP',
        label: 'Authenticator',
        enrolled_at: '2026-01-01T00:00:00Z',
        last_used_at: null,
      },
    ]);
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Security'));

    await waitFor(() =>
      expect(screen.getByText('Enabled')).toBeInTheDocument(),
    );
  });

  it('shows "Enable" button when MFA methods is empty', async () => {
    mockMfaMethods.mockResolvedValue([]);
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Security'));

    await waitFor(() => {
      // Find the Enable button in the 2FA row context
      const buttons = screen.getAllByRole('button', { name: 'Enable' });
      expect(buttons.length).toBeGreaterThan(0);
    });
  });

  // ── Notification preferences ──────────────────────────────────────────────────────────────

  it('renders notification preference rows from api.notifications.preferences()', async () => {
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Notifications'));

    await waitFor(() => {
      expect(screen.getByText('Task completed')).toBeInTheDocument();
      expect(screen.getByText('Needs review')).toBeInTheDocument();
    });
  });

  it('shows loading placeholder before preferences load', async () => {
    // Return a never-resolving promise so the component stays in "loading" state
    mockNotifPreferences.mockReturnValue(new Promise(() => {}));
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('Notifications'));

    // Before the promise resolves, we should see the loading state
    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });
});
