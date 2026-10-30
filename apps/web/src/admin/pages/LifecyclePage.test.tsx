/**
 * LifecyclePage — hard-delete and legal-hold release take two admins: asking
 * only opens an approval request (202), which another admin approves from the
 * Approval requests section. The page must never claim the deletion or release
 * happened when it only asked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ApiError } from '../../api/client';
import type { ApprovalRequest } from '../../api/types';
import { formatDateTimeAuto } from '../../i18n/dates';
import { DialogProvider } from '../components/dialogs';
import { useAdminStore } from '../../stores/adminStore';
import { useChatStore } from '../../stores/chatStore';

const lc = vi.hoisted(() => ({
  binList: vi.fn(),
  hardDelete: vi.fn(),
  holdsList: vi.fn(),
  release: vi.fn(),
  approvalsList: vi.fn(),
  approve: vi.fn(),
  reject: vi.fn(),
}));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      admin: {
        tenants: { list: () => Promise.resolve({ items: [] }) },
        lifecycle: {
          recycleBin: { list: lc.binList, softDelete: vi.fn(), restore: vi.fn(), hardDelete: lc.hardDelete },
          sweep: vi.fn(),
          legalHolds: { list: lc.holdsList, create: vi.fn(), release: lc.release },
          approvalRequests: { list: lc.approvalsList, get: vi.fn(), approve: lc.approve, reject: lc.reject },
        },
      },
    },
  };
});

import { LifecyclePage } from './LifecyclePage';

const ME = 'usr_01J5ME000000000000000000ME';
const OTHER = 'usr_01J5OTHER00000000000000OTH';
const EXPIRES_AT = '2026-10-30T10:30:00+00:00';

function approval(overrides: Partial<ApprovalRequest>): ApprovalRequest {
  return {
    id: 'apr_01J9AAAAAAAAAAAAAAAAAAAAAA',
    operation: 'HARD_DELETE',
    target_id: 'bin_1',
    status: 'PENDING',
    requested_by: ME,
    requested_at: '2026-10-30T10:00:00+00:00',
    expires_at: EXPIRES_AT,
    decided_by: null,
    decided_at: null,
    ...overrides,
  };
}

const MINE = approval({ id: 'apr_01J9MINE00000000000000MINE', requested_by: ME });
const THEIRS = approval({
  id: 'apr_01J9THEIRS000000000000THRS',
  operation: 'RELEASE_LEGAL_HOLD',
  target_id: 'lhd_1',
  requested_by: OTHER,
});
const THEIRS_DELETE = approval({ id: 'apr_01J9THEIRSDEL000000000TDEL', requested_by: OTHER });

const expiry = formatDateTimeAuto(new Date(EXPIRES_AT));

function renderPage() {
  return render(
    <DialogProvider>
      <LifecyclePage />
    </DialogProvider>,
  );
}

const approvalsSection = () => screen.getByRole('region', { name: 'Approval requests' });

/** The approval-request row naming this requester. */
async function approvalRow(requester: string) {
  const section = approvalsSection();
  const cell = await within(section).findByText(requester);
  return cell.closest('tr') as HTMLElement;
}

async function confirmDialog(title: string, button: string) {
  const dialog = await screen.findByRole('dialog', { name: title });
  await userEvent.click(within(dialog).getByRole('button', { name: button }));
}

beforeEach(() => {
  useAdminStore.setState({ tenants: [{ id: 'tnt_x', name: 'X' } as never], selectedTenantId: null });
  useChatStore.setState({ userProfile: { user_id: ME, email: 'me@example.com', display_name: 'Me', tenant_id: 'tnt_x', roles: [] } });
  lc.binList.mockResolvedValue({
    items: [{ id: 'bin_1', resource_type: 'document', resource_id: 'doc-1', deleted_at: '2026-10-29T09:00:00+00:00', expires_at: '2026-11-28T09:00:00+00:00' }],
    next_cursor: null,
  });
  lc.holdsList.mockResolvedValue([
    { id: 'lhd_1', resource_type: 'mailbox', resource_id: 'mbx-7', reason: 'Litigation hold for case 42', placed_at: '2026-10-01T09:00:00+00:00' },
  ]);
  lc.approvalsList.mockResolvedValue({ items: [], next_cursor: null });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('LifecyclePage — asking for hard-delete / release', () => {
  it('says a hard-delete waits for a second admin and shows when it expires', async () => {
    lc.hardDelete.mockResolvedValue({ destroyed: false, approval_request: MINE });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'Permanently delete' }));
    await confirmDialog('Permanently delete', 'Request deletion');

    // No body: the requester is the signed-in admin, not a name the page sends.
    await waitFor(() => expect(lc.hardDelete).toHaveBeenCalledWith('bin_1', undefined));
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent('Permanent deletion requested. It is waiting for a second admin to approve it.');
    expect(notice).toHaveTextContent(`The request expires at ${expiry}.`);
    expect(screen.queryByText('Permanently deleted')).not.toBeInTheDocument();
  });

  it('names the admin who already asked when the pending request is theirs', async () => {
    lc.hardDelete.mockResolvedValue({ destroyed: false, approval_request: approval({ requested_by: OTHER }) });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'Permanently delete' }));
    await confirmDialog('Permanently delete', 'Request deletion');

    expect(await screen.findByRole('status')).toHaveTextContent(
      `Permanent deletion was already requested by ${OTHER} and is waiting for a second admin to approve it`,
    );
  });

  it('says a release waits for a second admin and shows when it expires', async () => {
    lc.release.mockResolvedValue({ released: false, approval_request: THEIRS });
    useChatStore.setState({ userProfile: { user_id: OTHER, email: 'o@example.com', display_name: 'O', tenant_id: 'tnt_x', roles: [] } });
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'Release' }));
    await confirmDialog('Release legal hold', 'Request release');

    await waitFor(() => expect(lc.release).toHaveBeenCalledWith('lhd_1', undefined));
    const notice = await screen.findByRole('status');
    expect(notice).toHaveTextContent('Legal hold release requested. It is waiting for a second admin to approve it.');
    expect(notice).toHaveTextContent(`The request expires at ${expiry}.`);
    expect(screen.queryByText('Released')).not.toBeInTheDocument();
  });

  it('shows a legal-hold refusal plainly', async () => {
    lc.hardDelete.mockRejectedValue(
      new ApiError(409, 'FOUNDATION.LIFECYCLE.STATE.LEGAL_HOLD_ACTIVE', 'upstream message'),
    );
    renderPage();

    await userEvent.click(await screen.findByRole('button', { name: 'Permanently delete' }));
    await confirmDialog('Permanently delete', 'Request deletion');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A legal hold covers this resource, so it can’t be permanently deleted.',
    );
  });
});

describe('LifecyclePage — approval requests', () => {
  beforeEach(() => {
    lc.approvalsList.mockResolvedValue({ items: [MINE, THEIRS], next_cursor: null });
  });

  it('lists pending requests and disables Approve on your own', async () => {
    renderPage();

    const mine = await approvalRow(ME);
    expect(within(mine).getByText('(you)')).toBeInTheDocument();
    expect(await within(mine).findByText('document · doc-1')).toBeInTheDocument();
    expect(within(mine).getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(within(mine).getByRole('button', { name: 'Reject' })).toBeEnabled();

    const theirs = await approvalRow(OTHER);
    expect(within(theirs).getByText('Legal hold release')).toBeInTheDocument();
    expect(within(theirs).getByText('mailbox · mbx-7')).toBeInTheDocument();
    expect(within(theirs).getByText(expiry)).toBeInTheDocument();
    expect(within(theirs).getByRole('button', { name: 'Approve' })).toBeEnabled();

    expect(lc.approvalsList).toHaveBeenCalledWith({ status: 'PENDING', cursor: undefined, limit: 50 }, undefined);
  });

  it('filters by status', async () => {
    renderPage();
    await approvalRow(ME);

    await userEvent.click(within(approvalsSection()).getByRole('button', { name: /^Pending/ }));
    await userEvent.click(within(approvalsSection()).getByRole('button', { name: 'Approved' }));
    await waitFor(() =>
      expect(lc.approvalsList).toHaveBeenLastCalledWith({ status: 'APPROVED', cursor: undefined, limit: 50 }, undefined),
    );

    await userEvent.click(within(approvalsSection()).getByRole('button', { name: /^Approved/ }));
    await userEvent.click(within(approvalsSection()).getByRole('button', { name: 'All' }));
    await waitFor(() =>
      expect(lc.approvalsList).toHaveBeenLastCalledWith({ status: undefined, cursor: undefined, limit: 50 }, undefined),
    );
  });

  it('approves another admin’s request and reloads', async () => {
    lc.approve.mockResolvedValue({ ...THEIRS, status: 'APPROVED', decided_by: ME, decided_at: '2026-10-30T10:05:00+00:00' });
    renderPage();

    await userEvent.click(within(await approvalRow(OTHER)).getByRole('button', { name: 'Approve' }));
    await confirmDialog('Approve legal hold release', 'Approve');

    await waitFor(() => expect(lc.approve).toHaveBeenCalledWith(THEIRS.id, undefined));
    expect(await screen.findByText('Approved: legal hold released')).toBeInTheDocument();
    await waitFor(() => expect(lc.approvalsList).toHaveBeenCalledTimes(2));
  });

  it.each([
    [403, 'POLICY.SELF_APPROVAL_FORBIDDEN', THEIRS, 'You opened this request, so another admin has to approve it.'],
    [409, 'STATE.APPROVAL_NOT_PENDING', THEIRS, 'This request was already decided or has expired.'],
    [409, 'STATE.LEGAL_HOLD_ACTIVE', THEIRS_DELETE, 'A legal hold covers this resource, so it can’t be permanently deleted. The request stays pending.'],
    [404, 'RESOURCE.RESOURCE_NOT_IN_BIN', THEIRS_DELETE, 'This item is no longer in the recycle bin: it was restored or already deleted. The request stays pending.'],
  ])('shows an approval refused with %i %s plainly', async (status, code, row, text) => {
    lc.approvalsList.mockResolvedValue({ items: [row], next_cursor: null });
    lc.approve.mockRejectedValue(new ApiError(status, `FOUNDATION.LIFECYCLE.${code}`, 'upstream message'));
    renderPage();

    await userEvent.click(within(await approvalRow(OTHER)).getByRole('button', { name: 'Approve' }));
    await confirmDialog(
      row.operation === 'HARD_DELETE' ? 'Approve permanent deletion' : 'Approve legal hold release',
      'Approve',
    );

    await waitFor(() => expect(lc.approve).toHaveBeenCalledWith(row.id, undefined));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.queryByText('upstream message')).not.toBeInTheDocument();
  });

  it('lets the requester withdraw their own request', async () => {
    lc.reject.mockResolvedValue({ ...MINE, status: 'REJECTED', decided_by: ME, decided_at: '2026-10-30T10:05:00+00:00' });
    renderPage();

    await userEvent.click(within(await approvalRow(ME)).getByRole('button', { name: 'Reject' }));
    await confirmDialog('Withdraw request', 'Reject');

    await waitFor(() => expect(lc.reject).toHaveBeenCalledWith(MINE.id, undefined));
    expect(await screen.findByText('Request rejected')).toBeInTheDocument();
  });

  it('keeps the page usable when the lifecycle service has no approval requests', async () => {
    lc.approvalsList.mockRejectedValue(new ApiError(404, 'UNKNOWN', 'Not Found'));
    renderPage();

    expect(await within(approvalsSection()).findByText(/this lifecycle service has no approval requests/)).toBeInTheDocument();
    expect(await screen.findByText('doc-1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Permanently delete' })).toBeInTheDocument();
  });
});
