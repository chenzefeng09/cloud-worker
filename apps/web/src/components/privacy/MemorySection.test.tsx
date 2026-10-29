/**
 * MemorySection — the memory toggle (GET/PUT /me/privacy/settings) and the
 * saved-memories list (GET /me/memories, DELETE one, DELETE all with confirm).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { DialogProvider } from '../../admin/components/dialogs';
import { MemorySection } from './MemorySection';
import { callsTo, jsonBody, jsonResponse, mockFetch, noContent, type RouteHandler } from './testUtils';

const BASE = '/api/bff/v1';

const MEMORIES = [
  {
    id: 'mem_1',
    title: 'Quarterly report format',
    content: 'User prefers tables with totals in the last row.',
    origin_session_id: 'ses_9',
    created_at: new Date(Date.now() - 2 * 86_400_000).toISOString(),
  },
  { id: 'mem_2', title: 'Tone', content: 'Keep answers short.', origin_session_id: null, created_at: new Date().toISOString() },
];

function setup(overrides: Record<string, RouteHandler | object> = {}, props: { platformMemoryEnabled?: boolean } = {}) {
  const fetchMock = mockFetch({
    [`GET ${BASE}/me/privacy/settings`]: { memory_enabled: true, updated_at: null },
    [`GET ${BASE}/me/memories`]: { items: MEMORIES },
    ...overrides,
  });
  render(
    <DialogProvider>
      <MemoryRouter>
        <MemorySection {...props} />
      </MemoryRouter>
    </DialogProvider>,
  );
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('MemorySection', () => {
  it('explains memory, shows the setting and lists saved memories', async () => {
    setup();
    expect(screen.getByText(/short summary of each task you complete/)).toBeInTheDocument();
    const toggle = await screen.findByRole('switch', { name: 'Use memory' });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).toHaveAttribute('aria-checked', 'true');

    const list = await screen.findByRole('list', { name: 'Saved memories' });
    expect(within(list).getByText('Quarterly report format')).toBeInTheDocument();
    expect(within(list).getByText('User prefers tables with totals in the last row.')).toBeInTheDocument();
    expect(within(list).getByRole('link', { name: 'From this task' })).toHaveAttribute('href', '/tasks/ses_9');
    expect(screen.getByText('Saved memories (2)')).toBeInTheDocument();
  });

  it('turns memory off with PUT /me/privacy/settings', async () => {
    const fetchMock = setup({
      [`PUT ${BASE}/me/privacy/settings`]: { memory_enabled: false, updated_at: new Date().toISOString() },
    });
    const toggle = await screen.findByRole('switch', { name: 'Use memory' });
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);

    await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
    const [[, init]] = callsTo(fetchMock, 'PUT', `${BASE}/me/privacy/settings`);
    expect(jsonBody(init)).toEqual({ memory_enabled: false });
    expect(screen.getByText(/nothing new is saved/)).toBeInTheDocument();
  });

  it('keeps the old value and explains when saving the setting fails', async () => {
    setup({ [`PUT ${BASE}/me/privacy/settings`]: () => jsonResponse({ detail: 'nope' }, { status: 500 }) });
    const toggle = await screen.findByRole('switch', { name: 'Use memory' });
    await waitFor(() => expect(toggle).toBeEnabled());
    await userEvent.click(toggle);

    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't save your memory setting/i);
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  it('deletes a single memory', async () => {
    const fetchMock = setup({ [`DELETE ${BASE}/me/memories/mem_1`]: () => noContent() });
    await userEvent.click(await screen.findByRole('button', { name: 'Delete memory: Quarterly report format' }));

    await waitFor(() => expect(screen.queryByText('Quarterly report format')).not.toBeInTheDocument());
    expect(callsTo(fetchMock, 'DELETE', `${BASE}/me/memories/mem_1`)).toHaveLength(1);
    expect(screen.getByText('Tone')).toBeInTheDocument();
    expect(screen.getByText('Memory deleted.')).toBeInTheDocument();
  });

  it('asks before deleting all memories, and does nothing on cancel', async () => {
    const fetchMock = setup({ [`DELETE ${BASE}/me/memories`]: { deleted: 2 } });
    await screen.findByRole('list', { name: 'Saved memories' });

    await userEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    let confirmDialog = await screen.findByRole('dialog', { name: 'Delete all memories' });
    expect(confirmDialog).toHaveTextContent(/all 2 saved memories/);
    await userEvent.click(within(confirmDialog).getByRole('button', { name: 'Cancel' }));
    expect(callsTo(fetchMock, 'DELETE', `${BASE}/me/memories`)).toHaveLength(0);

    await userEvent.click(screen.getByRole('button', { name: 'Delete all' }));
    confirmDialog = await screen.findByRole('dialog', { name: 'Delete all memories' });
    await userEvent.click(within(confirmDialog).getByRole('button', { name: 'Delete all' }));

    expect(await screen.findByText('No memories saved yet.')).toBeInTheDocument();
    expect(screen.getByText('Deleted 2 memories.')).toBeInTheDocument();
    expect(callsTo(fetchMock, 'DELETE', `${BASE}/me/memories`)).toHaveLength(1);
  });

  it('disables the toggle when memory is off for the whole workspace', async () => {
    setup({}, { platformMemoryEnabled: false });
    const toggle = await screen.findByRole('switch', { name: 'Use memory' });
    expect(await screen.findByText(/switched off for this workspace/)).toBeInTheDocument();
    expect(toggle).toBeDisabled();
  });

  it('offers a retry when memories fail to load', async () => {
    let calls = 0;
    setup({
      [`GET ${BASE}/me/memories`]: () =>
        ++calls === 1 ? jsonResponse({ detail: 'down' }, { status: 503 }) : jsonResponse({ items: [] }),
    });
    expect(await screen.findByText(/couldn't load your memories/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('No memories saved yet.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete all' })).toBeDisabled();
  });
});
