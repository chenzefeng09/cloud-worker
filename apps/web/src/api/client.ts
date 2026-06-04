import type {
  ActiveSession,
  AdminUser,
  ApiKey,
  ApiKeyCreated,
  AuditEvent,
  AuditQueryRequest,
  Attachment,
  AttachmentKind,
  CreateTenantRequest,
  CursorPage,
  DailyTokenUsageResponse,
  IngestResult,
  KnowledgeDocument,
  ListPage,
  LoginResponse,
  McpServer,
  Message,
  MfaMethod,
  NotifPreferenceItem,
  PatchTenantRequest,
  PoolSnapshot,
  QuotaInfo,
  Run,
  SandboxItem,
  Session,
  Skill,
  SkillVersion,
  Tenant,
  TokenUsage,
  TopologyGraph,
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
    const rawDetail = problem.detail || 'Request failed';
    const detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail);
    throw new ApiError(res.status, problem.code || 'UNKNOWN', detail);
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
    mfaMethods: () => request<MfaMethod[]>('GET', '/auth/mfa/methods'),
    sessions: () => request<ActiveSession[]>('GET', '/auth/sessions'),
  },

  runs: {
    get: (sessionId: string, runId: string) =>
      request<Run>('GET', `/sessions/${sessionId}/runs/${runId}`),
    list: (sessionId: string) =>
      request<{ items: Run[] }>('GET', `/sessions/${sessionId}/runs`),
    count: () =>
      request<{ count: number }>('GET', '/runs/count'),
    tokenUsage: () =>
      request<TokenUsage>('GET', '/runs/token-usage'),
    tokenUsageDaily: (days = 7) =>
      request<DailyTokenUsageResponse>('GET', `/runs/token-usage/daily?days=${days}`),
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
    listDocuments: () =>
      request<ListPage<KnowledgeDocument>>('GET', '/library/documents'),

    installMcpServer: (body: {
      name: string;
      transport: McpServer['transport'];
      endpoint?: string | null;
      command?: string | null;
      enabled?: boolean;
    }) => request<McpServer>('POST', '/library/mcp-servers', body),

    createSkill: (body: {
      name: string;
      description?: string | null;
      owner?: string | null;
      visibility?: Skill['visibility'];
    }) => request<Skill>('POST', '/library/skills', body),

    uploadSkillVersion: async (skillId: string, formData: FormData): Promise<SkillVersion> => {
      const res = await fetch(`${BFF_BASE}/library/skills/${skillId}/versions`, {
        method: 'POST',
        body: formData,
        credentials: 'include',
        // No Content-Type header — browser sets multipart boundary automatically
      });
      if (!res.ok) {
        const problem = await res.json().catch(() => ({ detail: res.statusText }));
        const rawDetail = problem.detail || 'Upload failed';
        const detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail);
        throw new ApiError(res.status, problem.code || 'UNKNOWN', detail);
      }
      return res.json();
    },

    uploadDocument: async (formData: FormData): Promise<KnowledgeDocument> => {
      const res = await fetch(`${BFF_BASE}/library/documents`, {
        method: 'POST',
        body: formData,
        credentials: 'include',
        // No Content-Type header — browser sets multipart boundary automatically
      });
      if (!res.ok) {
        const problem = await res.json().catch(() => ({ detail: res.statusText }));
        const rawDetail = problem.detail || 'Upload failed';
        const detail = typeof rawDetail === 'string' ? rawDetail : JSON.stringify(rawDetail);
        throw new ApiError(res.status, problem.code || 'UNKNOWN', detail);
      }
      return res.json();
    },

    ingestDocument: (docId: string) =>
      request<IngestResult>('POST', `/library/documents/${docId}/ingest`),
  },

  billing: {
    me: () => request<QuotaInfo>('GET', '/billing/me'),
  },

  apiKeys: {
    list: () => request<ApiKey[]>('GET', '/api-keys'),
    create: (name: string) => request<ApiKeyCreated>('POST', '/api-keys', { name }),
    revoke: (id: string) => request<void>('DELETE', `/api-keys/${id}`),
  },

  fleet: {
    poolSnapshot: () =>
      request<PoolSnapshot>('GET', '/fleet/pool'),
    listSandboxes: () =>
      request<{ items: SandboxItem[] }>('GET', '/fleet/sandboxes'),
    topology: () =>
      request<TopologyGraph>('GET', '/fleet/topology'),
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
  },

  notifications: {
    preferences: () =>
      request<{ items: NotifPreferenceItem[] }>('GET', '/notifications/preferences'),
    updatePreferences: (items: { event_key: string; email_enabled: boolean; push_enabled: boolean }[]) =>
      request<{ items: NotifPreferenceItem[] }>('PUT', '/notifications/preferences', { items }),
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
