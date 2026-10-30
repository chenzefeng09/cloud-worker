/**
 * api/privacy — request shapes against the BFF contract, download handling
 * (Content-Disposition, progress) and error details for partial erasure.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from './client';
import { onSessionExpired } from './authEvents';
import {
  ERASE_CONFIRMATION_PHRASE,
  errorDetails,
  filenameFromContentDisposition,
  privacyApi,
} from './privacy';
import { callsTo, jsonBody, jsonResponse, mockFetch, noContent } from '../components/privacy/testUtils';

const BASE = '/api/bff/v1';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('privacyApi — JSON endpoints', () => {
  it('GETs the public notice with cookies', async () => {
    const fetchMock = mockFetch({ [`GET ${BASE}/privacy/notice`]: { version: 'v1' } });
    await expect(privacyApi.notice()).resolves.toEqual({ version: 'v1' });
    const [[url, init]] = fetchMock.mock.calls;
    expect(url).toBe(`${BASE}/privacy/notice`);
    expect(init?.credentials).toBe('include');
  });

  it('records an acknowledgement for a notice version', async () => {
    const fetchMock = mockFetch({ [`POST ${BASE}/me/privacy/acknowledgements`]: () => noContent() });
    await expect(privacyApi.acknowledgeNotice('2026-10')).resolves.toBeUndefined();
    const [[, init]] = callsTo(fetchMock, 'POST', `${BASE}/me/privacy/acknowledgements`);
    expect(jsonBody(init)).toEqual({ notice_version: '2026-10' });
  });

  it('reads and updates privacy settings', async () => {
    const fetchMock = mockFetch({
      [`GET ${BASE}/me/privacy/settings`]: { memory_enabled: true, updated_at: null },
      [`PUT ${BASE}/me/privacy/settings`]: { memory_enabled: false, updated_at: '2026-10-30T00:00:00Z' },
    });
    await expect(privacyApi.settings()).resolves.toEqual({ memory_enabled: true, updated_at: null });
    await expect(privacyApi.updateSettings({ memory_enabled: false })).resolves.toMatchObject({ memory_enabled: false });
    const [[, init]] = callsTo(fetchMock, 'PUT', `${BASE}/me/privacy/settings`);
    expect(jsonBody(init)).toEqual({ memory_enabled: false });
  });

  it('lists memories, deletes one (id encoded) and deletes all', async () => {
    const fetchMock = mockFetch({
      [`GET ${BASE}/me/memories`]: { items: [{ id: 'm1', title: 't', content: 'c', origin_session_id: null, created_at: '' }] },
      [`DELETE ${BASE}/me/memories/a%2Fb`]: () => noContent(),
      [`DELETE ${BASE}/me/memories`]: { deleted: 3 },
    });
    await expect(privacyApi.memories()).resolves.toHaveLength(1);
    await privacyApi.deleteMemory('a/b');
    await expect(privacyApi.deleteAllMemories()).resolves.toEqual({ deleted: 3 });
    expect(callsTo(fetchMock, 'DELETE', `${BASE}/me/memories/a%2Fb`)).toHaveLength(1);
  });

  it('treats a memories response without items as empty', async () => {
    mockFetch({ [`GET ${BASE}/me/memories`]: {} });
    await expect(privacyApi.memories()).resolves.toEqual([]);
  });
});

describe('privacyApi — export (download)', () => {
  it('returns the blob with the server filename and reports progress', async () => {
    const payload = JSON.stringify({ profile: { id: 'usr_1' }, sessions: [] });
    mockFetch({
      [`GET ${BASE}/me/privacy/export`]: () =>
        new Response(payload, {
          status: 200,
          headers: {
            'Content-Type': 'application/json',
            'Content-Disposition': 'attachment; filename="privacy-export-usr_1.json"',
            'Content-Length': String(payload.length),
          },
        }),
    });
    const onProgress = vi.fn();
    const file = await privacyApi.exportMyData({ onProgress });
    expect(file.filename).toBe('privacy-export-usr_1.json');
    expect(file.size).toBe(payload.length);
    // jsdom's Blob has no .text(); FileReader is implemented.
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(file.blob);
    });
    expect(text).toBe(payload);
    expect(onProgress).toHaveBeenLastCalledWith(payload.length, payload.length);
  });

  it('falls back to a dated filename without Content-Disposition', async () => {
    mockFetch({ [`GET ${BASE}/me/privacy/export`]: () => new Response('{}', { status: 200 }) });
    const file = await privacyApi.exportMyData();
    expect(file.filename).toMatch(/^my-data-\d{4}-\d{2}-\d{2}\.json$/);
  });

  it('throws an ApiError with the server message on failure', async () => {
    mockFetch({
      [`GET ${BASE}/me/privacy/export`]: () =>
        jsonResponse({ detail: { code: 'EXPORT_FAILED', user_message: 'Export is temporarily unavailable' } }, { status: 503 }),
    });
    const err = await privacyApi.exportMyData().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ status: 503, code: 'EXPORT_FAILED', message: 'Export is temporarily unavailable' });
  });
});

describe('filenameFromContentDisposition', () => {
  it.each([
    ['attachment; filename="a.json"', 'a.json'],
    ['attachment; filename=plain.json', 'plain.json'],
    ['attachment; filename="we \\"quoted\\".json"', 'we "quoted".json'],
    ["attachment; filename=\"fallback.json\"; filename*=UTF-8''export%20%C3%A9.json", 'export é.json'],
    ['attachment; filename="../../etc/passwd"', 'passwd'],
    ['attachment', null],
    [null, null],
  ])('%s → %s', (header, expected) => {
    expect(filenameFromContentDisposition(header)).toBe(expected);
  });
});

describe('privacyApi — erasure', () => {
  const RESULT = {
    platform: { deleted: { sessions: 3, attachments: 2 }, runs_cancelled: 1 },
    memories_deleted: 4,
    bff: { api_keys: 1 },
    account: 'pending_admin_closure',
  };

  it('POSTs the confirmation phrase and returns the result', async () => {
    const fetchMock = mockFetch({ [`POST ${BASE}/me/privacy/erase`]: RESULT });
    await expect(privacyApi.eraseMyData(ERASE_CONFIRMATION_PHRASE)).resolves.toEqual(RESULT);
    const [[, init]] = callsTo(fetchMock, 'POST', `${BASE}/me/privacy/erase`);
    expect(jsonBody(init)).toEqual({ confirm: 'DELETE MY DATA' });
    expect(init?.credentials).toBe('include');
  });

  it('keeps the partial-step details of a 502', async () => {
    const partial = { message: 'Erasure incomplete', completed: ['platform'], failed: ['memories'] };
    mockFetch({ [`POST ${BASE}/me/privacy/erase`]: () => jsonResponse({ detail: partial }, { status: 502 }) });
    const err = await privacyApi.eraseMyData(ERASE_CONFIRMATION_PHRASE).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.message).toBe('Erasure incomplete');
    expect(errorDetails(err)).toEqual(partial);
  });

  it('surfaces a 422 for a wrong confirmation', async () => {
    mockFetch({
      [`POST ${BASE}/me/privacy/erase`]: () => jsonResponse({ detail: 'confirm must be "DELETE MY DATA"' }, { status: 422 }),
    });
    await expect(privacyApi.eraseMyData('nope')).rejects.toMatchObject({ status: 422 });
  });
});

describe('privacyApi — session expiry', () => {
  it('reports a 401 from an export or an erasure as a possible session expiry', async () => {
    const expired = vi.fn();
    const unsubscribe = onSessionExpired(expired);
    mockFetch({
      [`GET ${BASE}/me/privacy/export`]: () => jsonResponse({ detail: 'Session expired' }, { status: 401 }),
      [`POST ${BASE}/me/privacy/erase`]: () => jsonResponse({ detail: 'Session expired' }, { status: 401 }),
    });

    await expect(privacyApi.exportMyData()).rejects.toMatchObject({ status: 401 });
    await expect(privacyApi.eraseMyData(ERASE_CONFIRMATION_PHRASE)).rejects.toMatchObject({ status: 401 });

    expect(expired).toHaveBeenCalledTimes(2);
    unsubscribe();
  });

  it('does not report other failures', async () => {
    const expired = vi.fn();
    const unsubscribe = onSessionExpired(expired);
    mockFetch({ [`POST ${BASE}/me/privacy/erase`]: () => jsonResponse({ detail: 'nope' }, { status: 403 }) });

    await expect(privacyApi.eraseMyData(ERASE_CONFIRMATION_PHRASE)).rejects.toMatchObject({ status: 403 });

    expect(expired).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe('privacyApi.admin', () => {
  it('exports a user via POST, scoped to the selected tenant', async () => {
    const fetchMock = mockFetch({
      [`POST ${BASE}/admin/privacy/users/usr%2F42/export`]: () => new Response('{"user":"x"}', { status: 200 }),
    });
    const file = await privacyApi.admin.exportUser('usr/42', { tenantId: 'tnt_1' });
    const [[url, init]] = fetchMock.mock.calls;
    expect(url).toBe(`${BASE}/admin/privacy/users/usr%2F42/export?tenant_id=tnt_1`);
    expect(init?.method).toBe('POST');
    expect(file.filename).toMatch(/^user-42-data-\d{4}-\d{2}-\d{2}\.json$/);
  });

  it('erases a user with the user id as confirmation', async () => {
    const fetchMock = mockFetch({
      [`POST ${BASE}/admin/privacy/users/usr_42/erase`]: { platform: { deleted: {}, runs_cancelled: 0 }, memories_deleted: 0, bff: {}, account: 'x' },
    });
    await privacyApi.admin.eraseUser('usr_42');
    const [[url, init]] = fetchMock.mock.calls;
    expect(url).toBe(`${BASE}/admin/privacy/users/usr_42/erase`);
    expect(jsonBody(init)).toEqual({ confirm: 'usr_42' });
  });
});
