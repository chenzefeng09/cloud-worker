/**
 * api/client — audited admin reads.
 *
 * The BFF writes an audit record for every read of another member's sessions,
 * so the same read asked for twice at once (React StrictMode re-running a
 * mount effect) must go out once.  Each caller still gets its own result.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './client';

function page(items: unknown[]): Response {
  return new Response(JSON.stringify({ items, next_cursor: null }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('audited admin reads', () => {
  it('sends one request for the same read asked for twice at once', async () => {
    const fetchMock = vi.fn(async () => page([{ id: 'ses_1' }, { id: 'ses_2' }]));
    vi.stubGlobal('fetch', fetchMock);

    const [first, second] = await Promise.all([
      api.admin.sessions.list(),
      api.admin.sessions.list(),
    ]);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(first.items.map((s) => s.id)).toEqual(['ses_1', 'ses_2']);
    // One caller reordering its copy leaves the other's alone.
    first.items.reverse();
    expect(second.items.map((s) => s.id)).toEqual(['ses_1', 'ses_2']);
  });

  it('asks again once the earlier read has finished', async () => {
    const fetchMock = vi.fn(async () => page([]));
    vi.stubGlobal('fetch', fetchMock);

    await api.admin.sessions.runs('ses_1');
    await api.admin.sessions.runs('ses_1');

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('keeps different reads apart and lets a failed one be retried', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('{}', { status: 500 }))
      .mockImplementation(async () => page([]));
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.admin.sessions.messages('ses_1')).rejects.toThrow();
    await Promise.all([api.admin.sessions.messages('ses_1'), api.admin.sessions.messages('ses_2')]);

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
