/**
 * RbacPage — role bindings follow foundation-authz's lifecycle model:
 *  - the list is ACTIVE-only by default; "Show revoked & expired" asks for
 *    every state, and revoked / expired rows are marked and can't be revoked
 *  - counts say what they count (the tab: active grants; the summary: by state)
 *  - next_cursor is followed with "Load more"
 *  - api.admin.authz.bindings.list sends filter.status / cursor to the BFF
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DialogProvider } from '../components/dialogs';
import { useAdminStore } from '../../stores/adminStore';
import { formatDateTimeAuto } from '../../i18n/dates';
import type { RoleBinding } from '../../api/types';

const authz = vi.hoisted(() => ({
  rolesList: vi.fn(),
  permissionsList: vi.fn(),
  bindingsList: vi.fn(),
  bindingsDelete: vi.fn(),
}));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      admin: {
        tenants: { list: () => Promise.resolve({ items: [] }) },
        authz: {
          roles: { list: authz.rolesList, create: vi.fn(), patch: vi.fn(), delete: vi.fn() },
          permissions: { list: authz.permissionsList },
          bindings: { list: authz.bindingsList, create: vi.fn(), delete: authz.bindingsDelete },
        },
      },
    },
  };
});

import { RbacPage } from './RbacPage';

const ALL_STATES = ['ACTIVE', 'REVOKED', 'EXPIRED'];
const ACTIVE: RoleBinding = {
  id: 'rbd_1',
  subject_type: 'USER',
  subject_id: 'usr_alice',
  role_id: 'rol_1',
  status: 'ACTIVE',
  expires_at: null,
  revoked_at: null,
};
const REVOKED: RoleBinding = {
  id: 'rbd_2',
  subject_type: 'USER',
  subject_id: 'usr_bob',
  role_id: 'rol_1',
  status: 'REVOKED',
  expires_at: null,
  revoked_at: '2026-10-01T09:30:00Z',
  revoked_by: 'usr_admin',
  revoked_reason: 'API revoke',
};
const EXPIRED: RoleBinding = {
  id: 'rbd_3',
  subject_type: 'TEAM',
  subject_id: 'team_ops',
  role_id: 'rol_1',
  status: 'EXPIRED',
  expires_at: '2026-09-01T00:00:00Z',
  revoked_at: null,
};

const page = (items: RoleBinding[], next_cursor: string | null = null) => ({ items, next_cursor });

function renderPage() {
  return render(
    <DialogProvider>
      <RbacPage />
    </DialogProvider>,
  );
}

/** Render the page and switch to the bindings tab, whose label is `tabLabel`. */
async function openBindings(tabLabel: string) {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole('button', { name: tabLabel }));
  return user;
}

const rowOf = (subjectId: string) => screen.getByText(subjectId).closest('tr') as HTMLElement;

beforeEach(() => {
  useAdminStore.setState({ tenants: [{ id: 'tnt_x', name: 'X' } as never], selectedTenantId: null });
  authz.rolesList.mockResolvedValue([{ id: 'rol_1', name: 'kb_editor', display_name: 'KB editor', permissions: [] }]);
  authz.permissionsList.mockResolvedValue([]);
  authz.bindingsDelete.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('RbacPage — role bindings', () => {
  it('lists active bindings only by default and counts them as active', async () => {
    authz.bindingsList.mockResolvedValue(page([ACTIVE]));
    await openBindings('Role bindings (1 active)');

    expect(authz.bindingsList).toHaveBeenCalledWith(undefined, { status: undefined, cursor: undefined });
    expect(screen.getByRole('checkbox', { name: 'Show revoked & expired' })).not.toBeChecked();
    expect(screen.getByText('Showing 1 active binding')).toBeInTheDocument();
    const row = rowOf('usr_alice');
    expect(within(row).getByText('Active')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Revoke' })).toBeEnabled();
  });

  it('"Show revoked & expired" asks for every state and marks the inactive rows', async () => {
    authz.bindingsList.mockImplementation((_tid: unknown, opts?: { status?: string[] }) =>
      Promise.resolve(opts?.status ? page([ACTIVE, REVOKED, EXPIRED]) : page([ACTIVE])),
    );
    const user = await openBindings('Role bindings (1 active)');
    await user.click(screen.getByRole('checkbox', { name: 'Show revoked & expired' }));

    expect(await screen.findByText('Showing 3 bindings: 1 active · 1 revoked · 1 expired')).toBeInTheDocument();
    expect(authz.bindingsList).toHaveBeenLastCalledWith(undefined, { status: ALL_STATES, cursor: undefined });
    expect(screen.getByRole('checkbox', { name: 'Show revoked & expired' })).toBeChecked();

    // Revoked: when, by whom and why — and no second revoke.
    const revoked = rowOf('usr_bob');
    expect(within(revoked).getByText('Revoked')).toBeInTheDocument();
    expect(
      within(revoked).getByText(`${formatDateTimeAuto(new Date(REVOKED.revoked_at!))} · by usr_admin`),
    ).toBeInTheDocument();
    expect(within(revoked).getByText('Reason: API revoke')).toBeInTheDocument();
    expect(within(revoked).getByRole('button', { name: 'Revoke' })).toBeDisabled();

    const expired = rowOf('team_ops');
    expect(within(expired).getByText('Expired')).toBeInTheDocument();
    expect(within(expired).getByRole('button', { name: 'Revoke' })).toBeDisabled();

    expect(within(rowOf('usr_alice')).getByRole('button', { name: 'Revoke' })).toBeEnabled();
    // The tab keeps counting current grants only.
    expect(screen.getByRole('button', { name: 'Role bindings (1 active)' })).toBeInTheDocument();
  });

  it('refetches only the bindings when the filter changes, keeping the tab on screen', async () => {
    let answer: (p: ReturnType<typeof page>) => void = () => {};
    authz.bindingsList.mockImplementation((_tid: unknown, opts?: { status?: string[] }) =>
      opts?.status
        ? new Promise((resolve) => {
            answer = resolve;
          })
        : Promise.resolve(page([ACTIVE])),
    );
    const user = await openBindings('Role bindings (1 active)');
    await user.click(screen.getByRole('checkbox', { name: 'Show revoked & expired' }));

    // While the list is fetched again the rows and the tab's controls stay.
    expect(screen.getByText('Loading…')).toBeInTheDocument();
    expect(screen.getByText('usr_alice')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+ New binding' })).toBeInTheDocument();

    answer(page([ACTIVE, REVOKED]));
    expect(await screen.findByText('Showing 2 bindings: 1 active · 1 revoked · 0 expired')).toBeInTheDocument();
    expect(authz.rolesList).toHaveBeenCalledTimes(1);
    expect(authz.permissionsList).toHaveBeenCalledTimes(1);
  });

  it('derives the state when an older upstream sends no status', async () => {
    const { status: _omitted, ...legacy } = REVOKED;
    authz.bindingsList.mockResolvedValue(page([ACTIVE, legacy]));
    await openBindings('Role bindings (1 active)');

    expect(within(rowOf('usr_bob')).getByText('Revoked')).toBeInTheDocument();
    expect(within(rowOf('usr_bob')).getByRole('button', { name: 'Revoke' })).toBeDisabled();
    expect(screen.getByText('Showing 2 bindings: 1 active · 1 revoked · 0 expired')).toBeInTheDocument();
  });

  it('follows next_cursor with "Load more", appending the next page', async () => {
    const carol: RoleBinding = { ...ACTIVE, id: 'rbd_4', subject_id: 'usr_carol' };
    authz.bindingsList.mockResolvedValueOnce(page([ACTIVE], 'rbd_1')).mockResolvedValueOnce(page([carol]));
    const user = await openBindings('Role bindings (1+ active)');
    expect(screen.getByText('Showing 1 active binding · more not loaded yet')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Load more' }));

    expect(await screen.findByText('usr_carol')).toBeInTheDocument();
    expect(authz.bindingsList).toHaveBeenLastCalledWith(undefined, { status: undefined, cursor: 'rbd_1' });
    expect(screen.getByText('usr_alice')).toBeInTheDocument();
    expect(screen.getByText('Showing 2 active bindings')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Role bindings (2 active)' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('revokes an active binding after confirmation, then reloads', async () => {
    authz.bindingsList.mockResolvedValue(page([ACTIVE]));
    const user = await openBindings('Role bindings (1 active)');

    await user.click(within(rowOf('usr_alice')).getByRole('button', { name: 'Revoke' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke role binding' });
    await user.click(within(dialog).getByRole('button', { name: 'Revoke' }));

    expect(authz.bindingsDelete).toHaveBeenCalledWith('rbd_1', undefined);
    await vi.waitFor(() => expect(authz.bindingsList).toHaveBeenCalledTimes(2));
  });
});

describe('api.admin.authz.bindings.list', () => {
  it('sends the status filter and cursor and returns the page envelope', async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(
        new Response(JSON.stringify({ items: [], next_cursor: 'rbd_9' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    const { api } = await vi.importActual<typeof import('../../api/client')>('../../api/client');

    await expect(
      api.admin.authz.bindings.list('tnt_x', { status: ['ACTIVE', 'REVOKED', 'EXPIRED'], cursor: 'rbd_2' }),
    ).resolves.toEqual({ items: [], next_cursor: 'rbd_9' });
    expect(fetchMock.mock.calls[0]).toEqual([
      '/api/bff/v1/admin/authz/bindings?tenant_id=tnt_x&filter.status=ACTIVE%2CREVOKED%2CEXPIRED&cursor=rbd_2',
      expect.objectContaining({ method: 'GET' }),
    ]);

    await api.admin.authz.bindings.list();
    expect(fetchMock.mock.calls[1][0]).toBe('/api/bff/v1/admin/authz/bindings');
  });
});
