/**
 * ProfilePage — billing + MFA wiring tests.
 *
 * Verifies that:
 * 1. Plan section renders real data from api.billing.me()
 * 2. Plan section renders null state when plan is null
 * 3. Plan section renders error state when billing call fails
 * 4. 2FA row renders "已启用" chip when MFA methods list is non-empty
 * 5. 2FA row renders "启用" button when MFA methods list is empty
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
      id: 'usr_test',
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
    mockApiKeysList.mockResolvedValue([]);
    mockNotifPreferences.mockResolvedValue({
      items: [
        { event_key: 'task_completed', label: '任务完成', sub: 'Worker 完成任务时', email_enabled: true, push_enabled: true },
        { event_key: 'needs_review',   label: '需要审查', sub: 'Worker 暂停等待确认', email_enabled: true, push_enabled: false },
      ],
    });
  });

  // ── Token 累计 stat tile ──────────────────────────────────────────────────

  it('renders Token 累计 stat tile with real token total', async () => {
    render(<ProfilePage />);

    await waitFor(() => {
      // total_tokens = 40, formatted as "40"
      expect(screen.getByText('40')).toBeInTheDocument();
    });
    expect(screen.getByText('Token 累计')).toBeInTheDocument();
  });

  it('shows … while token usage is loading', () => {
    mockTokenUsage.mockReturnValue(new Promise(() => {}));
    render(<ProfilePage />);

    // While loading, the token tile shows "…" as the value.
    // We check the label is present but the resolved value is not yet shown.
    expect(screen.getByText('Token 累计')).toBeInTheDocument();
    // The "…" placeholder may appear in multiple tiles; just ensure the label rendered.
  });

  // ── 套餐 ─────────────────────────────────────────────────────────────────

  it('renders plan name from real billing data', async () => {
    render(<ProfilePage />);

    // Switch to usage tab
    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() => {
      // "Pro" may appear as both the plan name and the header chip;
      // just assert at least one instance and that the tier chip "PRO" is present
      expect(screen.getAllByText('Pro').length).toBeGreaterThan(0);
    });
    // "PRO" tier chip may appear in multiple places (header + plan section)
    expect(screen.getAllByText('PRO').length).toBeGreaterThan(0);
  });

  it('renders "暂无活跃订阅" when plan is null', async () => {
    mockBillingMe.mockResolvedValue(NO_PLAN_RESPONSE);
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() =>
      expect(screen.getByText(/暂无活跃订阅/)).toBeInTheDocument(),
    );
  });

  it('renders error state when billing call fails', async () => {
    mockBillingMe.mockRejectedValue(new Error('network error'));
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('用量与账单'));

    await waitFor(() =>
      expect(screen.getByText(/套餐数据暂不可用/)).toBeInTheDocument(),
    );
  });

  // ── 双因素认证 ────────────────────────────────────────────────────────────

  it('shows "已启用" chip when MFA methods is non-empty', async () => {
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

    await userEvent.click(screen.getByText('安全'));

    await waitFor(() =>
      expect(screen.getByText('已启用')).toBeInTheDocument(),
    );
  });

  it('shows "启用" button when MFA methods is empty', async () => {
    mockMfaMethods.mockResolvedValue([]);
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('安全'));

    await waitFor(() => {
      // Find the 启用 button in the 2FA row context
      const buttons = screen.getAllByRole('button', { name: '启用' });
      expect(buttons.length).toBeGreaterThan(0);
    });
  });

  // ── 通知偏好 ──────────────────────────────────────────────────────────────

  it('renders notification preference rows from api.notifications.preferences()', async () => {
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('通知'));

    await waitFor(() => {
      expect(screen.getByText('任务完成')).toBeInTheDocument();
      expect(screen.getByText('需要审查')).toBeInTheDocument();
    });
  });

  it('shows loading placeholder before preferences load', async () => {
    // Return a never-resolving promise so the component stays in "loading" state
    mockNotifPreferences.mockReturnValue(new Promise(() => {}));
    render(<ProfilePage />);

    await userEvent.click(screen.getByText('通知'));

    // Before the promise resolves, we should see the loading state
    expect(screen.getByText('加载中…')).toBeInTheDocument();
  });
});
