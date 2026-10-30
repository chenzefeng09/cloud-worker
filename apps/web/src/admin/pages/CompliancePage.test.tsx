/**
 * CompliancePage — GDPR fixes and the data subject requests panel:
 *  - DSAR types are the upper-case foundation enum (all seven, human labels)
 *  - ROPA sends retention / cross-border / destination countries
 *  - DPIA sends the risks and mitigations entered (not empty arrays)
 *  - "Data subject requests": export / erase a user via the admin privacy API
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DialogProvider } from '../components/dialogs';
import { useAdminStore } from '../../stores/adminStore';
import { callsTo, jsonBody, jsonResponse, mockFetch } from '../../components/privacy/testUtils';

const compliance = vi.hoisted(() => ({
  ropaList: vi.fn(),
  ropaCreate: vi.fn(),
  reportsList: vi.fn(),
  reportsGenerate: vi.fn(),
  dsarCreate: vi.fn(),
  dsarGet: vi.fn(),
  dpiaCreate: vi.fn(),
  dpiaComplete: vi.fn(),
  breachesList: vi.fn(),
}));

// Keep the real request helpers (privacy.ts uses them with the stubbed fetch);
// replace only the `api` object the page calls directly.
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      admin: {
        tenants: { list: () => Promise.resolve({ items: [] }) },
        compliance: {
          ropa: { list: compliance.ropaList, create: compliance.ropaCreate },
          reports: { list: compliance.reportsList, generate: compliance.reportsGenerate },
          dsar: { create: compliance.dsarCreate, get: compliance.dsarGet },
          dpia: { create: compliance.dpiaCreate, complete: compliance.dpiaComplete },
          breaches: { list: compliance.breachesList },
        },
      },
    },
  };
});

const saveBlob = vi.fn();
vi.mock('../../components/privacy/download', () => ({ saveBlob: (...a: unknown[]) => saveBlob(...a) }));

import { CompliancePage } from './CompliancePage';

const BASE = '/api/bff/v1';

function renderPage() {
  return render(
    <DialogProvider>
      <CompliancePage />
    </DialogProvider>,
  );
}

beforeEach(() => {
  useAdminStore.setState({ tenants: [{ id: 'tnt_x', name: 'X' } as never], selectedTenantId: null });
  compliance.ropaList.mockResolvedValue([]);
  compliance.reportsList.mockResolvedValue([]);
  compliance.ropaCreate.mockResolvedValue({ id: 'ropa_1' });
  compliance.dsarCreate.mockImplementation((body: { request_type: string }) =>
    Promise.resolve({ id: 'dsar_1', user_id: 'usr_7', request_type: body.request_type, status: 'RECEIVED' }),
  );
  compliance.dpiaCreate.mockResolvedValue({ id: 'dpia_1' });
  compliance.breachesList.mockResolvedValue([]);
  mockFetch({});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('CompliancePage — DSAR types', () => {
  it('offers all seven GDPR request types and sends the upper-case value', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '+ New DSAR' }));
    const dialog = screen.getByRole('dialog', { name: 'New DSAR' });

    // Default is ACCESS, shown with its human label.
    await userEvent.click(within(dialog).getByRole('button', { name: /^Access — / }));
    for (const label of [
      /^Access — /, /^Rectification — /, /^Erasure — /, /^Restriction — /,
      /^Portability — /, /^Objection — /, /^Automated decisions — /,
    ]) {
      expect(within(dialog).getAllByRole('button', { name: label }).length).toBeGreaterThan(0);
    }
    await userEvent.click(within(dialog).getByRole('button', { name: /^Portability — / }));

    const [userInput, detailsInput] = within(dialog).getAllByRole('textbox');
    await userEvent.type(userInput, 'usr_7');
    await userEvent.type(detailsInput, 'Wants a copy');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() =>
      expect(compliance.dsarCreate).toHaveBeenCalledWith(
        { user_id: 'usr_7', request_type: 'PORTABILITY', details: 'Wants a copy' },
        undefined,
      ),
    );
    expect(await screen.findByText(/DSAR created: dsar_1 · Portability · status RECEIVED/)).toBeInTheDocument();
  });

  it('defaults to ACCESS', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '+ New DSAR' }));
    const dialog = screen.getByRole('dialog', { name: 'New DSAR' });
    await userEvent.type(within(dialog).getAllByRole('textbox')[0], 'usr_7');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(compliance.dsarCreate).toHaveBeenCalled());
    expect(compliance.dsarCreate.mock.calls[0][0].request_type).toBe('ACCESS');
  });
});

describe('CompliancePage — ROPA', () => {
  async function openRopa() {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '+ New' }));
    const dialog = screen.getByRole('dialog', { name: 'New ROPA' });
    await userEvent.type(within(dialog).getByPlaceholderText('billing'), 'agent-runtime');
    await userEvent.type(within(dialog).getByPlaceholderText('invoicing'), 'Run user tasks');
    return dialog;
  }

  it('sends retention, cross-border flag and upper-cased destination countries', async () => {
    const dialog = await openRopa();
    const retention = within(dialog).getByLabelText('Retention period (days)');
    expect(retention).toHaveValue(365);
    await userEvent.clear(retention);
    await userEvent.type(retention, '730');

    await userEvent.click(within(dialog).getByLabelText('Transferred outside the EU/EEA'));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/at least one destination country/);
    expect(compliance.ropaCreate).not.toHaveBeenCalled();

    await userEvent.type(within(dialog).getByLabelText(/Destination countries/), 'us, in');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(compliance.ropaCreate).toHaveBeenCalledTimes(1));
    expect(compliance.ropaCreate.mock.calls[0][0]).toMatchObject({
      service: 'agent-runtime',
      purpose: 'Run user tasks',
      legal_basis: 'CONSENT',
      retention_days: 730,
      cross_border: true,
      destination_countries: ['US', 'IN'],
    });
  });

  it('rejects a retention period that is not a positive whole number', async () => {
    const dialog = await openRopa();
    const retention = within(dialog).getByLabelText('Retention period (days)');
    await userEvent.clear(retention);
    await userEvent.type(retention, '0');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    // min=1: the browser's own validation stops the submit.
    expect(retention).toBeInvalid();
    expect(compliance.ropaCreate).not.toHaveBeenCalled();

    // Left blank, it gets our inline message.
    await userEvent.clear(retention);
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/whole number of days/);
    expect(compliance.ropaCreate).not.toHaveBeenCalled();
  });

  it('sends no destinations for a record that stays in the EU/EEA', async () => {
    const dialog = await openRopa();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(compliance.ropaCreate).toHaveBeenCalled());
    expect(compliance.ropaCreate.mock.calls[0][0]).toMatchObject({
      retention_days: 365,
      cross_border: false,
      destination_countries: [],
    });
  });
});

describe('CompliancePage — DPIA', () => {
  it('sends the risks and mitigations entered', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: '+ New DPIA' }));
    const dialog = screen.getByRole('dialog', { name: 'New DPIA' });
    await userEvent.type(within(dialog).getAllByRole('textbox')[0], 'Memory feature');

    await userEvent.click(within(dialog).getByRole('button', { name: '+ Add risk' }));
    await userEvent.type(within(dialog).getByLabelText('Risk 1'), 'Profiling from stored summaries');
    await userEvent.selectOptions(within(dialog).getByLabelText('Risk 1 severity'), 'high');
    await userEvent.click(within(dialog).getByRole('button', { name: '+ Add risk' }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Remove risk 2' }));

    await userEvent.click(within(dialog).getByRole('button', { name: '+ Add mitigation' }));
    await userEvent.type(within(dialog).getByLabelText('Mitigation 1'), 'Per-user opt-out');
    await userEvent.selectOptions(within(dialog).getByLabelText('Mitigation 1 status'), 'implemented');
    await userEvent.click(within(dialog).getByRole('button', { name: '+ Add mitigation' })); // left blank → dropped

    await userEvent.click(within(dialog).getByRole('button', { name: 'Create DPIA' }));

    await waitFor(() => expect(compliance.dpiaCreate).toHaveBeenCalledTimes(1));
    expect(compliance.dpiaCreate.mock.calls[0][0]).toMatchObject({
      project_name: 'Memory feature',
      risks: [{ risk: 'Profiling from stored summaries', likelihood: 'medium', severity: 'high' }],
      mitigations: [{ control: 'Per-user opt-out', status: 'implemented' }],
    });
    // The optional "complete now" prompt follows; dismiss it.
    const completePrompt = await screen.findByRole('dialog', { name: 'Complete DPIA (optional)' });
    await userEvent.click(within(completePrompt).getByRole('button', { name: 'Cancel' }));
  });
});

describe('CompliancePage — Data subject requests panel', () => {
  const ERASE_RESULT = {
    platform: { deleted: { sessions: 2, attachments: 1 }, runs_cancelled: 0 },
    memories_deleted: 3,
    bff: { api_keys: 1 },
    account: 'pending_admin_closure',
  };

  async function enterUser(id: string) {
    renderPage();
    const panel = screen.getByRole('region', { name: 'Data subject requests' });
    await userEvent.type(within(panel).getByLabelText('User ID'), id);
    return panel;
  }

  it('exports a user and downloads the file', async () => {
    const fetchMock = mockFetch({
      [`POST ${BASE}/admin/privacy/users/usr_42/export`]: () =>
        new Response('{"user":{}}', {
          status: 200,
          headers: { 'Content-Disposition': 'attachment; filename="usr_42-export.json"' },
        }),
    });
    const panel = await enterUser('usr_42');
    await userEvent.click(within(panel).getByRole('button', { name: 'Export data' }));

    expect(await within(panel).findByText('usr_42-export.json')).toBeInTheDocument();
    expect(saveBlob).toHaveBeenCalledWith(expect.anything(), 'usr_42-export.json');
    expect(callsTo(fetchMock, 'POST', `${BASE}/admin/privacy/users/usr_42/export`)).toHaveLength(1);
  });

  it('erases only after the user id is typed back, then shows the counts', async () => {
    const fetchMock = mockFetch({ [`POST ${BASE}/admin/privacy/users/usr_42/erase`]: ERASE_RESULT });
    const panel = await enterUser('usr_42');
    await userEvent.click(within(panel).getByRole('button', { name: 'Erase data…' }));

    const prompt = await screen.findByRole('dialog', { name: 'Erase user data' });
    expect(prompt).toHaveTextContent(/isn't closed by this/);
    await userEvent.type(within(prompt).getByRole('textbox'), 'usr_4');
    await userEvent.click(within(prompt).getByRole('button', { name: 'Erase data' }));
    expect(within(prompt).getByText("That doesn't match the user ID.")).toBeInTheDocument();
    expect(callsTo(fetchMock, 'POST', `${BASE}/admin/privacy/users/usr_42/erase`)).toHaveLength(0);

    await userEvent.type(within(prompt).getByRole('textbox'), '2');
    await userEvent.click(within(prompt).getByRole('button', { name: 'Erase data' }));

    expect(await within(panel).findByText(/Erased the data held for/)).toBeInTheDocument();
    const [[, init]] = callsTo(fetchMock, 'POST', `${BASE}/admin/privacy/users/usr_42/erase`);
    expect(jsonBody(init)).toEqual({ confirm: 'usr_42' });
    expect(within(panel).getByText('Memories').nextElementSibling).toHaveTextContent('3');
    expect(within(panel).getByText('Pending admin closure')).toBeInTheDocument();
  });

  it('explains a partial erasure and retries it without asking again', async () => {
    let attempts = 0;
    mockFetch({
      [`POST ${BASE}/admin/privacy/users/usr_42/erase`]: () =>
        ++attempts === 1
          ? jsonResponse({ detail: { steps: { platform: 'done', memories: 'failed' } } }, { status: 502 })
          : jsonResponse(ERASE_RESULT),
    });
    const panel = await enterUser('usr_42');
    await userEvent.click(within(panel).getByRole('button', { name: 'Erase data…' }));
    const prompt = await screen.findByRole('dialog', { name: 'Erase user data' });
    await userEvent.type(within(prompt).getByRole('textbox'), 'usr_42');
    await userEvent.click(within(prompt).getByRole('button', { name: 'Erase data' }));

    expect(await within(panel).findByText(/only partly finished/)).toBeInTheDocument();
    expect(within(panel).getByText('Failed')).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole('button', { name: 'Retry erasure' }));
    expect(await within(panel).findByText(/Erased the data held for/)).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('reports an unknown user', async () => {
    mockFetch({
      [`POST ${BASE}/admin/privacy/users/usr_x/export`]: () => jsonResponse({ detail: 'User not found' }, { status: 404 }),
    });
    const panel = await enterUser('usr_x');
    await userEvent.click(within(panel).getByRole('button', { name: 'Export data' }));
    expect(await within(panel).findByRole('alert')).toHaveTextContent('No user found with ID usr_x.');
    expect(saveBlob).not.toHaveBeenCalled();
  });
});
