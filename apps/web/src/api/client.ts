import type {
  ActiveSession,
  RespondBody,
  CanvasEditBody,
  CanvasEditResult,
  PendingInteractionsResponse,
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
  AssessDpiaRequest,
  CompleteDpiaRequest,
  ComplianceReport,
  CreatePlatformApiKeyRequest,
  DlpClassification,
  DlpClassificationList,
  DlpEventList,
  DlpScanRequest,
  DlpScanResult,
  DlpStats,
  DlpTagRequest,
  DpiaRecord,
  DsarRecord,
  GenerateReportRequest,
  NotificationSendRequest,
  NotificationTemplate,
  OAuthApp,
  PamElevation,
  PamSession,
  PlatformApiKey,
  RecordProcessingRequest,
  RegisterOAuthAppRequest,
  RequestDsarRequest,
  RequestElevationRequest,
  ApproveElevationRequest,
  RopaRecord,
  SubscribeWebhookRequest,
  Webhook,
  AuditChainVerifyRequest,
  AuditChainVerifyResult,
  AuditEvent,
  AuditQueryRequest,
  Attachment,
  AttachmentKind,
  BillingPlanDef,
  CancelSubscriptionRequest,
  ChangePlanRequest,
  CreatePlanRequest,
  CreateSubscriptionRequest,
  CreateBindingRequest,
  CreateOrgUnitRequest,
  CreateRoleRequest,
  CreateTeamRequest,
  CreateTenantRequest,
  CursorPage,
  DailyTokenUsageResponse,
  FeatureFlag,
  FeatureFlagUpdate,
  UserFlagOverride,
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
  LargeAttachmentTenantSetting,
  LargeAttachmentUserAllow,
  LargeAttachmentSubjectAllow,
  LargeAttachmentSubjectType,
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
  TokenUsageBreakdown,
  TopologyGraph,
  UpdateOrgUnitRequest,
  UpdateProviderRequest,
  UpdateRoleRequest,
  UsageRecord,
  UserModelPreference,
  UserProfile,
  VulnSlaDashboard,
  Vulnerability,
  RiskAcceptRequest,
  SecurityIncident,
  CreateIncidentRequest,
  ResolveIncidentRequest,
  ScanRun,
  SbomWatch,
  RegisterSbomWatchRequest,
  SecurityDrill,
  ScheduleDrillRequest,
  RecycleBinItem,
  RecycleBinPage,
  SoftDeleteRequest,
  HardDeleteRequest,
  SweepResult,
  LegalHold,
  PlaceLegalHoldRequest,
  ReleaseLegalHoldRequest,
  MaskingRulesResponse,
  DataResidency,
  CrossBorderCheckRequest,
  CrossBorderResult,
  DestructionRequest,
  CreateDestructionRequest,
  ConsentRecord,
  RecordConsentRequest,
  SsoProvider,
  CreateSsoProviderRequest,
  SsoRoutingRule,
  CreateSsoRoutingRuleRequest,
  CreatePlatformSsoProviderRequest,
  StorageQuotaSummaryResponse,
  StorageQuotaSummaryItem,
  StorageQuotaPatch,
  OrphanReport,
  OrphanReportListResponse,
  PromQueryRangeResponse,
  BucketPurpose,
  CronJob,
  CronJobCreate,
  CronJobUpdate,
} from './types';

const BFF_BASE = '/api/bff/v1';

class ApiError extends Error {
  status: number;
  code: string;
  /** Optional machine-readable cause from the upstream problem (e.g. the org
   *  NOT_EMPTY `reason`: has_children / has_teams / has_members). Lets callers
   *  show a more specific message than the generic `user_message`. */
  reason?: string;
  constructor(status: number, code: string, detail: string, reason?: string) {
    super(detail);
    this.status = status;
    this.code = code;
    this.reason = reason;
    this.name = 'ApiError';
  }
}

// FastAPI wraps error bodies as { detail: <body> }, and the BFF proxy may nest
// the upstream RFC7807 problem+json one level deeper. Walk the structure to
// surface the friendliest message available: a `user_message` (already
// localised) wins over a raw `detail` string; never stringify a whole problem
// document into the message the user sees.
function extractMessage(problem: unknown, depth = 0): string {
  if (problem == null || depth > 4) return '请求失败';
  if (typeof problem === 'string') return problem;
  if (typeof problem !== 'object') return '请求失败';
  const obj = problem as Record<string, unknown>;
  if (typeof obj.user_message === 'string' && obj.user_message) return obj.user_message;
  if ('detail' in obj) return extractMessage(obj.detail, depth + 1);
  if (typeof obj.title === 'string' && obj.title) return obj.title;
  return '请求失败';
}

function extractCode(problem: unknown, depth = 0): string | undefined {
  if (problem == null || depth > 4 || typeof problem !== 'object') return undefined;
  const obj = problem as Record<string, unknown>;
  if (typeof obj.code === 'string' && obj.code) return obj.code;
  if ('detail' in obj) return extractCode(obj.detail, depth + 1);
  return undefined;
}

// foundation flattens its FoundationError ``extra`` (e.g. {"reason": ...}) onto
// the top of the problem document; the BFF proxy nests that under `detail`.
function extractReason(problem: unknown, depth = 0): string | undefined {
  if (problem == null || depth > 4 || typeof problem !== 'object') return undefined;
  const obj = problem as Record<string, unknown>;
  if (typeof obj.reason === 'string' && obj.reason) return obj.reason;
  if ('detail' in obj) return extractReason(obj.detail, depth + 1);
  return undefined;
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
    throw new ApiError(
      res.status,
      extractCode(problem) || 'UNKNOWN',
      extractMessage(problem),
      extractReason(problem),
    );
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
    updateProfile: (body: { display_name: string }) =>
      request<UserProfile>('PATCH', '/auth/me', body),
    mfaMethods: () => request<MfaMethod[]>('GET', '/auth/mfa/methods'),
    sessions: () => request<ActiveSession[]>('GET', '/auth/sessions'),
    // PUBLIC — enterprise SSO login options shown on the login page before
    // the user has a session.  Each item drives one "企业账号登录" button.
    ssoOptions: () =>
      request<{ items: { id: string; name: string; kind: string }[] }>(
        'GET',
        '/auth/sso/options',
      ),
  },

  featureFlags: {
    list: () =>
      request<FeatureFlag[]>('GET', '/flags').then((r) =>
        Array.isArray(r) ? r : [],
      ),
  },

  userFlagOverrides: {
    list: () =>
      request<UserFlagOverride[]>('GET', '/user-flag-overrides').then((r) =>
        Array.isArray(r) ? r : [],
      ),
    put: (key: string, body: { enabled: boolean }) =>
      request<UserFlagOverride>('PUT', `/user-flag-overrides/${key}`, { body }),
    delete: (key: string) =>
      request<void>('DELETE', `/user-flag-overrides/${key}`),
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
    tokenUsageBreakdown: (days = 30) =>
      request<TokenUsageBreakdown>('GET', `/runs/token-usage/breakdown?days=${days}`),
  },

  sessions: {
    list: (cursor?: string) =>
      request<CursorPage<Session>>('GET', `/sessions?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`),
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
    /**
     * Forward a HITL response (confirm approve/reject, or a question answer)
     * to the orchestrator via the BFF proxy.
     *
     * Returns `{ run_id, status: "RUNNING" }` on success.
     * Throws ApiError with status 409 if the run is not paused.
     * Throws ApiError with status 404 if the pause has expired.
     */
    respond: (sessionId: string, runId: string, body: RespondBody) =>
      request<{ run_id: string; status: string }>(
        'POST',
        `/sessions/${sessionId}/runs/${runId}/respond`,
        body,
      ),
    /**
     * Pull all currently-paused HITL interactions for the session.
     * Called on mount / reconnect to rehydrate ConfirmCard / QuestionCard
     * for a run that paused while the page was closed.  Best-effort: the
     * backend always returns 200 with a (possibly empty) `items` array.
     */
    pending: (sessionId: string) =>
      request<PendingInteractionsResponse>('GET', `/sessions/${sessionId}/pending`),
    /**
     * Silent canvas revision — the ChatGPT-canvas-style局部修订. Unlike the
     * normal chat flow this does NOT go through POST /chat, does NOT start an
     * agent run, and leaves the transcript untouched. The BFF returns the
     * replacement text synchronously; the caller feeds it into the canvas
     * suggestion card (accept / reject) without any message ever appearing.
     * Throws ApiError carrying the localised `user_message` on failure.
     */
    canvasEdit: (sessionId: string, body: CanvasEditBody) =>
      request<CanvasEditResult>('POST', `/sessions/${sessionId}/canvas/edit`, body),
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
        throw new ApiError(res.status, extractCode(problem) || 'UNKNOWN', extractMessage(problem));
      }
      return res.json();
    },
    // Pre-upload gate: ask the BFF whether a file of this size may be uploaded.
    // Large attachments (>= BFF threshold) trigger the large-memory sandbox tier
    // and require the `large-attachment` entitlement; small files are always
    // allowed. The BFF owns the threshold (no hardcoded size in the frontend).
    largeCheck: (sizeBytes: number) =>
      request<{ allowed: boolean; is_large: boolean; reason: string | null }>(
        'GET',
        `/attachments/large-check?size_bytes=${encodeURIComponent(sizeBytes)}`,
      ),
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
        throw new ApiError(res.status, extractCode(problem) || 'UNKNOWN', extractMessage(problem));
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
    // Install a skill into the calling user's toolbox.
    //   pinnedVersion = undefined / ''  → omit the `version` field, backend
    //                                     defaults to null → follow latest.
    //   pinnedVersion = "0.2.0"         → lock the install to that exact
    //                                     skill-store version.
    // The body field name is `version` (matches skill-store InstallSkillBody).
    installSkill: (skillId: string, enabledByDefault = true, pinnedVersion?: string) => {
      const body: { enabled_by_default: boolean; version?: string } = {
        enabled_by_default: enabledByDefault,
      };
      if (pinnedVersion) body.version = pinnedVersion;
      return request<InstalledSkill>('POST', `/library/skills/${skillId}/install`, body);
    },
    uninstallSkill: (skillId: string) =>
      request<void>('DELETE', `/library/skills/${skillId}/install`),
    // Unwrap the ListPage envelope: the BFF returns {items, total} from
    // skill-store, but the install modal only needs the array.
    listSkillVersions: (skillId: string) =>
      request<ListPage<SkillVersion>>('GET', `/library/skills/${skillId}/versions`).then(
        (r) => r.items,
      ),
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
        throw new ApiError(res.status, extractCode(problem) || 'UNKNOWN', extractMessage(problem));
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
        throw new ApiError(res.status, extractCode(problem) || 'UNKNOWN', extractMessage(problem));
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
    // Session replay (tenant-admin only): lists EVERY session in the caller's
    // tenant. Distinct from `api.sessions.list()`, which is now owner-scoped
    // (a member only sees their own). Gated server-side by a tenant_admin
    // authz check in the BFF.
    sessions: {
      list: (cursor?: string) =>
        request<CursorPage<Session>>(
          'GET',
          `/admin/sessions?limit=50${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        ),
    },
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
      // Invite a new user. Cross-tenant target tenant goes in the query string.
      invite: (body: InviteUserRequest, tenantId?: string) =>
        request<AdminUser>('POST', `/admin/users${qs({ tenant_id: tenantId })}`, body),
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
      // Export audit events as a downloadable blob (CSV by default). The query
      // filter (the same shape used by events:query) is posted as the JSON body.
      exportEvents: async (
        filter: Record<string, unknown>,
        opts?: { format?: string; tenantId?: string },
      ): Promise<Blob> => {
        const res = await fetch(
          `${BFF_BASE}/admin/audit/export${qs({ format: opts?.format ?? 'csv', tenant_id: opts?.tenantId })}`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(filter),
            credentials: 'include',
          },
        );
        if (!res.ok) {
          const problem = await res.json().catch(() => ({ detail: res.statusText }));
          throw new ApiError(res.status, extractCode(problem) || 'UNKNOWN', extractMessage(problem));
        }
        return res.blob();
      },
    },
    billing: {
      quota: (tenantId: string) =>
        request<QuotaInfo>('GET', `/admin/billing/quota?tenant_id=${encodeURIComponent(tenantId)}`),
      // Plan catalogue — GET returns a {items: [...]} envelope; unwrap to array.
      plans: () =>
        request<{ items: BillingPlanDef[] }>('GET', '/admin/billing/plans').then((r) => r.items),
      createPlan: (body: CreatePlanRequest) =>
        request<BillingPlanDef>('POST', '/admin/billing/plans', body),
      createSubscription: (body: CreateSubscriptionRequest, tenantId?: string) =>
        request<TenantSubscription>('POST', `/admin/billing/subscriptions${qs({ tenant_id: tenantId })}`, body),
      // Current subscription for a tenant (scoped via tenant_id query).
      subscription: (tenantId?: string) =>
        request<TenantSubscription>('GET', `/admin/billing/subscription${qs({ tenant_id: tenantId })}`),
      changePlan: (body: ChangePlanRequest, tenantId?: string) =>
        request<TenantSubscription>('POST', `/admin/billing/subscription/change-plan${qs({ tenant_id: tenantId })}`, body),
      cancelSubscription: (body: CancelSubscriptionRequest, tenantId?: string) =>
        request<TenantSubscription>('POST', `/admin/billing/subscription/cancel${qs({ tenant_id: tenantId })}`, body),
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
        delete: (teamId: string, tenantId?: string) =>
          request<void>('DELETE', `/admin/org/teams/${teamId}${qs({ tenant_id: tenantId })}`),
        members: {
          list: (teamId: string, tenantId?: string) =>
            request<TeamMember[] | { items: TeamMember[] }>('GET', `/admin/org/teams/${teamId}/members${qs({ tenant_id: tenantId })}`).then((r) =>
              Array.isArray(r) ? r : (r.items ?? []),
            ),
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
      list: (tenantId?: string) =>
        request<FeatureFlag[] | { items: FeatureFlag[] }>('GET', `/admin/flags${qs({ tenant_id: tenantId })}`).then((r) =>
          Array.isArray(r) ? r : (r.items ?? []),
        ),
      put: (key: string, body: FeatureFlagUpdate, tenantId?: string) =>
        request<FeatureFlag>('PUT', `/admin/flags/${encodeURIComponent(key)}${qs({ tenant_id: tenantId })}`, body),
      killSwitch: (key: string, body: KillSwitchRequest, tenantId?: string) =>
        request<FeatureFlag>('POST', `/admin/flags/${encodeURIComponent(key)}/kill-switch${qs({ tenant_id: tenantId })}`, body),
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
        delete: (id: string, tenantId?: string) =>
          request<void>('DELETE', `/admin/knowledge/documents/${id}${qs({ tenant_id: tenantId })}`),
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
    },
    // Notification center (→ foundation-notification). All endpoints accept an
    // optional ?tenant_id for cross-tenant operation. List unwraps {items}.
    notification: {
      templates: (tenantId?: string) =>
        request<{ items: NotificationTemplate[] } | NotificationTemplate[]>(
          'GET',
          `/admin/notification/templates${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      send: (body: NotificationSendRequest, tenantId?: string) =>
        request<Record<string, unknown>>('POST', `/admin/notification/send${qs({ tenant_id: tenantId })}`, body),
    },
    // API platform (→ foundation-api-platform). Cross-tenant via ?tenant_id.
    apiPlatform: {
      keys: {
        list: (tenantId?: string) =>
          request<{ items: PlatformApiKey[] } | PlatformApiKey[]>(
            'GET',
            `/admin/api-platform/keys${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: CreatePlatformApiKeyRequest, tenantId?: string) =>
          request<PlatformApiKey>('POST', `/admin/api-platform/keys${qs({ tenant_id: tenantId })}`, body),
        revoke: (id: string, tenantId?: string) =>
          request<Record<string, unknown>>('POST', `/admin/api-platform/keys/${id}/revoke${qs({ tenant_id: tenantId })}`),
      },
      oauthApps: {
        list: (tenantId?: string) =>
          request<{ items: OAuthApp[] } | OAuthApp[]>(
            'GET',
            `/admin/api-platform/oauth-apps${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: RegisterOAuthAppRequest, tenantId?: string) =>
          request<OAuthApp>('POST', `/admin/api-platform/oauth-apps${qs({ tenant_id: tenantId })}`, body),
      },
      webhooks: {
        list: (tenantId?: string) =>
          request<{ items: Webhook[] } | Webhook[]>(
            'GET',
            `/admin/api-platform/webhooks${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: SubscribeWebhookRequest, tenantId?: string) =>
          request<Webhook>('POST', `/admin/api-platform/webhooks${qs({ tenant_id: tenantId })}`, body),
        delete: (id: string, tenantId?: string) =>
          request<Record<string, unknown>>('DELETE', `/admin/api-platform/webhooks/${id}${qs({ tenant_id: tenantId })}`),
      },
    },
    // SSO identity sources (→ foundation-identity /sso). Cross-tenant via ?tenant_id.
    sso: {
      list: (tenantId?: string) =>
        request<SsoProvider[] | { items: SsoProvider[] }>(
          'GET',
          `/admin/sso/providers${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      create: (body: CreateSsoProviderRequest, tenantId?: string) =>
        request<SsoProvider>('POST', `/admin/sso/providers${qs({ tenant_id: tenantId })}`, body),
      enable: (id: string, tenantId?: string) =>
        request<SsoProvider>('POST', `/admin/sso/providers/${id}/enable${qs({ tenant_id: tenantId })}`),
      disable: (id: string, tenantId?: string) =>
        request<SsoProvider>('POST', `/admin/sso/providers/${id}/disable${qs({ tenant_id: tenantId })}`),
      delete: (id: string, tenantId?: string) =>
        request<Record<string, unknown>>('DELETE', `/admin/sso/providers/${id}${qs({ tenant_id: tenantId })}`),
      // Platform-scoped providers + tenant routing rules (super_admin only).
      // No tenant override — platform scope ignores tenant context.
      platform: {
        list: () =>
          request<SsoProvider[] | { items: SsoProvider[] }>(
            'GET',
            '/admin/sso/platform/providers',
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: CreatePlatformSsoProviderRequest) =>
          request<SsoProvider>('POST', '/admin/sso/platform/providers', body),
        enable: (id: string) =>
          request<SsoProvider>('POST', `/admin/sso/platform/providers/${id}/enable`),
        disable: (id: string) =>
          request<SsoProvider>('POST', `/admin/sso/platform/providers/${id}/disable`),
        delete: (id: string) =>
          request<Record<string, unknown>>('DELETE', `/admin/sso/platform/providers/${id}`),
        rules: {
          list: (providerId: string) =>
            request<SsoRoutingRule[] | { items: SsoRoutingRule[] }>(
              'GET',
              `/admin/sso/platform/providers/${providerId}/routing-rules`,
            ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
          create: (providerId: string, body: CreateSsoRoutingRuleRequest) =>
            request<SsoRoutingRule>(
              'POST',
              `/admin/sso/platform/providers/${providerId}/routing-rules`,
              body,
            ),
          update: (
            providerId: string,
            ruleId: string,
            body: Partial<CreateSsoRoutingRuleRequest>,
          ) =>
            request<SsoRoutingRule>(
              'PATCH',
              `/admin/sso/platform/providers/${providerId}/routing-rules/${ruleId}`,
              body,
            ),
          delete: (providerId: string, ruleId: string) =>
            request<Record<string, unknown>>(
              'DELETE',
              `/admin/sso/platform/providers/${providerId}/routing-rules/${ruleId}`,
            ),
        },
      },
    },
    // PAM — privileged access management (→ foundation-pam).
    pam: {
      pendingElevations: (tenantId?: string) =>
        request<{ items: PamElevation[] } | PamElevation[]>(
          'GET',
          `/admin/pam/elevations/pending${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      elevationHistory: (tenantId?: string) =>
        request<{ items: PamElevation[] } | PamElevation[]>(
          'GET',
          `/admin/pam/elevations/history${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      sessions: (tenantId?: string) =>
        request<{ items: PamSession[] } | PamSession[]>(
          'GET',
          `/admin/pam/sessions${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      requestElevation: (body: RequestElevationRequest, tenantId?: string) =>
        request<PamElevation>('POST', `/admin/pam/elevations${qs({ tenant_id: tenantId })}`, body),
      approveElevation: (id: string, body: ApproveElevationRequest, tenantId?: string) =>
        request<PamElevation>('POST', `/admin/pam/elevations/${id}/approve${qs({ tenant_id: tenantId })}`, body),
      revokeElevation: (id: string, tenantId?: string) =>
        request<Record<string, unknown>>('POST', `/admin/pam/elevations/${id}/revoke${qs({ tenant_id: tenantId })}`),
    },
    // DLP — data-loss prevention (→ foundation-dlp). Note: classification
    // endpoints use literal ':' verbs (tag / get).
    dlp: {
      scan: (body: DlpScanRequest, tenantId?: string) =>
        request<DlpScanResult>('POST', `/admin/dlp/scan${qs({ tenant_id: tenantId })}`, body),
      tagClassification: (body: DlpTagRequest, tenantId?: string) =>
        request<DlpClassification>('POST', `/admin/dlp/classification:tag${qs({ tenant_id: tenantId })}`, body),
      getClassification: (resourceType: string, resourceId: string, tenantId?: string) =>
        request<DlpClassification>(
          'GET',
          `/admin/dlp/classification:get${qs({ resource_type: resourceType, resource_id: resourceId, tenant_id: tenantId })}`,
        ),
      listEvents: (
        params: { check_type?: string; decision?: string; limit?: number; before?: string },
        tenantId?: string,
      ) =>
        request<DlpEventList>('GET', `/admin/dlp/events${qs({ ...params, tenant_id: tenantId })}`),
      listClassifications: (
        params: { sensitivity?: string; resource_type?: string; limit?: number; before?: string },
        tenantId?: string,
      ) =>
        request<DlpClassificationList>(
          'GET',
          `/admin/dlp/classifications${qs({ ...params, tenant_id: tenantId })}`,
        ),
      stats: (params: { since?: string }, tenantId?: string) =>
        request<DlpStats>('GET', `/admin/dlp/stats${qs({ ...params, tenant_id: tenantId })}`),
    },
    // Compliance center (→ foundation-compliance): ROPA / reports / DSAR / DPIA.
    compliance: {
      ropa: {
        list: (tenantId?: string) =>
          request<{ items: RopaRecord[] } | RopaRecord[]>(
            'GET',
            `/admin/compliance/ropa${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: RecordProcessingRequest, tenantId?: string) =>
          request<RopaRecord>('POST', `/admin/compliance/ropa${qs({ tenant_id: tenantId })}`, body),
      },
      reports: {
        list: (tenantId?: string) =>
          request<{ items: ComplianceReport[] } | ComplianceReport[]>(
            'GET',
            `/admin/compliance/reports${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        generate: (body: GenerateReportRequest, tenantId?: string) =>
          request<ComplianceReport>('POST', `/admin/compliance/reports${qs({ tenant_id: tenantId })}`, body),
      },
      dsar: {
        create: (body: RequestDsarRequest, tenantId?: string) =>
          request<DsarRecord>('POST', `/admin/compliance/dsar${qs({ tenant_id: tenantId })}`, body),
        get: (id: string, tenantId?: string) =>
          request<DsarRecord>('GET', `/admin/compliance/dsar/${id}${qs({ tenant_id: tenantId })}`),
      },
      dpia: {
        create: (body: AssessDpiaRequest, tenantId?: string) =>
          request<DpiaRecord>('POST', `/admin/compliance/dpia${qs({ tenant_id: tenantId })}`, body),
        complete: (id: string, body: CompleteDpiaRequest, tenantId?: string) =>
          request<DpiaRecord>('POST', `/admin/compliance/dpia/${id}/complete${qs({ tenant_id: tenantId })}`, body),
      },
    },
    // Security operations (→ foundation-security-ops): vulnerabilities / incidents /
    // scans / SBOM watch / drills. Platform-level; tenant_id is optional.
    securityOps: {
      vulnerabilities: {
        slaDashboard: (tenantId?: string) =>
          request<VulnSlaDashboard>(
            'GET',
            `/admin/security-ops/vulnerabilities/sla-dashboard${qs({ tenant_id: tenantId })}`,
          ),
        // Filters use dotted alias keys (filter.severity / filter.status / filter.source).
        list: (params?: Record<string, string | undefined>, tenantId?: string) =>
          request<{ items: Vulnerability[] } | Vulnerability[]>(
            'GET',
            `/admin/security-ops/vulnerabilities${qs({ ...(params ?? {}), tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        update: (id: string, body: Record<string, unknown>, tenantId?: string) =>
          request<Vulnerability>('PATCH', `/admin/security-ops/vulnerabilities/${id}${qs({ tenant_id: tenantId })}`, body),
        riskAccept: (id: string, body: RiskAcceptRequest, tenantId?: string) =>
          request<Record<string, unknown>>(
            'POST',
            `/admin/security-ops/vulnerabilities/${id}/risk-accept${qs({ tenant_id: tenantId })}`,
            body,
          ),
      },
      incidents: {
        list: (tenantId?: string) =>
          request<{ items: SecurityIncident[] } | SecurityIncident[]>(
            'GET',
            `/admin/security-ops/incidents${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: CreateIncidentRequest, tenantId?: string) =>
          request<SecurityIncident>('POST', `/admin/security-ops/incidents${qs({ tenant_id: tenantId })}`, body),
        update: (id: string, body: Record<string, unknown>, tenantId?: string) =>
          request<SecurityIncident>('PATCH', `/admin/security-ops/incidents/${id}${qs({ tenant_id: tenantId })}`, body),
        resolve: (id: string, body: ResolveIncidentRequest, tenantId?: string) =>
          request<Record<string, unknown>>(
            'POST',
            `/admin/security-ops/incidents/${id}/resolve${qs({ tenant_id: tenantId })}`,
            body,
          ),
      },
      scans: {
        runs: (tenantId?: string) =>
          request<{ items: ScanRun[] } | ScanRun[]>(
            'GET',
            `/admin/security-ops/scans/runs${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      },
      sbom: {
        // Returns a bare array.
        watchList: (tenantId?: string) =>
          request<SbomWatch[] | { items: SbomWatch[] }>(
            'GET',
            `/admin/security-ops/sbom/watch${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        register: (body: RegisterSbomWatchRequest, tenantId?: string) =>
          request<SbomWatch>('POST', `/admin/security-ops/sbom/watch${qs({ tenant_id: tenantId })}`, body),
      },
      drills: {
        list: (tenantId?: string) =>
          request<{ items: SecurityDrill[] } | SecurityDrill[]>(
            'GET',
            `/admin/security-ops/drills${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: ScheduleDrillRequest, tenantId?: string) =>
          request<SecurityDrill>('POST', `/admin/security-ops/drills${qs({ tenant_id: tenantId })}`, body),
      },
    },
    // Data lifecycle (→ foundation-lifecycle): recycle bin + legal holds. Tenant-scoped.
    // Note: several paths embed a `:verb` suffix that must NOT be encoded.
    lifecycle: {
      recycleBin: {
        list: (params?: { cursor?: string; limit?: number }, tenantId?: string) =>
          request<RecycleBinPage>(
            'GET',
            `/admin/lifecycle/recycle-bin${qs({ ...(params ?? {}), tenant_id: tenantId })}`,
          ),
        softDelete: (body: SoftDeleteRequest, tenantId?: string) =>
          request<RecycleBinItem>('POST', `/admin/lifecycle/recycle-bin:soft-delete${qs({ tenant_id: tenantId })}`, body),
        restore: (id: string, tenantId?: string) =>
          request<Record<string, unknown>>('POST', `/admin/lifecycle/recycle-bin/${id}:restore${qs({ tenant_id: tenantId })}`),
        hardDelete: (id: string, body: HardDeleteRequest, tenantId?: string) =>
          request<Record<string, unknown>>(
            'POST',
            `/admin/lifecycle/recycle-bin/${id}:hard-delete${qs({ tenant_id: tenantId })}`,
            body,
          ),
      },
      sweep: (tenantId?: string) =>
        request<SweepResult>('POST', `/admin/lifecycle/sweep${qs({ tenant_id: tenantId })}`),
      legalHolds: {
        list: (tenantId?: string) =>
          request<{ items: LegalHold[] } | LegalHold[]>(
            'GET',
            `/admin/lifecycle/legal-holds${qs({ tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: PlaceLegalHoldRequest, tenantId?: string) =>
          request<LegalHold>('POST', `/admin/lifecycle/legal-holds${qs({ tenant_id: tenantId })}`, body),
        release: (id: string, body: ReleaseLegalHoldRequest, tenantId?: string) =>
          request<Record<string, unknown>>(
            'POST',
            `/admin/lifecycle/legal-holds/${id}:release${qs({ tenant_id: tenantId })}`,
            body,
          ),
      },
      // Storage admin (Phase 7-8): cross-tenant quota summary + adjust + the
      // orphan reconcile report archive.  All four go through the
      // require_super_admin_bff guard and the BFF attaches the scope header.
      storageQuotas: {
        summary: () =>
          request<StorageQuotaSummaryResponse>(
            'GET',
            '/admin/lifecycle/storage-quotas/summary',
          ),
        patch: (
          tenantId: string,
          bucketPurpose: BucketPurpose,
          body: StorageQuotaPatch,
        ) =>
          request<StorageQuotaSummaryItem>(
            'PATCH',
            `/admin/lifecycle/storage-quotas/${encodeURIComponent(tenantId)}/${encodeURIComponent(bucketPurpose)}`,
            body,
          ),
      },
      orphanReports: {
        list: (params?: { limit?: number; before_id?: string }) =>
          request<OrphanReportListResponse>(
            'GET',
            `/admin/lifecycle/orphan-reports${qs(params)}`,
          ),
        get: (reportId: string) =>
          request<OrphanReport>(
            'GET',
            `/admin/lifecycle/orphan-reports/${encodeURIComponent(reportId)}`,
          ),
      },
    },
    // Admin metrics (→ Prometheus via BFF).  Today only the storage-admin
    // trend chart uses this; new chart surfaces should add a sibling entry.
    metrics: {
      getStorageQuotaSeries: (params: {
        range: string;
        step: string;
        purpose?: BucketPurpose;
        tenant_id?: string;
      }) =>
        request<PromQueryRangeResponse>(
          'GET',
          `/admin/metrics/storage-quotas/series${qs(params)}`,
        ),
    },
    // Large-attachment entitlement (→ foundation lifecycle): two-layer
    // boolean admission (tenant switch + per-user allowlist). Tenant-scoped via
    // the optional tenant_id query override. The user-allows list may arrive
    // either as {items:[...]} (current) or a bare array — both are unwrapped.
    largeAttachment: {
      tenantSetting: (tenantId?: string) =>
        request<LargeAttachmentTenantSetting>(
          'GET',
          `/admin/large-attachment/tenant-setting${qs({ tenant_id: tenantId })}`,
        ),
      setTenantSetting: (enabled: boolean, tenantId?: string) =>
        request<LargeAttachmentTenantSetting>(
          'PUT',
          `/admin/large-attachment/tenant-setting${qs({ tenant_id: tenantId })}`,
          { enabled },
        ),
      userAllows: (tenantId?: string) =>
        request<{ items: LargeAttachmentUserAllow[] } | LargeAttachmentUserAllow[]>(
          'GET',
          `/admin/large-attachment/user-allows${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      setUserAllow: (userId: string, allowed: boolean, tenantId?: string) =>
        request<LargeAttachmentUserAllow>(
          'PUT',
          `/admin/large-attachment/user-allows${qs({ tenant_id: tenantId })}`,
          { user_id: userId, allowed },
        ),
      removeUserAllow: (userId: string, tenantId?: string) =>
        request<{ deleted: boolean; user_id: string }>(
          'DELETE',
          `/admin/large-attachment/user-allows/${encodeURIComponent(userId)}${qs({ tenant_id: tenantId })}`,
        ),
      subjectAllows: (tenantId?: string) =>
        request<{ items: LargeAttachmentSubjectAllow[] } | LargeAttachmentSubjectAllow[]>(
          'GET',
          `/admin/large-attachment/subject-allows${qs({ tenant_id: tenantId })}`,
        ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
      setSubjectAllow: (subjectType: LargeAttachmentSubjectType, subjectId: string, allowed: boolean, tenantId?: string) =>
        request<LargeAttachmentSubjectAllow>(
          'PUT',
          `/admin/large-attachment/subject-allows${qs({ tenant_id: tenantId })}`,
          { subject_type: subjectType, subject_id: subjectId, allowed },
        ),
      removeSubjectAllow: (subjectType: LargeAttachmentSubjectType, subjectId: string, tenantId?: string) =>
        request<{ deleted: boolean; subject_type: LargeAttachmentSubjectType; subject_id: string }>(
          'DELETE',
          `/admin/large-attachment/subject-allows/${encodeURIComponent(subjectType)}/${encodeURIComponent(subjectId)}${qs({ tenant_id: tenantId })}`,
        ),
    },
    // Data protection (→ foundation-data-protection): masking / residency /
    // cross-border / destruction / consent. Tenant-scoped.
    dataProtection: {
      maskingRules: (tenantId?: string) =>
        request<MaskingRulesResponse>('GET', `/admin/data-protection/masking-rules${qs({ tenant_id: tenantId })}`),
      dataResidency: (tenantId?: string) =>
        request<DataResidency>('GET', `/admin/data-protection/data-residency${qs({ tenant_id: tenantId })}`),
      crossBorderCheck: (body: CrossBorderCheckRequest, tenantId?: string) =>
        request<CrossBorderResult>('POST', `/admin/data-protection/cross-border/check${qs({ tenant_id: tenantId })}`, body),
      destruction: {
        get: (id: string, tenantId?: string) =>
          request<DestructionRequest>('GET', `/admin/data-protection/destruction/${id}${qs({ tenant_id: tenantId })}`),
        create: (body: CreateDestructionRequest, tenantId?: string) =>
          request<DestructionRequest>('POST', `/admin/data-protection/destruction${qs({ tenant_id: tenantId })}`, body),
        approve: (id: string, tenantId?: string) =>
          request<DestructionRequest>('POST', `/admin/data-protection/destruction/${id}/approve${qs({ tenant_id: tenantId })}`),
      },
      consent: {
        list: (userId?: string, tenantId?: string) =>
          request<{ items: ConsentRecord[] } | ConsentRecord[]>(
            'GET',
            `/admin/data-protection/consent${qs({ user_id: userId, tenant_id: tenantId })}`,
          ).then((r) => (Array.isArray(r) ? r : r.items ?? [])),
        create: (body: RecordConsentRequest, tenantId?: string) =>
          request<ConsentRecord>('POST', `/admin/data-protection/consent${qs({ tenant_id: tenantId })}`, body),
      },
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

  schedules: {
    list: () => request<CronJob[]>('GET', '/schedules'),
    create: (body: CronJobCreate) => request<CronJob>('POST', '/schedules', body),
    update: (id: string, body: CronJobUpdate) =>
      request<CronJob>('PATCH', `/schedules/${id}`, body),
    remove: (id: string) => request<void>('DELETE', `/schedules/${id}`),
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
