/**
 * DataNoticeModal — the chat-page privacy notice gate.
 *
 * Content comes from GET /privacy/notice; acknowledgement is recorded with
 * POST /me/privacy/acknowledgements and checked against GET …/latest, so the
 * notice reappears when its version changes. localStorage is only a cache
 * (and, unlike the old modal, its absence no longer suppresses the notice).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { DataNoticeModal } from './DataNoticeModal';
import { FALLBACK_NOTICE_VERSION } from './privacy/useDataNotice';
import { useChatStore } from '../stores/chatStore';
import { callsTo, jsonBody, jsonResponse, memoryStorage, mockFetch, noContent } from './privacy/testUtils';
import type { PrivacyNotice } from '../api/privacy';

const BASE = '/api/bff/v1';
const NOTICE_URL = `${BASE}/privacy/notice`;
const LATEST_URL = `${BASE}/me/privacy/acknowledgements/latest`;
const ACK_URL = `${BASE}/me/privacy/acknowledgements`;
const CACHE_KEY = 'privacy-notice-ack:usr_1';

const NOTICE: PrivacyNotice = {
  version: '2026-10',
  controller: { name: 'Acme Robotics GmbH', contact_email: 'privacy@acme.example' },
  dpo_email: 'dpo@acme.example',
  purposes: [
    { purpose: 'Running the tasks you ask for', legal_basis: 'CONTRACT' },
    { purpose: 'Security auditing of conversations', legal_basis: 'LEGITIMATE_INTEREST' },
  ],
  retention: { session_retention_days: 90, trash_grace_days: 14 },
  memory: { enabled: true },
  processors: [{ name: 'Anthropic', role: 'AI model provider', country: 'US', transfer_mechanism: 'SCC' }],
  rights: ['access', 'erasure', 'portability'],
};

function renderGate() {
  return render(
    <MemoryRouter>
      <DataNoticeModal />
    </MemoryRouter>,
  );
}

let storage: Storage;

beforeEach(() => {
  storage = memoryStorage();
  vi.stubGlobal('localStorage', storage);
  useChatStore.setState({
    userProfile: { id: 'usr_1', email: 'a@b.c', display_name: 'A', tenant_id: 't', roles: [] } as never,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('DataNoticeModal', () => {
  it('shows the server notice when the user has not acknowledged it', async () => {
    mockFetch({ [`GET ${NOTICE_URL}`]: NOTICE, [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null } });
    renderGate();

    const dialog = await screen.findByRole('dialog', { name: 'Privacy notice' });
    const d = within(dialog);
    expect(d.getByText('Acme Robotics GmbH')).toBeInTheDocument();
    expect(d.getAllByRole('link', { name: 'privacy@acme.example' })[0]).toHaveAttribute('href', 'mailto:privacy@acme.example');
    expect(d.getByRole('link', { name: 'dpo@acme.example' })).toBeInTheDocument();
    expect(d.getByText(/legal basis: Performance of a contract/)).toBeInTheDocument();
    expect(d.getByText(/legal basis: Legitimate interests/)).toBeInTheDocument();
    // Retention comes from the server — no more hard-coded "30 days".
    expect(d.getByText(/kept for 90 days, then deleted automatically/)).toBeInTheDocument();
    expect(d.getByText(/stays in the trash for 14 days/)).toBeInTheDocument();
    expect(d.queryByText(/30 days/)).not.toBeInTheDocument();
    expect(d.getByText(/Memory is on/)).toBeInTheDocument();
    for (const link of d.getAllByRole('link', { name: 'Profile → Privacy & data' })) {
      expect(link).toHaveAttribute('href', '/profile?tab=privacy');
    }
    expect(d.getByText('Anthropic')).toBeInTheDocument();
    expect(d.getByText(/United States · Standard Contractual Clauses/)).toBeInTheDocument();
    expect(d.getByText('Have your data deleted')).toBeInTheDocument();
    expect(d.getByText(/Complain to a data protection supervisory authority/)).toBeInTheDocument();
    expect(d.getByText('Version 2026-10')).toBeInTheDocument();
  });

  it('records the acknowledgement server-side, caches it and closes', async () => {
    const fetchMock = mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null },
      [`POST ${ACK_URL}`]: () => noContent(),
    });
    renderGate();
    await userEvent.click(await screen.findByRole('button', { name: 'Got it' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const [[, init]] = callsTo(fetchMock, 'POST', ACK_URL);
    expect(jsonBody(init)).toEqual({ notice_version: '2026-10' });
    expect(JSON.parse(storage.getItem(CACHE_KEY)!)).toMatchObject({ version: '2026-10' });
  });

  it('stays hidden when the server already has this version acknowledged', async () => {
    const fetchMock = mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: '2026-10', acknowledged_at: '2026-10-01T00:00:00Z' },
    });
    renderGate();
    await waitFor(() => expect(callsTo(fetchMock, 'GET', LATEST_URL)).toHaveLength(1));
    await waitFor(() => expect(storage.getItem(CACHE_KEY)).not.toBeNull());
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('shows the notice again when its version changes', async () => {
    storage.setItem(CACHE_KEY, JSON.stringify({ version: '2026-09', acknowledged_at: '2026-09-01T00:00:00Z' }));
    mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: '2026-09', acknowledged_at: '2026-09-01T00:00:00Z' },
    });
    renderGate();
    expect(await screen.findByRole('dialog', { name: 'Privacy notice' })).toBeInTheDocument();
  });

  it('skips the server check when the cache already matches the current version', async () => {
    storage.setItem(CACHE_KEY, JSON.stringify({ version: '2026-10', acknowledged_at: '2026-10-02T00:00:00Z' }));
    const fetchMock = mockFetch({ [`GET ${NOTICE_URL}`]: NOTICE });
    renderGate();
    await waitFor(() => expect(callsTo(fetchMock, 'GET', NOTICE_URL)).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(callsTo(fetchMock, 'GET', LATEST_URL)).toHaveLength(0);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('still shows and records the notice when localStorage is unavailable', async () => {
    vi.stubGlobal('localStorage', undefined);
    const fetchMock = mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null },
      [`POST ${ACK_URL}`]: () => noContent(),
    });
    renderGate();
    await userEvent.click(await screen.findByRole('button', { name: 'Got it' }));
    await waitFor(() => expect(callsTo(fetchMock, 'POST', ACK_URL)).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('falls back to a built-in summary when the notice endpoint fails', async () => {
    const fetchMock = mockFetch({ [`GET ${NOTICE_URL}`]: () => jsonResponse({ detail: 'boom' }, { status: 500 }) });
    renderGate();

    const dialog = await screen.findByRole('dialog', { name: 'Privacy notice' });
    expect(within(dialog).getByText(/couldn't load the full privacy notice/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/supervisory authority/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/\d+ days/)).not.toBeInTheDocument();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Got it' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // No server version to record — only the local cache.
    expect(callsTo(fetchMock, 'POST', ACK_URL)).toHaveLength(0);
    expect(JSON.parse(storage.getItem(CACHE_KEY)!)).toMatchObject({ version: FALLBACK_NOTICE_VERSION });
  });

  it("doesn't repeat the fallback for someone who acknowledged a notice before", async () => {
    storage.setItem(CACHE_KEY, JSON.stringify({ version: '2026-10', acknowledged_at: null }));
    const fetchMock = mockFetch({ [`GET ${NOTICE_URL}`]: () => jsonResponse({}, { status: 503 }) });
    renderGate();
    await waitFor(() => expect(callsTo(fetchMock, 'GET', NOTICE_URL)).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps the notice open with a retry when saving the acknowledgement fails', async () => {
    let attempts = 0;
    mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null },
      [`POST ${ACK_URL}`]: () => (++attempts === 1 ? jsonResponse({ detail: 'down' }, { status: 503 }) : noContent()),
    });
    renderGate();
    await userEvent.click(await screen.findByRole('button', { name: 'Got it' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't save that/i);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(attempts).toBe(2);
  });

  it('Escape closes the notice without recording an acknowledgement', async () => {
    const fetchMock = mockFetch({
      [`GET ${NOTICE_URL}`]: NOTICE,
      [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null },
    });
    renderGate();
    await screen.findByRole('dialog', { name: 'Privacy notice' });
    await userEvent.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(callsTo(fetchMock, 'POST', ACK_URL)).toHaveLength(0);
    expect(storage.getItem(CACHE_KEY)).toBeNull();
  });

  it('renders "memory is off" when the platform has memory disabled', async () => {
    mockFetch({
      [`GET ${NOTICE_URL}`]: { ...NOTICE, memory: { enabled: false }, retention: { session_retention_days: null, trash_grace_days: 0 } },
      [`GET ${LATEST_URL}`]: { notice_version: null, acknowledged_at: null },
    });
    renderGate();
    const dialog = await screen.findByRole('dialog', { name: 'Privacy notice' });
    expect(within(dialog).getByText(/Memory is off/)).toBeInTheDocument();
    expect(within(dialog).getByText('Tasks and conversations are kept until you delete them.')).toBeInTheDocument();
    expect(within(dialog).getByText(/removed for good straight away/)).toBeInTheDocument();
  });
});
