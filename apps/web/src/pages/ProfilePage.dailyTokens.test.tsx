/**
 * ProfilePage — token usage dashboard tests (Usage & billing tab).
 *
 * Verifies the multi-dimensional breakdown built on
 * api.runs.tokenUsageBreakdown():
 * 1. Stacked daily chart renders one bar per day with MM-DD labels
 * 2. "No token usage in the last N days" empty state shown when all totals are 0
 * 3. Loading placeholder shown while the breakdown API resolves
 * 4. Total token count shown in the section title (reflecting the window)
 * 5. By source section renders localized source rows with percent-of-total
 * 6. Time-range segmented control refetches with the chosen window
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
      tokenUsageBreakdown: (days: number) => mockTokenUsageBreakdown(days),
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

import { ProfilePage } from './ProfilePage';

const BREAKDOWN_WITH_USAGE = {
  days_window: 30,
  daily: [
    { day: '2026-05-29', input_tokens: 0,   output_tokens: 0,   total_tokens: 0 },
    { day: '2026-05-30', input_tokens: 60,  output_tokens: 40,  total_tokens: 100 },
    { day: '2026-05-31', input_tokens: 120, output_tokens: 80,  total_tokens: 200 },
    { day: '2026-06-01', input_tokens: 90,  output_tokens: 60,  total_tokens: 150 },
    { day: '2026-06-02', input_tokens: 180, output_tokens: 120, total_tokens: 300 },
    { day: '2026-06-03', input_tokens: 30,  output_tokens: 20,  total_tokens: 50 },
    { day: '2026-06-04', input_tokens: 220, output_tokens: 200, total_tokens: 420 },
  ],
  by_source: [
    { source: 'user', total_tokens: 900, run_count: 4 },
    { source: 'schedule', total_tokens: 320, run_count: 2 },
  ],
};

const BREAKDOWN_ALL_ZERO = {
  days_window: 30,
  daily: [
    { day: '2026-05-29', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-05-30', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-05-31', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-06-01', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-06-02', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-06-03', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    { day: '2026-06-04', input_tokens: 0, output_tokens: 0, total_tokens: 0 },
  ],
  by_source: [],
};

describe('ProfilePage — token usage dashboard', () => {
  beforeEach(() => {
    mockTokenUsageBreakdown.mockReset();
    mockAuthMe.mockResolvedValue({
      user_id: 'usr_test',
      email: 'test@example.com',
      display_name: 'Test User',
      tenant_id: 'tnt_test',
      roles: [],
    });
    mockBillingMe.mockResolvedValue({ tenant_id: 'tnt_test', plan: null, usage: {} });
    mockMfaMethods.mockResolvedValue([]);
    mockAuthSessions.mockResolvedValue([]);
    mockRunsCount.mockResolvedValue({ count: 5 });
    mockTokenUsage.mockResolvedValue({ input_tokens: 600, output_tokens: 620, total_tokens: 1220 });
    mockTokenUsageDaily.mockResolvedValue({ days: [] });
    mockTokenUsageBreakdown.mockResolvedValue(BREAKDOWN_WITH_USAGE);
    mockApiKeysList.mockResolvedValue([]);
    mockNotifPreferences.mockResolvedValue({ items: [] });
  });

  it('renders chart section title on Usage & billing tab (default 30-day window)', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      expect(screen.getByText(/Token usage \(last 30 days\)/)).toBeInTheDocument();
    });
  });

  it('shows total token count in section title', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    // Total = 0+100+200+150+300+50+420 = 1220
    await waitFor(() => {
      expect(screen.getByText(/1,220 tokens total/)).toBeInTheDocument();
    });
  });

  it('renders stacked-bar tooltips with MM-DD labels', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      // StackedBars renders date labels sliced to MM-DD (d.day.slice(5))
      expect(screen.getByText('06-04')).toBeInTheDocument();
    });
  });

  it('shows "No token usage in the last 30 days" when all totals are 0', async () => {
    mockTokenUsageBreakdown.mockResolvedValue(BREAKDOWN_ALL_ZERO);
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      expect(screen.getByText(/No token usage in the last 30 days/)).toBeInTheDocument();
    });
  });

  it('shows loading placeholder before breakdown data resolves', async () => {
    mockTokenUsageBreakdown.mockReturnValue(new Promise(() => {})); // never resolves
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    expect(screen.getByText('Loading…')).toBeInTheDocument();
  });

  it('renders one bar entry per day', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      // Each day renders a div with a title attribute containing "tokens"
      const bars = document.querySelectorAll('[title*="tokens"]');
      expect(bars.length).toBe(7);
    });
  });

  it('renders By source rows with localized labels and percent-of-total', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      expect(screen.getByText('By source')).toBeInTheDocument();
    });
    // user → Manual, schedule → Scheduled
    expect(screen.getByText('Manual')).toBeInTheDocument();
    expect(screen.getByText('Scheduled')).toBeInTheDocument();
    // total = 900 + 320 = 1220 → user 73.8%, schedule 26.2%
    expect(screen.getByText('73.8%')).toBeInTheDocument();
    expect(screen.getByText('26.2%')).toBeInTheDocument();
  });

  it('shows "No usage data yet" when by_source is empty', async () => {
    mockTokenUsageBreakdown.mockResolvedValue(BREAKDOWN_ALL_ZERO);
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      expect(screen.getByText(/No usage data yet/)).toBeInTheDocument();
    });
  });

  it('refetches with the chosen window when time-range changes', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('Usage & billing'));

    await waitFor(() => {
      expect(mockTokenUsageBreakdown).toHaveBeenCalledWith(30);
    });

    await userEvent.click(screen.getByText('Last 7 days'));
    await waitFor(() => {
      expect(mockTokenUsageBreakdown).toHaveBeenCalledWith(7);
    });
  });
});
