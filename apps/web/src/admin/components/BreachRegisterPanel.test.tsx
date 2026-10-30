/**
 * BreachRegisterPanel — the GDPR Art. 33(5) breach register on the Compliance page:
 *  - each row shows the live Art. 33(1) 72-hour clock (due / overdue / notified / not required)
 *  - recording a breach sends the form as ISO timestamps for the selected tenant
 *  - a late or missing notification asks for the reasons before saving
 *  - editing sends only the fields that changed
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { BreachRecord } from '../../api/types';
import { DialogProvider } from './dialogs';

const breaches = vi.hoisted(() => ({ list: vi.fn(), create: vi.fn(), update: vi.fn() }));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return { ...actual, api: { admin: { compliance: { breaches } } } };
});

import { ApiError } from '../../api/client';
import { BreachRegisterPanel } from './BreachRegisterPanel';

const HOUR = 60 * 60 * 1000;

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

/** The value a datetime-local input holds for a moment `ms` ago (local time). */
function localAgo(ms: number): string {
  const d = new Date(Date.now() - ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function breach(over: Partial<BreachRecord> = {}): BreachRecord {
  const aware = over.aware_at ?? ago(10 * HOUR);
  return {
    id: 'brc_1',
    tenant_id: 'tnt_x',
    title: 'Misdirected email',
    description: '',
    occurred_at: null,
    aware_at: aware,
    status: 'open',
    risk_level: 'medium',
    data_categories: [],
    approx_subjects: null,
    approx_records: null,
    likely_consequences: '',
    measures_taken: '',
    authority_notified_at: null,
    authority_reference: null,
    subjects_notified_at: null,
    delay_reason: null,
    security_incident_id: null,
    notify_authority_by: new Date(Date.parse(aware) + 72 * HOUR).toISOString(),
    created_by: 'usr_admin',
    created_at: aware,
    updated_at: aware,
    ...over,
  };
}

function renderPanel() {
  return render(
    <DialogProvider>
      <BreachRegisterPanel tenantId="tnt_x" />
    </DialogProvider>,
  );
}

const rowOf = (title: string) => screen.getByText(title).closest('tr') as HTMLElement;

async function openCreateDialog() {
  const user = userEvent.setup();
  await screen.findByText('No breaches recorded');
  await user.click(screen.getByRole('button', { name: '+ Record breach' }));
  return { user, dialog: screen.getByRole('dialog', { name: 'Record personal data breach' }) };
}

beforeEach(() => {
  breaches.list.mockResolvedValue([]);
  breaches.create.mockImplementation((body: Partial<BreachRecord>) => Promise.resolve(breach(body)));
  breaches.update.mockImplementation((id: string, body: Partial<BreachRecord>) =>
    Promise.resolve(breach({ id, ...body })),
  );
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('BreachRegisterPanel — the 72-hour clock', () => {
  it('shows each breach as due, overdue, notified or not requiring notification', async () => {
    breaches.list.mockResolvedValue([
      breach({ id: 'b1', title: 'Due soon', aware_at: ago(10 * HOUR) }),
      breach({ id: 'b2', title: 'Overdue', aware_at: ago(80 * HOUR), risk_level: 'high' }),
      breach({ id: 'b3', title: 'Notified', authority_notified_at: ago(HOUR) }),
      breach({ id: 'b4', title: 'Low risk', risk_level: 'low' }),
    ]);
    renderPanel();

    await screen.findByText('Due soon');
    expect(breaches.list).toHaveBeenCalledWith('tnt_x');
    expect(within(rowOf('Due soon')).getByText(/^Notify by .+ · 2 d 14 h left$/)).toBeInTheDocument();
    expect(within(rowOf('Overdue')).getByText('Overdue by 8 h — notify now')).toBeInTheDocument();
    expect(within(rowOf('Overdue')).getByText('Tell the affected individuals (Art. 34)')).toBeInTheDocument();
    expect(within(rowOf('Notified')).getByText(/^Notified /)).toBeInTheDocument();
    expect(within(rowOf('Low risk')).getByText('Not required — low risk')).toBeInTheDocument();
  });
});

describe('BreachRegisterPanel — recording a breach', () => {
  it('sends the form with ISO timestamps for the selected tenant and reloads', async () => {
    renderPanel();
    const { user, dialog } = await openCreateDialog();
    const aware = localAgo(2 * HOUR);

    await user.type(within(dialog).getByLabelText('Title'), 'Laptop stolen');
    fireEvent.change(within(dialog).getByLabelText('Became aware at'), { target: { value: aware } });
    await user.selectOptions(within(dialog).getByLabelText('Risk to individuals'), 'high');
    await user.type(within(dialog).getByLabelText('Data categories (comma-separated)'), 'contact, health');
    await user.type(within(dialog).getByLabelText('Individuals affected (approx.)'), '12');
    await user.click(within(dialog).getByRole('button', { name: 'Record breach' }));

    await waitFor(() => expect(breaches.create).toHaveBeenCalledTimes(1));
    const [body, tenantId] = breaches.create.mock.calls[0];
    expect(tenantId).toBe('tnt_x');
    expect(body).toMatchObject({
      title: 'Laptop stolen',
      aware_at: new Date(aware).toISOString(),
      occurred_at: null,
      status: 'open',
      risk_level: 'high',
      data_categories: ['contact', 'health'],
      approx_subjects: 12,
      approx_records: null,
      authority_notified_at: null,
      delay_reason: null,
    });
    await waitFor(() => expect(breaches.list).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('asks for the reasons when the authority was notified after 72 hours', async () => {
    renderPanel();
    const { user, dialog } = await openCreateDialog();

    await user.type(within(dialog).getByLabelText('Title'), 'Late report');
    fireEvent.change(within(dialog).getByLabelText('Became aware at'), {
      target: { value: localAgo(5 * 24 * HOUR) },
    });
    fireEvent.change(within(dialog).getByLabelText('Authority notified at'), {
      target: { value: localAgo(24 * HOUR) },
    });
    const reasons = within(dialog).getByLabelText('Reasons for the late or missing notification (required)');
    await user.click(within(dialog).getByRole('button', { name: 'Record breach' }));

    expect(within(dialog).getByRole('alert')).toHaveTextContent(/reasons for the late or missing notification/i);
    expect(breaches.create).not.toHaveBeenCalled();

    await user.type(reasons, 'Scope only known after the forensic review');
    await user.click(within(dialog).getByRole('button', { name: 'Record breach' }));
    await waitFor(() => expect(breaches.create).toHaveBeenCalledTimes(1));
    expect(breaches.create.mock.calls[0][0].delay_reason).toBe('Scope only known after the forensic review');
  });

  it('keeps the dialog open with the server message when saving fails', async () => {
    breaches.create.mockRejectedValue(
      new ApiError(422, 'VALIDATION', "The breach can't have happened after you became aware of it."),
    );
    renderPanel();
    const { user, dialog } = await openCreateDialog();

    await user.type(within(dialog).getByLabelText('Title'), 'Wrong order');
    await user.click(within(dialog).getByRole('button', { name: 'Record breach' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      "The breach can't have happened after you became aware of it.",
    );
    expect(screen.getByRole('dialog', { name: 'Record personal data breach' })).toBeInTheDocument();
  });
});

describe('BreachRegisterPanel — editing', () => {
  it('sends only the fields that changed', async () => {
    breaches.list.mockResolvedValue([
      breach({ id: 'brc_9', title: 'Misdirected email', aware_at: '2026-10-29T08:30:27.123Z' }),
    ]);
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit Misdirected email' }));
    const dialog = screen.getByRole('dialog', { name: 'Edit breach record' });

    await user.selectOptions(within(dialog).getByLabelText('Status'), 'contained');
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(breaches.update).toHaveBeenCalledTimes(1));
    expect(breaches.update).toHaveBeenCalledWith('brc_9', { status: 'contained' }, 'tnt_x');
  });

  it('asks why a breach is closed without notification unless the risk is low', async () => {
    breaches.list.mockResolvedValue([breach({ id: 'brc_7', title: 'Open breach' })]);
    renderPanel();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Edit Open breach' }));
    const dialog = screen.getByRole('dialog', { name: 'Edit breach record' });

    await user.selectOptions(within(dialog).getByLabelText('Status'), 'closed');
    expect(
      within(dialog).getByLabelText('Reasons for the late or missing notification (required)'),
    ).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText('Risk to individuals'), 'low');
    expect(within(dialog).queryByLabelText(/Reasons for the late or missing notification/)).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await waitFor(() =>
      expect(breaches.update).toHaveBeenCalledWith('brc_7', { status: 'closed', risk_level: 'low' }, 'tnt_x'),
    );
  });
});
