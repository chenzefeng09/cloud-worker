import type {
  AdminUser,
  AuditEvent,
  AuditQueryRequest,
  Attachment,
  AttachmentKind,
  CreateTenantRequest,
  CursorPage,
  KnowledgeDocument,
  ListPage,
  LoginResponse,
  McpServer,
  Message,
  PatchTenantRequest,
  PoolSnapshot,
  ProbeReport,
  ProbeRequest,
  QuotaInfo,
  Run,
  RunState,
  SandboxItem,
  Session,
  Skill,
  InstalledSkill,
  Tenant,
  UserProfile,
} from './types';

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
    cancel: (sessionId: string) =>
      request<Record<string, unknown>>('POST', `/sessions/${sessionId}/cancel`),
  },

  messages: {
    list: (sessionId: string, cursor?: string) =>
      request<CursorPage<Message>>('GET', `/sessions/${sessionId}/messages${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
    deleteFrom: (sessionId: string, fromSequence: number) =>
      request<{ deleted_count: number }>('DELETE', `/sessions/${sessionId}/messages?from_sequence=${fromSequence}`),
  },

  runs: {
    list: (sessionId: string) =>
      request<{ items: Run[] }>('GET', `/sessions/${sessionId}/runs`),
    getState: (sessionId: string, runId: string) =>
      request<RunState>('GET', `/sessions/${sessionId}/runs/${runId}/state`),
  },

  attachments: {
    upload: async (sessionId: string, file: File): Promise<Attachment> => {
      const formData = new FormData();
      formData.append('file', file, file.name);
      const res = await fetch(`${BFF_BASE}/sessions/${sessionId}/attachments`, {
        method: 'POST',
        body: formData,
        credentials: 'include',
        // No Content-Type header — browser sets multipart boundary automatically
      });
      if (!res.ok) {
        const problem = await res.json().catch(() => ({ detail: res.statusText }));
        throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Upload failed');
      }
      return res.json();
    },
    list: (sessionId: string, kind?: AttachmentKind) => {
      const qs = kind ? `?kind=${encodeURIComponent(kind)}` : '';
      return request<{ items: Attachment[] }>('GET', `/sessions/${sessionId}/attachments${qs}`);
    },
    getContent: async (sessionId: string, fileId: string): Promise<{ text: string | null; blob: Blob; mime: string }> => {
      const res = await fetch(`${BFF_BASE}/sessions/${sessionId}/files/${fileId}/content`, {
        credentials: 'include',
      });
      if (!res.ok) {
        const problem = await res.json().catch(() => ({ detail: res.statusText }));
        throw new ApiError(res.status, problem.code || 'UNKNOWN', problem.detail || 'Fetch failed');
      }
      const mime = res.headers.get('content-type') || 'application/octet-stream';
      const blob = await res.blob();
      // For text-like content, also decode as string for preview
      const isText = mime.startsWith('text/') || mime.includes('json') || mime.includes('xml') || mime.includes('yaml') || mime.includes('markdown');
      let text: string | null = null;
      if (isText) {
        text = await blob.text();
      }
      return { text, blob, mime };
    },
  },

  library: {
    listMcpServers: () =>
      request<ListPage<McpServer>>('GET', '/library/mcp-servers'),
    listSkills: () =>
      request<ListPage<Skill>>('GET', '/library/skills'),
    listInstalledSkills: () =>
      request<ListPage<InstalledSkill>>('GET', '/library/skills/installed'),
    installSkill: (skillId: string, enabledByDefault = true) =>
      request<InstalledSkill>('POST', `/library/skills/${skillId}/install`, {
        enabled_by_default: enabledByDefault,
      }),
    uninstallSkill: (skillId: string) =>
      request<void>('DELETE', `/library/skills/${skillId}/install`),
    listDocuments: () =>
      request<ListPage<KnowledgeDocument>>('GET', '/library/documents'),
  },

  fleet: {
    poolSnapshot: () =>
      request<PoolSnapshot>('GET', '/fleet/pool'),
    listSandboxes: () =>
      request<{ items: SandboxItem[] }>('GET', '/fleet/sandboxes'),
  },

  admin: {
    tenants: {
      list: () =>
        request<{ items: Tenant[] }>('GET', '/admin/tenants'),
      create: (body: CreateTenantRequest) =>
        request<Tenant>('POST', '/admin/tenants', body),
      get: (tid: string) =>
        request<Tenant>('GET', `/admin/tenants/${tid}`),
      patch: (tid: string, body: PatchTenantRequest) =>
        request<Tenant>('PATCH', `/admin/tenants/${tid}`, body),
    },
    users: {
      list: (tenantId?: string) => {
        const qs = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : '';
        return request<{ items: AdminUser[] }>('GET', `/admin/users${qs}`);
      },
    },
    audit: {
      query: (body: AuditQueryRequest) =>
        request<{ items: AuditEvent[]; next_cursor?: string }>('POST', '/admin/audit/events:query', body),
    },
    billing: {
      quota: (tenantId: string) =>
        request<QuotaInfo>('GET', `/admin/billing/quota?tenant_id=${encodeURIComponent(tenantId)}`),
    },
    llm: {
      probe: (body: ProbeRequest) =>
        request<ProbeReport>('POST', '/admin/llm/probe', body),
    },
  },

  chat: {
    send: (sessionId: string, message: string, attachmentIds?: string[]): AbortController => {
      const controller = new AbortController();
      fetch(`${BFF_BASE}/sessions/${sessionId}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, attachment_ids: attachmentIds || [] }),
        credentials: 'include',
        signal: controller.signal,
      });
      return controller;
    },
  },
};

export { ApiError };
