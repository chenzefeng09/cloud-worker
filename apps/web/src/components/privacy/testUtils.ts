/**
 * Test-only helpers for the privacy screens (imported by *.test.tsx files,
 * never by app code): a routed `fetch` stub and an in-memory Storage.
 *
 * jsdom under this Node version exposes no usable `localStorage`, so tests
 * that care about the acknowledgement cache stub one in explicitly.
 */
import { vi } from 'vitest';

export type RouteHandler = (init: RequestInit | undefined, url: string) => Response | Promise<Response>;

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  return new Response(JSON.stringify(body), { status: 200, ...init, headers });
}

export function noContent(): Response {
  return new Response(null, { status: 204 });
}

/**
 * Stub global fetch with `"METHOD /path"` routes (query string optional in the
 * key). A route value is either a handler or a JSON body for a 200 response.
 * Unmatched requests get a 404 problem so a missing mock is loud in assertions.
 */
export function mockFetch(routes: Record<string, RouteHandler | object>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const method = (init?.method ?? 'GET').toUpperCase();
    const route = routes[`${method} ${url}`] ?? routes[`${method} ${url.split('?')[0]}`];
    if (route === undefined) return jsonResponse({ detail: `no mock for ${method} ${url}` }, { status: 404 });
    return typeof route === 'function' ? (route as RouteHandler)(init, url) : jsonResponse(route);
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

/** Calls the stub received for one route, as [url, init] pairs. */
export function callsTo(fn: ReturnType<typeof mockFetch>, method: string, path: string) {
  return fn.mock.calls.filter(([input, init]) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    return (init?.method ?? 'GET').toUpperCase() === method && url.split('?')[0] === path;
  });
}

export function jsonBody(init: RequestInit | undefined): unknown {
  return typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
}

export function memoryStorage(seed: Record<string, string> = {}): Storage {
  const data = new Map(Object.entries(seed));
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => data.get(k) ?? null,
    key: (i: number) => Array.from(data.keys())[i] ?? null,
    removeItem: (k: string) => {
      data.delete(k);
    },
    setItem: (k: string, v: string) => {
      data.set(k, String(v));
    },
  };
}
