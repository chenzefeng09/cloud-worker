/**
 * GDPR / privacy endpoints (BFF `/api/bff/v1`, cookie auth).
 *
 * Self-service: privacy notice + acknowledgements, privacy settings (memory),
 * stored memories, data export (Art. 15/20) and erasure (Art. 17).
 * Admin: export / erase a given user's data to fulfil data subject requests.
 *
 * JSON calls go through the shared `apiRequest` helper. Downloads and erasure
 * use `fetch` directly: downloads need the raw body + Content-Disposition, and
 * erasure needs the full problem body on a 502 (it lists which steps finished
 * so the UI can explain a partial failure; retrying is safe).
 *
 * Nothing from `./client` is touched at module load, so test files that mock
 * `../api/client` with only `api` + `ApiError` can still import this module.
 */
import { apiQuery, apiRequest, ApiError, BFF_BASE } from './client';

// ─── Types ────────────────────────────────────────────────────────────────────

export interface PrivacySettings {
  memory_enabled: boolean;
  updated_at: string | null;
}

export interface MemoryItem {
  id: string;
  title: string;
  content: string;
  origin_session_id: string | null;
  created_at: string;
}

export interface PrivacyNoticePurpose {
  purpose: string;
  legal_basis: string;
}

export interface PrivacyNoticeProcessor {
  name: string;
  role: string;
  country: string;
  transfer_mechanism: string;
}

export interface PrivacyNotice {
  version: string;
  controller: { name: string; contact_email: string };
  dpo_email: string | null;
  purposes: PrivacyNoticePurpose[];
  retention: { session_retention_days: number | null; trash_grace_days: number };
  memory: { enabled: boolean };
  processors: PrivacyNoticeProcessor[];
  rights: string[];
}

export interface NoticeAcknowledgement {
  notice_version: string | null;
  acknowledged_at: string | null;
}

/** Result of a completed erasure (self-service or admin). */
/** A step this deployment can't perform comes back as `{ skipped: reason }`, never as done. */
export type ErasureStepOutcome = { deleted: number } | { skipped: string };

export interface ErasureResult {
  /** Triggers (schedules and webhooks) the user created — erased first. */
  schedules?: ErasureStepOutcome;
  platform: { deleted: Record<string, number>; runs_cancelled: number };
  memories_deleted: number;
  /** Model preferences held by the LLM gateway (skipped on an admin request). */
  llm_preferences?: ErasureStepOutcome;
  bff: Record<string, unknown>;
  account: string;
}

/** The exact phrase `POST /me/privacy/erase` requires in `confirm`. */
export const ERASE_CONFIRMATION_PHRASE = 'DELETE MY DATA';

export interface DownloadedFile {
  blob: Blob;
  filename: string;
  size: number;
}

export interface DownloadOptions {
  /** Called as bytes arrive; `totalBytes` is null when the server sends no length. */
  onProgress?: (receivedBytes: number, totalBytes: number | null) => void;
}

/** An ApiError that also carries the parsed problem body (e.g. partial-erasure steps). */
export type PrivacyApiError = ApiError & { details?: unknown };

/** The problem details attached to an error thrown by this module, if any. */
export function errorDetails(err: unknown): unknown {
  return err && typeof err === 'object' && 'details' in err ? (err as PrivacyApiError).details : undefined;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function findString(problem: unknown, key: string, depth = 0): string | undefined {
  if (problem == null || depth > 4 || typeof problem !== 'object') return undefined;
  const obj = problem as Record<string, unknown>;
  const v = obj[key];
  if (typeof v === 'string' && v) return v;
  return 'detail' in obj ? findString(obj.detail, key, depth + 1) : undefined;
}

// Mirrors client.ts: a localised `user_message` wins over a raw `detail` string.
function findMessage(problem: unknown, depth = 0): string {
  if (problem == null || depth > 4) return 'Request failed';
  if (typeof problem === 'string') return problem || 'Request failed';
  if (typeof problem !== 'object') return 'Request failed';
  const obj = problem as Record<string, unknown>;
  if (typeof obj.user_message === 'string' && obj.user_message) return obj.user_message;
  if ('detail' in obj && obj.detail != null) return findMessage(obj.detail, depth + 1);
  if (typeof obj.message === 'string' && obj.message) return obj.message;
  if (typeof obj.title === 'string' && obj.title) return obj.title;
  return 'Request failed';
}

// FastAPI nests the body under `detail` (and the BFF may nest an upstream
// problem one level deeper) — return the innermost object.
function innermostObject(problem: unknown): unknown {
  let cur = problem;
  for (let i = 0; i < 5; i++) {
    if (!cur || typeof cur !== 'object') break;
    const next = (cur as Record<string, unknown>).detail;
    if (!next || typeof next !== 'object') break;
    cur = next;
  }
  return cur;
}

async function errorFromResponse(res: Response): Promise<PrivacyApiError> {
  let problem: unknown;
  try {
    problem = await res.json();
  } catch {
    problem = { detail: res.statusText };
  }
  const err: PrivacyApiError = new ApiError(
    res.status,
    findString(problem, 'code') || 'UNKNOWN',
    findMessage(problem),
    findString(problem, 'reason'),
  );
  err.details = innermostObject(problem);
  return err;
}

/** JSON request that keeps the problem body on failure (see `errorDetails`). */
async function requestWithDetails<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BFF_BASE}${path}`, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include',
  });
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  return res.json();
}

function sanitizeFilename(name: string): string | null {
  const base = (name.split(/[\\/]/).pop() ?? '').trim();
  return base && base !== '.' && base !== '..' ? base : null;
}

/** Filename from a Content-Disposition header (RFC 6266; `filename*` preferred). */
export function filenameFromContentDisposition(header: string | null): string | null {
  if (!header) return null;
  const extended = /filename\*\s*=\s*([^']*)'[^']*'([^;]+)/i.exec(header);
  if (extended) {
    try {
      const decoded = sanitizeFilename(decodeURIComponent(extended[2].trim().replace(/^"|"$/g, '')));
      if (decoded) return decoded;
    } catch {
      // Malformed percent-encoding — fall back to the plain parameter.
    }
  }
  const plain = /filename\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;]+))/i.exec(header);
  if (!plain) return null;
  const raw = plain[1] !== undefined ? plain[1].replace(/\\(.)/g, '$1') : plain[2];
  return sanitizeFilename(raw);
}

async function readBlob(res: Response, onProgress?: DownloadOptions['onProgress']): Promise<Blob> {
  const type = res.headers.get('Content-Type') ?? 'application/json';
  const length = Number(res.headers.get('Content-Length'));
  const total = Number.isFinite(length) && length > 0 ? length : null;
  const reader = onProgress && res.body && typeof res.body.getReader === 'function' ? res.body.getReader() : null;
  if (!reader) return res.blob();
  const chunks: BlobPart[] = [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      chunks.push(value);
      received += value.byteLength;
      onProgress?.(received, total);
    }
  }
  return new Blob(chunks, { type });
}

async function download(
  method: 'GET' | 'POST',
  path: string,
  fallbackName: string,
  opts?: DownloadOptions,
): Promise<DownloadedFile> {
  const res = await fetch(`${BFF_BASE}${path}`, {
    method,
    headers: { Accept: 'application/json' },
    credentials: 'include',
  });
  if (!res.ok) throw await errorFromResponse(res);
  const blob = await readBlob(res, opts?.onProgress);
  return {
    blob,
    size: blob.size,
    filename: filenameFromContentDisposition(res.headers.get('Content-Disposition')) ?? fallbackName,
  };
}

// ─── Endpoints ────────────────────────────────────────────────────────────────

export const privacyApi = {
  /** Public privacy notice (Art. 13): controller, purposes, retention, processors, rights. */
  notice: () => apiRequest<PrivacyNotice>('GET', '/privacy/notice'),
  latestAcknowledgement: () =>
    apiRequest<NoticeAcknowledgement>('GET', '/me/privacy/acknowledgements/latest'),
  acknowledgeNotice: (noticeVersion: string) =>
    apiRequest<void>('POST', '/me/privacy/acknowledgements', { notice_version: noticeVersion }),

  settings: () => apiRequest<PrivacySettings>('GET', '/me/privacy/settings'),
  updateSettings: (body: { memory_enabled: boolean }) =>
    apiRequest<PrivacySettings>('PUT', '/me/privacy/settings', body),

  memories: () =>
    apiRequest<{ items?: MemoryItem[] }>('GET', '/me/memories').then((r) => r?.items ?? []),
  deleteMemory: (id: string) => apiRequest<void>('DELETE', `/me/memories/${encodeURIComponent(id)}`),
  deleteAllMemories: () => apiRequest<{ deleted: number }>('DELETE', '/me/memories'),

  /** Download everything we hold about the current user as JSON. */
  exportMyData: (opts?: DownloadOptions) =>
    download('GET', '/me/privacy/export', `my-data-${today()}.json`, opts),
  /**
   * Erase the current user's data. 422 when `confirm` is wrong; 502 when only
   * some steps finished — `errorDetails(err)` then describes them (safe to retry).
   */
  eraseMyData: (confirm: string) =>
    requestWithDetails<ErasureResult>('POST', '/me/privacy/erase', { confirm }),

  admin: {
    exportUser: (userId: string, opts?: DownloadOptions & { tenantId?: string }) =>
      download(
        'POST',
        `/admin/privacy/users/${encodeURIComponent(userId)}/export${apiQuery({ tenant_id: opts?.tenantId })}`,
        `user-${sanitizeFilename(userId) ?? 'unknown'}-data-${today()}.json`,
        opts,
      ),
    /** The typed confirmation for an admin erasure is the user id itself. */
    eraseUser: (userId: string, opts?: { tenantId?: string }) =>
      requestWithDetails<ErasureResult>(
        'POST',
        `/admin/privacy/users/${encodeURIComponent(userId)}/erase${apiQuery({ tenant_id: opts?.tenantId })}`,
        { confirm: userId },
      ),
  },
};
