/**
 * ProfilePage — daily token usage bar chart tests.
 *
 * Verifies:
 * 1. Chart renders bars with real days data from api.runs.tokenUsageDaily()
 * 2. "近 7 天无 Token 用量" empty state shown when all tokens are 0
 * 3. Loading placeholder shown while API resolves
 * 4. Total token count shown in section title
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

// Mock chatStore
vi.mock('../stores/chatStore', () => ({
  useChatStore: (selector: (s: unknown) => unknown) =>
    selector({
      userProfile: {
        id: 'usr_test',
        email: 'test@example.com',
        display_name: 'Test User',
        tenant_id: 'tnt_test',
        roles: [],
      },
      logout: vi.fn(),
    }),
}));

const mockBillingMe = vi.fn();
const mockMfaMethods = vi.fn();
const mockAuthMe = vi.fn();
const mockAuthSessions = vi.fn();
const mockRunsCount = vi.fn();
const mockTokenUsage = vi.fn();
const mockTokenUsageDaily = vi.fn();
const mockApiKeysList = vi.fn();
const mockNotifPreferences = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    auth: {
      me: () => mockAuthMe(),
      mfaMethods: () => mockMfaMethods(),
      logout: () => Promise.resolve(),
      sessions: () => mockAuthSessions(),
    },
    billing: {
      me: () => mockBillingMe(),
    },
    runs: {
      count: () => mockRunsCount(),
      tokenUsage: () => mockTokenUsage(),
      tokenUsageDaily: () => mockTokenUsageDaily(),
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

const DAILY_DATA_WITH_USAGE = {
  days: [
    { day: '2026-05-29', tokens: 0 },
    { day: '2026-05-30', tokens: 100 },
    { day: '2026-05-31', tokens: 200 },
    { day: '2026-06-01', tokens: 150 },
    { day: '2026-06-02', tokens: 300 },
    { day: '2026-06-03', tokens: 50 },
    { day: '2026-06-04', tokens: 420 },
  ],
};

const DAILY_DATA_ALL_ZERO = {
  days: [
    { day: '2026-05-29', tokens: 0 },
    { day: '2026-05-30', tokens: 0 },
    { day: '2026-05-31', tokens: 0 },
    { day: '2026-06-01', tokens: 0 },
    { day: '2026-06-02', tokens: 0 },
    { day: '2026-06-03', tokens: 0 },
    { day: '2026-06-04', tokens: 0 },
  ],
};

describe('ProfilePage — daily token usage chart', () => {
  beforeEach(() => {
    mockAuthMe.mockResolvedValue({
      id: 'usr_test',
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
    mockTokenUsageDaily.mockResolvedValue(DAILY_DATA_WITH_USAGE);
    mockApiKeysList.mockResolvedValue([]);
    mockNotifPreferences.mockResolvedValue({ items: [] });
  });

  it('renders chart section title on 用量与账单 tab', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() => {
      expect(screen.getByText(/每日 Token 用量/)).toBeInTheDocument();
    });
  });

  it('shows total token count in section title', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    // Total = 0+100+200+150+300+50+420 = 1220
    await waitFor(() => {
      expect(screen.getByText(/1,220 tokens 合计/)).toBeInTheDocument();
    });
  });

  it('renders bar tooltips with MM-DD dates', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() => {
      // SparkBars renders date labels sliced to MM-DD (d.day.slice(5))
      expect(screen.getByText('06-04')).toBeInTheDocument();
    });
  });

  it('shows "近 7 天无 Token 用量" when all tokens are 0', async () => {
    mockTokenUsageDaily.mockResolvedValue(DAILY_DATA_ALL_ZERO);
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() => {
      expect(screen.getByText(/近 7 天无 Token 用量/)).toBeInTheDocument();
    });
  });

  it('shows loading placeholder before daily data resolves', async () => {
    mockTokenUsageDaily.mockReturnValue(new Promise(() => {})); // never resolves
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    expect(screen.getByText('加载中…')).toBeInTheDocument();
  });

  it('renders 7 bar entries (one per day)', async () => {
    render(<ProfilePage />);
    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() => {
      // Each day renders a div with a title attribute like "YYYY-MM-DD · N tokens"
      const bars = document.querySelectorAll('[title*="tokens"]');
      expect(bars.length).toBe(7);
    });
  });
});
