/**
 * "Download your data" (GET /me/privacy/export) and "Delete your data"
 * (POST /me/privacy/erase with the typed phrase, incl. the 502 partial path).
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DataExportSection } from './DataExportSection';
import { DataErasureSection } from './DataErasureSection';
import { callsTo, jsonBody, jsonResponse, memoryStorage, mockFetch } from './testUtils';

const saveBlob = vi.fn();
vi.mock('./download', () => ({ saveBlob: (...args: unknown[]) => saveBlob(...args) }));

const BASE = '/api/bff/v1';
const ERASE_URL = `${BASE}/me/privacy/erase`;

const RESULT = {
  schedules: { deleted: 2 },
  platform: { deleted: { sessions: 3, messages: 41, attachments: 2, canvases: 1, workspaces: 1 }, runs_cancelled: 1 },
  memories_deleted: 4,
  llm_preferences: { deleted: 1 },
  bff: { api_keys: 2, preferences: 5 },
  account: 'pending_admin_closure',
};

afterEach(() => {
  vi.unstubAllGlobals();
  saveBlob.mockReset();
});

describe('DataExportSection', () => {
  it('downloads the export as a file and confirms it', async () => {
    const fetchMock = mockFetch({
      [`GET ${BASE}/me/privacy/export`]: () =>
        new Response('{"profile":{}}', {
          status: 200,
          headers: { 'Content-Disposition': 'attachment; filename="my-export.json"', 'Content-Type': 'application/json' },
        }),
    });
    render(<DataExportSection />);
    expect(screen.getByText(/tasks and\s+conversations/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Download my data' }));

    expect(await screen.findByText(/Saved my-export\.json/)).toBeInTheDocument();
    expect(saveBlob).toHaveBeenCalledTimes(1);
    expect(saveBlob.mock.calls[0][1]).toBe('my-export.json');
    expect(callsTo(fetchMock, 'GET', `${BASE}/me/privacy/export`)).toHaveLength(1);
  });

  it('shows an error and lets the user try again', async () => {
    mockFetch({ [`GET ${BASE}/me/privacy/export`]: () => jsonResponse({ detail: 'boom' }, { status: 500 }) });
    render(<DataExportSection />);
    await userEvent.click(screen.getByRole('button', { name: 'Download my data' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't prepare your download/i);
    expect(screen.getByRole('button', { name: 'Download my data' })).toBeEnabled();
    expect(saveBlob).not.toHaveBeenCalled();
  });
});

describe('DataErasureSection', () => {
  const confirmInput = () => screen.getByLabelText(/to confirm/);

  it('explains what is erased and requires the exact phrase', async () => {
    mockFetch({});
    render(<DataErasureSection onFinished={vi.fn()} />);
    expect(screen.getByText(/tasks and conversations, uploaded and generated files, canvases/)).toBeInTheDocument();
    expect(screen.getByText(/an administrator closes it separately/)).toBeInTheDocument();

    const button = screen.getByRole('button', { name: 'Delete my data' });
    expect(button).toBeDisabled();
    await userEvent.type(confirmInput(), 'delete my data');
    expect(button).toBeDisabled();
    await userEvent.clear(confirmInput());
    await userEvent.type(confirmInput(), 'DELETE MY DATA');
    expect(button).toBeEnabled();
  });

  it('erases, shows what was removed and continues on request', async () => {
    const onFinished = vi.fn();
    const fetchMock = mockFetch({ [`POST ${ERASE_URL}`]: RESULT });
    const storage = memoryStorage({ 'article-canvas-draft-v1:ses_1': '{"content":"draft"}' });
    vi.stubGlobal('localStorage', storage);
    render(<DataErasureSection onFinished={onFinished} />);

    await userEvent.type(confirmInput(), 'DELETE MY DATA');
    await userEvent.click(screen.getByRole('button', { name: 'Delete my data' }));

    expect(await screen.findByText('Your data has been deleted.')).toBeInTheDocument();
    const [[, init]] = callsTo(fetchMock, 'POST', ERASE_URL);
    expect(jsonBody(init)).toEqual({ confirm: 'DELETE MY DATA' });

    const row = (label: string) => screen.getByText(label).nextElementSibling;
    expect(row('Tasks and conversations')).toHaveTextContent('3');
    expect(row('Messages')).toHaveTextContent('41');
    expect(row('Running tasks stopped')).toHaveTextContent('1');
    expect(row('Memories')).toHaveTextContent('4');
    expect(row('Triggers')).toHaveTextContent('2');
    expect(row('Model preferences')).toHaveTextContent('1');
    expect(row('API keys')).toHaveTextContent('2');
    expect(screen.getByText('Pending admin closure')).toBeInTheDocument();
    expect(screen.queryByText('Skipped:')).not.toBeInTheDocument();
    expect(storage.getItem('article-canvas-draft-v1:ses_1')).toBeNull();

    await userEvent.click(screen.getByRole('button', { name: 'Continue' }));
    expect(onFinished).toHaveBeenCalledTimes(1);
  });

  it('explains a partial failure (502) and retries', async () => {
    let attempts = 0;
    mockFetch({
      [`POST ${ERASE_URL}`]: () =>
        ++attempts === 1
          ? jsonResponse(
              // The BFF's ErasureIncompleteError.detail().
              {
                detail: {
                  user_message: 'Your data could not be fully erased.',
                  code: 'PRIVACY_ERASURE_INCOMPLETE',
                  succeeded: ['platform'],
                  skipped: { schedules: 'not_configured' },
                  failed: 'memories',
                  not_attempted: ['llm_preferences', 'apps', 'bff'],
                  error: 'knowledge-ingest returned HTTP 503',
                  results: { schedules: { skipped: 'not_configured' }, platform: RESULT.platform },
                },
              },
              { status: 502 },
            )
          : jsonResponse(RESULT),
    });
    render(<DataErasureSection onFinished={vi.fn()} />);
    await userEvent.type(confirmInput(), 'DELETE MY DATA');
    await userEvent.click(screen.getByRole('button', { name: 'Delete my data' }));

    expect(await screen.findByText(/couldn't be deleted yet/)).toBeInTheDocument();
    expect(screen.getByText(/safe to try again/)).toBeInTheDocument();
    expect(screen.getByText('Tasks, files, canvases and workspaces')).toBeInTheDocument();
    expect(screen.getByText('Memories')).toBeInTheDocument();
    expect(screen.getByText('Model preferences, Apps, Preferences, API keys and sign-ins')).toBeInTheDocument();
    expect(screen.getByText('Triggers: not part of this deployment')).toBeInTheDocument();
    // The counts come from `results`, not the top level of the problem body.
    expect(screen.getByText('Already deleted:')).toBeInTheDocument();
    expect(screen.getByText('Messages').nextElementSibling).toHaveTextContent('41');

    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('Your data has been deleted.')).toBeInTheDocument();
    expect(attempts).toBe(2);
  });

  it('says plainly that nothing was deleted when the preflight refuses (503)', async () => {
    mockFetch({
      [`POST ${ERASE_URL}`]: () =>
        jsonResponse(
          {
            detail: {
              user_message: 'Nothing was erased: a platform service is unavailable or refused the request.',
              code: 'PRIVACY_ERASURE_UNAVAILABLE',
              failed: 'apps',
              error: 'app-registry unreachable (ConnectError)',
            },
          },
          { status: 503 },
        ),
    });
    render(<DataErasureSection onFinished={vi.fn()} />);
    await userEvent.type(confirmInput(), 'DELETE MY DATA');
    await userEvent.click(screen.getByRole('button', { name: 'Delete my data' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Nothing was deleted/);
    expect(screen.queryByText(/couldn't be deleted yet/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete my data' })).toBeEnabled());
  });

  it('reports other failures without claiming success', async () => {
    mockFetch({ [`POST ${ERASE_URL}`]: () => jsonResponse({ detail: 'nope' }, { status: 500 }) });
    render(<DataErasureSection onFinished={vi.fn()} />);
    await userEvent.type(confirmInput(), 'DELETE MY DATA');
    await userEvent.click(screen.getByRole('button', { name: 'Delete my data' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/couldn't delete your data/i);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Delete my data' })).toBeEnabled());
    expect(screen.queryByText('Your data has been deleted.')).not.toBeInTheDocument();
  });
});
