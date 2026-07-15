import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { LibraryPage } from './LibraryPage';

const mocks = vi.hoisted(() => ({
  listMcpServers: vi.fn(),
  listSkills: vi.fn(),
  listDocuments: vi.fn(),
  listSkillVersions: vi.fn(),
  refreshInstalled: vi.fn(),
}));

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    library: {
      listMcpServers: (...args: unknown[]) => mocks.listMcpServers(...args),
      listSkills: (...args: unknown[]) => mocks.listSkills(...args),
      listDocuments: (...args: unknown[]) => mocks.listDocuments(...args),
      listSkillVersions: (...args: unknown[]) => mocks.listSkillVersions(...args),
      installSkill: vi.fn(),
    },
  },
}));

vi.mock('../hooks/useInstalledSkills', () => ({
  useInstalledSkills: () => Object.assign([
    {
      id: 'sk_default',
      tenant_id: 'tnt_test',
      name: 'docx',
      description: 'Word documents',
      owner: null,
      visibility: 'tenant',
      created_at: '2026-07-01T00:00:00Z',
      enabled_by_default: true,
      installed_at: '2026-07-02T00:00:00Z',
    },
    {
      id: 'sk_ondemand',
      tenant_id: 'tnt_test',
      name: 'pdf',
      description: 'PDF documents',
      owner: null,
      visibility: 'tenant',
      created_at: '2026-07-01T00:00:00Z',
      enabled_by_default: false,
      installed_at: '2026-07-02T00:00:00Z',
    },
  ], { refresh: mocks.refreshInstalled }),
}));

describe('LibraryPage skill rows', () => {
  beforeEach(() => {
    mocks.listMcpServers.mockResolvedValue({ items: [], total: 0 });
    mocks.listDocuments.mockResolvedValue({ items: [], total: 0 });
    mocks.listSkillVersions.mockResolvedValue([]);
    mocks.listSkills.mockResolvedValue({
      items: [
        {
          id: 'sk_default',
          tenant_id: 'tnt_test',
          name: 'docx',
          description: `Use this skill ${'for very long descriptions '.repeat(30)}`,
          owner: null,
          visibility: 'tenant',
          created_at: '2026-07-01T00:00:00Z',
        },
        {
          id: 'sk_ondemand',
          tenant_id: 'tnt_test',
          name: 'pdf',
          description: 'PDF documents',
          owner: null,
          visibility: 'tenant',
          created_at: '2026-07-01T00:00:00Z',
        },
        {
          id: 'sk_new',
          tenant_id: 'tnt_test',
          name: 'xlsx',
          description: 'Spreadsheet files',
          owner: null,
          visibility: 'tenant',
          created_at: '2026-07-01T00:00:00Z',
        },
      ],
      total: 3,
    });
  });

  it('contains long descriptions and metadata inside the bounded grid', async () => {
    const user = userEvent.setup();
    render(<LibraryPage />);

    await user.click(screen.getByRole('button', { name: /技能/ }));

    const row = await screen.findByTestId('skill-row-sk_default');
    expect(row).toHaveClass('grid-cols-[minmax(0,1fr)_80px_100px_72px]');
    expect(row.querySelector('.min-w-0')).not.toBeNull();
    expect(row).toHaveTextContent('默认启用');
    expect(screen.getByTestId('skill-row-sk_ondemand')).toHaveTextContent('按需启用');
  });

  it('explains default and per-session activation in the install dialog', async () => {
    const user = userEvent.setup();
    render(<LibraryPage />);

    await user.click(screen.getByRole('button', { name: /技能/ }));
    await user.click(await screen.findByRole('button', { name: '安装' }));

    expect(screen.getByRole('checkbox')).toBeChecked();
    expect(screen.getByText('默认在新会话启用')).toBeInTheDocument();
    expect(screen.getByText(/每个会话输入框的 ⊕ 菜单/)).toBeInTheDocument();
  });
});
