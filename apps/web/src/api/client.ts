import type {
  ActiveSession,
  AddTeamMemberRequest,
  AdminRole,
  AdminSkill,
  AdminSkillVersion,
  AdminUser,
  ApiKey,
  ApiKeyCreated,
  App,
  AppCreate,
  AppReviewItem,
  AppRunResult,
  AppUpdate,
  AppVersion,
  AppVersionCreate,
  AuditChainVerifyRequest,
  AuditChainVerifyResult,
  AuditEvent,
  AuditExport,
  AuditExportRequest,
  AuditQueryRequest,
  Attachment,
  AttachmentKind,
  BillingPlanDef,
  CancelSubscriptionRequest,
  ChangePlanRequest,
  CreateBindingRequest,
  CreateOrgUnitRequest,
  CreateRoleRequest,
  CreateTeamRequest,
  CreateTenantRequest,
  CursorPage,
  DailyTokenUsageResponse,
  FeatureFlag,
  FeatureFlagUpdate,
  IngestResult,
  InviteUserRequest,
  KillSwitchRequest,
  KnowledgeDocument,
  ListPage,
  LoginResponse,
  McpServer,
  Message,
  MfaMethod,
  ModelCatalogProvider,
  ModelProvider,
  ModelPurpose,
  MoveOrgUnitRequest,
  NotifPreferenceItem,
  OpsRun,
  OrgUnit,
  PatchMcpServerRequest,
  PatchTenantRequest,
  PermissionDefinition,
  PoolSnapshot,
  ProbeReport,
  ProbeRequest,
  QuotaInfo,
  RegisterMcpServerRequest,
  RegisterProviderRequest,
  ResolvedDefault,
  RoleBinding,
  Run,
  RunState,
  SandboxInfo,
  SandboxItem,
  Session,
  Skill,
  InstalledSkill,
  SkillVersion,
  Team,
  TeamMember,
  Tenant,
  TenantDefaultModel,
  TenantSubscription,
  TokenUsage,
  TopologyGraph,
  UpdateOrgUnitRequest,
  UpdateProviderRequest,
  UpdateRoleRequest,
  UsageRecord,
  UserModelPreference,
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

/**
 * Build a query string from a flat params object, dropping null/undefined
 * values. Returns "" or "?a=1&b=2". Used by the admin console v2 namespaces
 * where most list endpoints accept optional cross-tenant / filter params.
 */
function qs(params?: Record<string, string | number | boolean | undefined | null>): string {
  if (!params) return '';
  const pairs = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return pairs.length ? `?${pairs.join('&')}` : '';
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
    getState: (sessionId: string, runId: string) =>
      request<RunState>('GET', `/sessions/${sessionId}/runs/${runId}/state`),
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
    create: (title?: string, agentConfig?: Record<string, unknown>) =>
      request<Session>('POST', '/sessions', { title, agent_config: agentConfig }),
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

  apps: {
    list: (scope?: 'mine' | 'published') => {
      const qs = scope ? `?scope=${encodeURIComponent(scope)}` : '';
      return request<ListPage<App>>('GET', `/apps${qs}`);
    },
    get: (id: string) =>
      request<App>('GET', `/apps/${id}`),
    create: (body: AppCreate) =>
      request<App>('POST', '/apps', body),
    update: (id: string, body: AppUpdate) =>
      request<App>('PUT', `/apps/${id}`, body),
    remove: (id: string) =>
      request<void>('DELETE', `/apps/${id}`),
    createVersion: (id: string, body: AppVersionCreate) =>
      request<AppVersion>('POST', `/apps/${id}/versions`, body),
    updateVersion: (id: string, vid: string, body: AppVersionCreate) =>
      request<AppVersion>('PUT', `/apps/${id}/versions/${vid}`, body),
    submit: (id: string, vid: string) =>
      request<AppVersion>('POST', `/apps/${id}/versions/${vid}/submit`),
    publish: (id: string, vid: string) =>
      request<AppVersion>('POST', `/apps/${id}/versions/${vid}/publish`),
    reviewPending: () =>
      request<ListPage<AppReviewItem>>('GET', '/apps/review/pending'),
    approve: (vid: string, note?: string) =>
      request<AppVersion>('POST', `/apps/review/${vid}/approve`, { note }),
    reject: (vid: string, note: string) =>
      request<AppVersion>('POST', `/apps/review/${vid}/reject`, { note }),
    run: (
      id: string,
      body: {
        inputs: Record<string, unknown>;
        file_inputs: Record<string, string[]>;
        session_id?: string;
      },
    ) => request<AppRunResult>('POST', `/apps/${id}/run`, body),
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
        const query = tenantId ? `?tenant_id=${encodeURIComponent(tenantId)}` : '';
        return request<{ items: AdminUser[] }>('GET', `/admin/users${query}`);
      },
      // Invite a new user. Cross-tenant: pass tenant_id in the body.
      invite: (body: InviteUserRequest) =>
        request<AdminUser>('POST', '/admin/users', body),
      freeze: (id: string, tenantId?: string) =>
        request<void>('POST', `/admin/users/${id}/freeze${qs({ tenant_id: tenantId })}`),
      unfreeze: (id: string, tenantId?: string) =>
        request<void>('POST', `/admin/users/${id}/unfreeze${qs({ tenant_id: tenantId })}`),
      resetPassword: (id: string, tenantId?: string) =>
        request<void>('POST', `/admin/users/${id}/password:reset${qs({ tenant_id: tenantId })}`),
    },
    audit: {
      query: (body: AuditQueryRequest) =>
        request<{ items: AuditEvent[]; next_cursor?: string }>('POST', '/admin/audit/events:query', body),
      // Verify hash-chain integrity over a time window.
      verifyChain: (body: AuditChainVerifyRequest) =>
        request<AuditChainVerifyResult>('POST', '/admin/audit/chain:verify', body),
      // Kick off an async export job; poll getExport(id) until READY.
      createExport: (body: AuditExportRequest) =>
        request<AuditExport>('POST', '/admin/audit/export', body),
      getExport: (id: string) =>
        request<AuditExport>('GET', `/admin/audit/export/${id}`),
    },
    billing: {
      quota: (tenantId: string) =>
        request<QuotaInfo>('GET', `/admin/billing/quota?tenant_id=${encodeURIComponent(tenantId)}`),
      // Plan catalogue — GET returns a {items: [...]} envelope; unwrap to array.
      plans: () =>
        request<{ items: BillingPlanDef[] }>('GET', '/admin/billing/plans').then((r) => r.items),
      // Current subscription for a tenant (scoped via tenant_id query).
      subscription: (tenantId?: string) =>
        request<TenantSubscription>('GET', `/admin/billing/subscription${qs({ tenant_id: tenantId })}`),
      changePlan: (body: ChangePlanRequest) =>
        request<TenantSubscription>('POST', '/admin/billing/subscription/change-plan', body),
      cancelSubscription: (body: CancelSubscriptionRequest) =>
        request<TenantSubscription>('POST', '/admin/billing/subscription/cancel', body),
      // Usage query — params (tenant_id, dimension, time range, granularity) passed through.
      usage: (params?: Record<string, string | number | undefined>) =>
        request<UsageRecord>('GET', `/admin/billing/usage${qs(params)}`),
    },
    // RBAC (→ foundation-authz). List endpoints return {items:[...]} — unwrapped to arrays.
    authz: {
      roles: {
        list: (tenantId?: string) =>
          request<{ items: AdminRole[] }>('GET', `/admin/authz/roles${qs({ tenant_id: tenantId })}`).then((r) => r.items),
        create: (body: CreateRoleRequest, tenantId?: string) =>
          request<AdminRole>('POST', `/admin/authz/roles${qs({ tenant_id: tenantId })}`, body),
        get: (roleId: string, tenantId?: string) =>
          request<AdminRole>('GET', `/admin/authz/roles/${roleId}${qs({ tenant_id: tenantId })}`),
        patch: (roleId: string, body: UpdateRoleRequest, tenantId?: string) =>
          request<AdminRole>('PATCH', `/admin/authz/roles/${roleId}${qs({ tenant_id: tenantId })}`, body),
        delete: (roleId: string, tenantId?: string) =>
          request<void>('DELETE', `/admin/authz/roles/${roleId}${qs({ tenant_id: tenantId })}`),
      },
      permissions: {
        // Permission catalogue. Optional `service` filter passed through.
        // Upstream (authz) returns a *bare array*, not an {items} envelope.
        list: (params?: { service?: string }) =>
          request<PermissionDefinition[] | { items: PermissionDefinition[] }>(
            'GET',
            `/admin/authz/permissions${qs(params)}`,
          ).then((r) => (Array.isArray(r) ? r : r.items)),
      },
      bindings: {
        list: (tenantId?: string) =>
          request<{ items: RoleBinding[] }>('GET', `/admin/authz/bindings${qs({ tenant_id: tenantId })}`).then((r) => r.items),
        create: (body: CreateBindingRequest, tenantId?: string) =>
          request<RoleBinding>('POST', `/admin/authz/bindings${qs({ tenant_id: tenantId })}`, body),
        delete: (bindingId: string, tenantId?: string) =>
          request<void>('DELETE', `/admin/authz/bindings/${bindingId}${qs({ tenant_id: tenantId })}`),
      },
    },
    // Organisation structure (→ foundation-org). List endpoints unwrap {items:[...]}.
    org: {
      orgUnits: {
        list: (tenantId?: string) =>
          request<{ items: OrgUnit[] }>('GET', `/admin/org/org-units${qs({ tenant_id: tenantId })}`).then((r) => r.items),
        create: (body: CreateOrgUnitRequest, tenantId?: string) =>
          request<OrgUnit>('POST', `/admin/org/org-units${qs({ tenant_id: tenantId })}`, body),
        get: (id: string, tenantId?: string) =>
          request<OrgUnit>('GET', `/admin/org/org-units/${id}${qs({ tenant_id: tenantId })}`),
        patch: (id: string, body: UpdateOrgUnitRequest, tenantId?: string) =>
          request<OrgUnit>('PATCH', `/admin/org/org-units/${id}${qs({ tenant_id: tenantId })}`, body),
        delete: (id: string, tenantId?: string) =>
          request<void>('DELETE', `/admin/org/org-units/${id}${qs({ tenant_id: tenantId })}`),
        move: (id: string, body: MoveOrgUnitRequest, tenantId?: string) =>
          request<OrgUnit>('POST', `/admin/org/org-units/${id}/move${qs({ tenant_id: tenantId })}`, body),
      },
      teams: {
        list: (tenantId?: string) =>
          request<{ items: Team[] }>('GET', `/admin/org/teams${qs({ tenant_id: tenantId })}`).then((r) => r.items),
        create: (body: CreateTeamRequest, tenantId?: string) =>
          request<Team>('POST', `/admin/org/teams${qs({ tenant_id: tenantId })}`, body),
        members: {
          list: (teamId: string, tenantId?: string) =>
            request<{ items: TeamMember[] }>('GET', `/admin/org/teams/${teamId}/members${qs({ tenant_id: tenantId })}`).then((r) => r.items),
          add: (teamId: string, body: AddTeamMemberRequest, tenantId?: string) =>
            request<TeamMember>('POST', `/admin/org/teams/${teamId}/members${qs({ tenant_id: tenantId })}`, body),
          remove: (teamId: string, userId: string, tenantId?: string) =>
            request<void>('DELETE', `/admin/org/teams/${teamId}/members/${userId}${qs({ tenant_id: tenantId })}`),
        },
      },
    },
    // Feature flags (→ foundation-feature-flag). Upstream returns a bare array;
    // tolerate a legacy {items:[...]} wrapper as well.
    flags: {
      list: () =>
        request<FeatureFlag[] | { items: FeatureFlag[] }>('GET', '/admin/flags').then((r) =>
          Array.isArray(r) ? r : (r.items ?? []),
        ),
      get: (key: string) =>
        request<FeatureFlag>('GET', `/admin/flags/${encodeURIComponent(key)}`),
      put: (key: string, body: FeatureFlagUpdate) =>
        request<FeatureFlag>('PUT', `/admin/flags/${encodeURIComponent(key)}`, body),
      killSwitch: (key: string, body: KillSwitchRequest) =>
        request<FeatureFlag>('POST', `/admin/flags/${encodeURIComponent(key)}/kill-switch`, body),
    },
    // Skills governance (→ skill-store). list/versions unwrap {items:[...]}.
    skills: {
      list: () =>
        request<{ items: AdminSkill[] }>('GET', '/admin/skills').then((r) => r.items),
      get: (name: string) =>
        request<AdminSkill>('GET', `/admin/skills/${encodeURIComponent(name)}`),
      versions: (name: string) =>
        request<{ items: AdminSkillVersion[] }>('GET', `/admin/skills/${encodeURIComponent(name)}/versions`).then((r) => r.items),
      delete: (name: string) =>
        request<void>('DELETE', `/admin/skills/${encodeURIComponent(name)}`),
    },
    // MCP servers governance (→ mcp-registry). list() unwraps {items:[...]}.
    mcp: {
      list: () =>
        request<{ items: McpServer[] }>('GET', '/admin/mcp/servers').then((r) => r.items),
      register: (body: RegisterMcpServerRequest) =>
        request<McpServer>('POST', '/admin/mcp/servers', body),
      get: (id: string) =>
        request<McpServer>('GET', `/admin/mcp/servers/${id}`),
      patch: (id: string, body: PatchMcpServerRequest) =>
        request<McpServer>('PATCH', `/admin/mcp/servers/${id}`, body),
      delete: (id: string) =>
        request<void>('DELETE', `/admin/mcp/servers/${id}`),
    },
    // Knowledge base governance (→ knowledge-ingest). documents.list unwraps {items:[...]}.
    knowledge: {
      documents: {
        list: (tenantId?: string) =>
          request<{ items: KnowledgeDocument[] }>('GET', `/admin/knowledge/documents${qs({ tenant_id: tenantId })}`).then((r) => r.items),
        get: (id: string, tenantId?: string) =>
          request<KnowledgeDocument>('GET', `/admin/knowledge/documents/${id}${qs({ tenant_id: tenantId })}`),
        reingest: (id: string, tenantId?: string) =>
          request<IngestResult>('POST', `/admin/knowledge/documents/${id}/reingest${qs({ tenant_id: tenantId })}`),
      },
    },
    // Run & sandbox governance (→ scheduler / sandbox-pool / orchestrator).
    // runs / sandboxes unwrap {items:[...]}; pool / usageDaily return objects.
    ops: {
      runs: (params?: Record<string, string | number | undefined>) =>
        request<{ items: OpsRun[] }>('GET', `/admin/ops/runs${qs(params)}`).then((r) => r.items),
      cancelRun: (id: string) =>
        request<void>('POST', `/admin/ops/runs/${id}/cancel`),
      sandboxes: () =>
        request<{ items: SandboxInfo[] }>('GET', '/admin/ops/sandboxes').then((r) => r.items),
      stopSandbox: (id: string) =>
        request<void>('POST', `/admin/ops/sandboxes/${id}/stop`),
      pool: () =>
        request<PoolSnapshot>('GET', '/admin/ops/pool'),
      usageDaily: (params?: Record<string, string | number | undefined>) =>
        request<DailyTokenUsageResponse>('GET', `/admin/ops/usage/daily${qs(params)}`),
    },
    llm: {
      probe: (body: ProbeRequest) =>
        request<ProbeReport>('POST', '/admin/llm/probe', body),
      listProviders: () =>
        request<{ items: ModelProvider[] }>('GET', '/admin/llm/providers'),
      registerProvider: (body: RegisterProviderRequest) =>
        request<ModelProvider>('POST', '/admin/llm/providers', body),
      updateProvider: (id: string, body: UpdateProviderRequest) =>
        request<ModelProvider>('PATCH', `/admin/llm/providers/${id}`, body),
      deleteProvider: (id: string) =>
        request<void>('DELETE', `/admin/llm/providers/${id}`),
      // Tenant-level model defaults — proxied via BFF /admin/llm/tenant-defaults*.
      // GET returns a {items: [...]} envelope; unwrap to an array for callers.
      listTenantDefaults: () =>
        request<{ items: TenantDefaultModel[] }>('GET', '/admin/llm/tenant-defaults').then((r) => r.items),
      upsertTenantDefault: (body: { purpose: ModelPurpose; model_id: string; provider_id: string }) =>
        request<TenantDefaultModel>('PUT', '/admin/llm/tenant-defaults', body),
      resolveTenantDefault: (purpose: ModelPurpose) =>
        request<ResolvedDefault>('GET', `/admin/llm/tenant-defaults/resolve?purpose=${purpose}`),
    },
  },

  notifications: {
    preferences: () =>
      request<{ items: NotifPreferenceItem[] }>('GET', '/notifications/preferences'),
    updatePreferences: (items: { event_key: string; email_enabled: boolean; push_enabled: boolean }[]) =>
      request<{ items: NotifPreferenceItem[] }>('PUT', '/notifications/preferences', { items }),
  },

  // User-level model preferences — proxied via BFF /llm/user-preferences*.
  // GET returns a {items: [...]} envelope; unwrap to an array for callers.
  modelPreferences: {
    // Sanitized provider catalogue for non-admin model selection (no creds).
    listModels: () =>
      request<{ items: ModelCatalogProvider[] }>('GET', '/llm/models').then((r) => r.items),
    list: () =>
      request<{ items: UserModelPreference[] }>('GET', '/llm/user-preferences').then((r) => r.items),
    upsert: (body: { purpose: ModelPurpose; model_id: string; provider_id: string }) =>
      request<UserModelPreference>('PUT', '/llm/user-preferences', body),
    delete: (purpose: ModelPurpose) =>
      request<void>('DELETE', `/llm/user-preferences/${encodeURIComponent(purpose)}`),
    resolve: (purpose: ModelPurpose) =>
      request<ResolvedDefault>('GET', `/llm/user-preferences/resolve?purpose=${purpose}`),
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
