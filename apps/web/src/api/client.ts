import type { CursorPage, LoginResponse, Message, Session, UserProfile } from './types';

const BFF_BASE = '/api/bff/v1';

class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.status = status;
    this.code = code;
    this.name = 'ApiError';
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BFF_BASE}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'include',
  });
  if (!res.ok) {
    const problem = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Request failed');
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  auth: {
    login: (email: string, password: string) =>
      request<LoginResponse>('POST', '/auth/login', { email, password }),
    logout: () => request<void>('POST', '/auth/logout'),
    me: () => request<UserProfile>('GET', '/auth/me'),
  },

  sessions: {
    list: (cursor?: string) =>
      request<CursorPage<Session>>('GET', `/sessions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
    create: (title?: string) =>
      request<Session>('POST', '/sessions', { title }),
    get: (id: string) =>
      request<Session>('GET', `/sessions/${id}`),
    update: (id: string, data: { title?: string; status?: string }) =>
      request<Session>('PATCH', `/sessions/${id}`, data),
    delete: (id: string) =>
      request<void>('DELETE', `/sessions/${id}`),
  },

  messages: {
    list: (sessionId: string, cursor?: string) =>
      request<CursorPage<Message>>('GET', `/sessions/${sessionId}/messages${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  },

  chat: {
    send: (sessionId: string, message: string): AbortController => {
      const controller = new AbortController();
      fetch(`${BFF_BASE}/sessions/${sessionId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message }),
        credentials: 'include',
        signal: controller.signal,
      });
      return controller;
    },
  },
};

export { ApiError };
