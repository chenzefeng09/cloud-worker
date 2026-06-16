/**
 * ProfilePage — display-name rename flow tests (账户 tab → 个人信息).
 *
 * Covers handleSaveProfile + the edit-mode handlers (enterProfileEdit /
 * cancelProfileEdit). The rename headline calls api.auth.updateProfile(
 * { display_name }) → PATCH /auth/me, then setLiveProfile(updated) and store
 * setUserProfile(updated).
 *
 * 1. SUCCESS — 编辑 → change 显示名称 → 保存更改: updateProfile called with the
 *    trimmed value, field returns read-only showing the new name, 已保存 shown.
 * 2. FAILURE — updateProfile rejects: stays in edit mode, 保存失败 shown.
 * 3. NO-OP — empty or unchanged name: exits edit WITHOUT calling updateProfile.
 * 4. PERMISSION-SURVIVES-SAVE (B1 guard) — updateProfile resolves with a profile
 *    carrying `permissions`; setUserProfile is called with an object that still
 *    carries `permissions` (the component does not drop it).
 * 5. ENTER-key saves; CANCEL reverts (nice-to-have).
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
  useTranslation: () => ({ lang: 'zh', setLang: vi.fn(), t: (k: string) => k }),
}));

const mockSetUserProfile = vi.fn();

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
      setUserProfile: mockSetUserProfile,
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
const mockUpdateProfile = vi.fn();

vi.mock('../api/client', () => ({
  api: {
    auth: {
      me: () => mockAuthMe(),
      mfaMethods: () => mockMfaMethods(),
      logout: () => Promise.resolve(),
      sessions: () => mockAuthSessions(),
      updateProfile: (...args: unknown[]) => mockUpdateProfile(...args),
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

const BASE_PROFILE = {
  id: 'usr_test',
  email: 'test@example.com',
  display_name: 'Test User',
  tenant_id: 'tnt_test',
  roles: [],
};

// The 显示名称 input is the first text input in the 个人信息 section. We grab it
// by its current display value (the read-only field shows the live name).
function nameInput(currentValue: string): HTMLInputElement {
  return screen.getByDisplayValue(currentValue) as HTMLInputElement;
}

describe('ProfilePage — display-name rename flow', () => {
  beforeEach(() => {
    mockSetUserProfile.mockReset();
    mockUpdateProfile.mockReset();
    mockAuthMe.mockResolvedValue({ ...BASE_PROFILE });
    mockBillingMe.mockResolvedValue({ tenant_id: 'tnt_test', plan: null, usage: {} });
    mockMfaMethods.mockResolvedValue([]);
    mockAuthSessions.mockResolvedValue([]);
    mockRunsCount.mockResolvedValue({ count: 5 });
    mockTokenUsage.mockResolvedValue({ input_tokens: 600, output_tokens: 620, total_tokens: 1220 });
    mockTokenUsageDaily.mockResolvedValue({ days: [] });
    mockTokenUsageBreakdown.mockResolvedValue({ days_window: 30, daily: [], by_source: [] });
    mockApiKeysList.mockResolvedValue([]);
    mockNotifPreferences.mockResolvedValue({ items: [] });
  });

  it('saves a renamed display name and returns to read-only with success message', async () => {
    mockUpdateProfile.mockResolvedValue({ ...BASE_PROFILE, display_name: 'Renamed User' });
    render(<ProfilePage />);

    // Wait for liveProfile (api.auth.me) to resolve so the field shows the real name.
    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));

    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.type(input, '  Renamed User  ');

    await userEvent.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => {
      expect(mockUpdateProfile).toHaveBeenCalledWith({ display_name: 'Renamed User' });
    });

    // Back to read-only: the 编辑 button is shown again (保存更改 is gone) and the
    // field now shows the new name.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: '保存更改' })).not.toBeInTheDocument();
    expect(nameInput('Renamed User')).toBeInTheDocument();
    expect(screen.getByText('已保存')).toBeInTheDocument();
  });

  it('stays in edit mode and shows 保存失败 when updateProfile rejects', async () => {
    mockUpdateProfile.mockRejectedValue(new Error('boom'));
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));

    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.type(input, 'New Name');

    await userEvent.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => {
      expect(screen.getByText('保存失败')).toBeInTheDocument();
    });
    // Still in edit mode: 保存更改 button remains.
    expect(screen.getByRole('button', { name: '保存更改' })).toBeInTheDocument();
  });

  it('does not call updateProfile when the name is unchanged (no-op exits edit)', async () => {
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));
    // Save without changing anything.
    await userEvent.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument();
    });
    expect(mockUpdateProfile).not.toHaveBeenCalled();
  });

  it('does not call updateProfile when the name is emptied (no-op exits edit)', async () => {
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument();
    });
    expect(mockUpdateProfile).not.toHaveBeenCalled();
  });

  it('passes permissions through to setUserProfile on save (B1 regression guard)', async () => {
    const updatedWithPerms = {
      ...BASE_PROFILE,
      display_name: 'Perms User',
      permissions: ['admin.users.read', 'admin.billing.read'],
    };
    mockUpdateProfile.mockResolvedValue(updatedWithPerms);
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.type(input, 'Perms User');
    await userEvent.click(screen.getByRole('button', { name: '保存更改' }));

    await waitFor(() => {
      expect(mockSetUserProfile).toHaveBeenCalled();
    });
    const passed = mockSetUserProfile.mock.calls[0][0];
    expect(passed.permissions).toEqual(['admin.users.read', 'admin.billing.read']);
  });

  it('saves on Enter key in the 显示名称 input', async () => {
    mockUpdateProfile.mockResolvedValue({ ...BASE_PROFILE, display_name: 'Enter Name' });
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.type(input, 'Enter Name{Enter}');

    await waitFor(() => {
      expect(mockUpdateProfile).toHaveBeenCalledWith({ display_name: 'Enter Name' });
    });
  });

  it('reverts the edited name when 取消 is clicked', async () => {
    render(<ProfilePage />);

    await waitFor(() => expect(nameInput('Test User')).toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: '编辑' }));
    const input = nameInput('Test User');
    await userEvent.clear(input);
    await userEvent.type(input, 'Discarded');

    await userEvent.click(screen.getByRole('button', { name: '取消' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '编辑' })).toBeInTheDocument();
    });
    // Field reverted to the live name, edit not saved.
    expect(nameInput('Test User')).toBeInTheDocument();
    expect(mockUpdateProfile).not.toHaveBeenCalled();
  });
});
