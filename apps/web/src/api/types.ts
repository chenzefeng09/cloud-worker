export interface Session {
  id: string;
  tenant_id: string;
  user_id: string;
  title: string | null;
  status: 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ERROR' | 'ARCHIVED';
  current_run_id: string | null;
  metadata?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

/** One entry in a TOOL message's tool_calls array. */
export interface ToolCallEntry {
  id: string;
  name: string;
  /** Parsed input object (may be empty if args couldn't be parsed). */
  input: Record<string, unknown>;
  /** Tool output text, present after the tool has returned. */
  result?: string;
  /** Execution state, e.g. "success" or "error". */
  state?: string;
}

/** Minimal attachment metadata embedded in a user message. */
export interface MessageAttachment {
  id: string;
  filename: string;
  mime?: string;
  size?: number;
}

export interface Message {
  id: string;
  session_id: string;
  /** The server stores lowercase ("user"/"assistant"/"tool"); normalise with toUpperCase() in components. */
  role: 'USER' | 'ASSISTANT' | 'SYSTEM' | 'TOOL' | string;
  content: string;
  tool_calls: ToolCallEntry[] | null;
  run_id: string | null;
  sequence: number;
  created_at: string;
  /**
   * Attachment metadata for user messages. Populated by the orchestrator
   * when the message was created with attachment_ids.
   */
  attachments?: MessageAttachment[];
  /**
   * True only while the message is an in-flight streaming placeholder.
   * Never persisted to the server — the field disappears once the stream
   * finalises or the session is re-hydrated from the DB.
   */
  streaming?: boolean;

  /**
   * "thinking" marks a transient thinking block (ASSISTANT role with
   * thinking text).  Used only by the interleaved streaming pipeline.
   */
  blockKind?: 'text' | 'tool' | 'thinking';

  /**
   * The run that produced this temporary block.  Used by
   * clearStreamingBlocks to remove all temp blocks for a run at once.
   */
  streamRunId?: string;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export interface LoginResponse {
  access_token: string;
  refresh_token: string;
  user_id: string;
  tenant_id: string;
  email: string;
  display_name: string;
}

export interface UserProfile {
  id: string;
  email: string;
  display_name: string;
  tenant_id: string;
  roles: string[];
}

export interface SSEEvent {
  event: string;
  data: string;
}

// ─── Library ────────────────────────────────────────────────────────────────

export type McpTransport = 'http' | 'stdio';

export interface McpServer {
  id: string;
  tenant_id: string;
  name: string;
  transport: McpTransport;
  endpoint: string | null;
  command: string | null;
  env: Record<string, string>;
  scopes: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface Skill {
  id: string;
  tenant_id: string;
  name: string;
  description: string | null;
  owner: string | null;
  visibility: 'private' | 'tenant' | 'public';
  created_at: string;
}

/** A skill in the user's toolbox — Skill fields + install metadata. */
export interface InstalledSkill extends Skill {
  enabled_by_default: boolean;
  installed_at: string;
}

export interface SkillVersion {
  id: string;
  skill_id: string;
  version: string;
  manifest: string | null;
  sha256: string | null;
  created_at: string;
}

export interface IngestResult {
  document_id: string;
  status: string;
  chunks_created: number;
}

export type DocumentStatus = 'PENDING' | 'INGESTING' | 'READY' | 'FAILED';

export interface KnowledgeDocument {
  id: string;
  tenant_id: string;
  title: string;
  mime: string;
  source_uri: string | null;
  status: DocumentStatus;
  uploaded_at: string;
  chunk_count: number;
}

export interface ListPage<T> {
  items: T[];
  total: number;
}

// ─── Apps ─────────────────────────────────────────────────────────────────────

export type AppVisibility = 'private' | 'tenant';
export type AppVersionStatus = 'draft' | 'pending_review' | 'published' | 'rejected';
export type InputFieldType = 'text' | 'textarea' | 'select' | 'multiselect' | 'file';

/** Per-run sampling overrides applied at the gateway model layer. */
export interface SamplingParams {
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
}

/** A single option for select / multiselect input fields. */
export interface InputFieldOption {
  value: string;
  label: string;
}

/** Upload constraints for a `file`-type input field. */
export interface InputFieldFileConstraint {
  accept: string[];
  max_size_bytes: number;
  multiple: boolean;
}

/** One field in an App's input form (ordered within `input_schema`). */
export interface InputField {
  key: string;
  type: InputFieldType;
  label: string;
  help?: string;
  required: boolean;
  default?: string | string[] | null;
  options?: InputFieldOption[];
  file?: InputFieldFileConstraint;
}

/** An immutable harness version of an App (frozen at publish time). */
export interface AppVersion {
  id: string;
  app_id: string;
  tenant_id: string;
  version: string;
  status: AppVersionStatus;
  system_prompt: string;
  user_prompt_template: string;
  model: string;
  reasoning_effort?: string | null;
  sampling_params: SamplingParams;
  forced_skills: string[];
  forced_mcp_server_ids: string[];
  input_schema: InputField[];
  submitted_by?: string | null;
  submitted_at?: string | null;
  reviewed_by?: string | null;
  reviewed_at?: string | null;
  review_note?: string | null;
  created_by?: string | null;
  created_at: string;
}

/** Request body for creating a draft version (or the App's initial draft). */
export interface AppVersionCreate {
  /** Deprecated request field; app-registry now assigns monotonically increasing versions. */
  version?: string | null;
  system_prompt: string;
  user_prompt_template: string;
  model: string;
  reasoning_effort?: string | null;
  sampling_params: SamplingParams;
  forced_skills: string[];
  forced_mcp_server_ids: string[];
  input_schema: InputField[];
}

/** Stable App identity + its versions. */
export interface App {
  id: string;
  tenant_id: string;
  name: string;
  description?: string | null;
  icon?: string | null;
  author_id: string;
  visibility: AppVisibility;
  current_version_id?: string | null;
  created_at: string;
  updated_at: string;
  versions?: AppVersion[];
}

/** Request body for creating an App + its initial draft version (atomic). */
export interface AppCreate {
  name: string;
  description?: string | null;
  icon?: string | null;
  visibility: AppVisibility;
  version: AppVersionCreate;
}

/** Partial App-level field edits (does not touch versions). */
export interface AppUpdate {
  name?: string;
  description?: string | null;
  icon?: string | null;
  visibility?: AppVisibility;
}

/** One row in the admin review queue: a pending version + its App context. */
export interface AppReviewItem {
  version: AppVersion;
  app_id: string;
  app_name: string;
  author_id: string;
}

/** One resolved input value recorded in an App run snapshot. */
export interface AppRunSnapshotInput {
  key: string;
  label: string;
  type: string;
  value: unknown;
}

/** Immutable record of the harness + inputs that produced an App run. */
export interface AppRunSnapshot {
  app_id: string;
  app_name: string;
  app_version: string;
  app_version_id: string;
  model: string;
  reasoning_effort?: string | null;
  sampling_params: SamplingParams;
  forced_skills: string[];
  forced_mcp_server_ids?: string[];
  system_prompt?: string;
  user_prompt_template?: string;
  rendered_task_preview: string;
  inputs: AppRunSnapshotInput[];
  snapshot_at: string;
}

/** Result of triggering an App run. */
export interface AppRunResult {
  session_id: string;
  run_id: string;
  status: string;
}

// ─── Attachments ────────────────────────────────────────────────────────────

export type AttachmentKind = 'input' | 'output';

export interface Attachment {
  id: string;
  session_id: string;
  filename: string;
  mime: string;
  size_bytes: number;
  blob_key: string;
  kind: AttachmentKind;
  run_id: string | null;
  created_at: string;
}

export interface SessionFile extends Attachment {
  // Alias for clarity in UI contexts
}

// ─── Admin ───────────────────────────────────────────────────────────────────

export type TenantTier = 'FREE' | 'PRO' | 'BUSINESS' | 'ENTERPRISE' | 'DEDICATED';
export type TenantStatus = 'ACTIVE' | 'SUSPENDED' | 'TERMINATING' | 'TERMINATED';

export interface Tenant {
  id: string;
  name: string;
  tier: TenantTier;
  status: TenantStatus;
  created_at: string;
}

export interface CreateTenantRequest {
  name: string;
  tier: TenantTier;
  admin_email: string;
}

export interface PatchTenantRequest {
  tier?: TenantTier;
  status?: TenantStatus;
}

export interface AdminUser {
  id: string;
  email: string;
  display_name: string;
  tenant_id: string;
  roles: string[];
  created_at?: string;
}

// ─── Audit / Billing (proxy DTOs, BFF may not expose all) ────────────────────

/**
 * Audit event as returned by foundation-audit (`/events:query`).
 * Field names mirror the upstream schema (event_id / occurred_at / event_type …),
 * not a BFF-renamed shape.
 */
export interface AuditEvent {
  event_id: string;
  tenant_id: string;
  occurred_at: string;
  event_type: string;
  severity?: string;
  outcome?: string;
  actor_kind?: string;
  actor_user_id?: string | null;
  actor_api_key_id?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  resource_path?: string | null;
  payload?: Record<string, unknown> | null;
}

export interface AuditQueryRequest {
  /** Required by upstream: the audit verifier scopes every query to a tenant. */
  tenant_id?: string;
  /** Upstream filters by event_type (e.g. "foundation.tenant.create"). */
  event_type?: string;
  /** Multi-select filter: restrict to any of these event types. */
  event_types?: string[];
  from?: string;
  to?: string;
  limit?: number;
  cursor?: string;
}

export interface BillingPlan {
  id: string;
  name: string;
  tier: TenantTier;
  limits: Record<string, number>;
}

export interface QuotaInfo {
  tenant_id: string;
  plan: BillingPlan | null;
  usage: Record<string, number>;
}

// ─── LLM Model Management ────────────────────────────────────────────────────

export type ProviderType = 'openai_compatible' | 'anthropic' | 'vllm' | 'ollama' | 'bedrock' | 'azure' | 'zhipu' | 'deepseek' | 'moonshot' | 'minimax' | 'aliyun' | 'baidu' | 'tencent';

/** Model type categories following Dify conventions. */
export type ModelType = 'llm' | 'text_embedding' | 'rerank';

/** Mapping from model type to list of model IDs. */
export type ModelTypeMap = Partial<Record<ModelType, string[]>>;

export interface ModelSetting {
  model_type: ModelType;
  context_length?: number;
  max_tokens?: number;
  vision_support?: boolean;
  structured_output?: boolean;
}

/** Mapping from model ID to its specific settings. */
export type ModelSettingsMap = Record<string, ModelSetting>;

/** A registered model provider with credentials and model catalogue. */
export interface ModelProvider {
  id: string;
  name: string;
  kind: ProviderType;
  base_url: string;
  models: string[] | null;
  default_model: string;
  capabilities: Record<string, unknown>;
  enabled: boolean;
  created_at: string | null;
}

/**
 * Sanitized provider catalogue entry returned by the non-admin
 * GET /llm/models endpoint. A subset of ModelProvider — credentials and
 * base_url are never present — structurally compatible with the
 * providerModels helpers (which only read id/name/models/capabilities).
 */
export type ModelCatalogProvider = Pick<
  ModelProvider,
  'id' | 'name' | 'kind' | 'enabled' | 'models' | 'capabilities'
>;

export interface RegisterProviderRequest {
  name: string;
  api_base: string;
  api_key: string;
  provider_type: ProviderType;
  models: string[];
  model_type_map: ModelTypeMap;
  model_settings: ModelSettingsMap;
}

export interface UpdateProviderRequest {
  enabled?: boolean;
  name?: string;
  models?: string[];
  default_model?: string;
  model_type_map?: ModelTypeMap;
  model_settings?: ModelSettingsMap;
}

// ─── LLM Probe (credential validation) ──────────────────────────────────────

export interface ProbeRequest {
  url: string;
  api_key: string;
  provider_type?: ProviderType;
}

/** Each capability result is boolean (true/false) or "unknown" or an error string. */
export type ProbeCapabilityResult = boolean | 'unknown' | string;

export interface ProbeResults {
  list_models: ProbeCapabilityResult;
  model_list?: string[];
  model_count?: string;
  function_calling: ProbeCapabilityResult;
  streaming: ProbeCapabilityResult;
  /** True when the reported context window is >= 32768 tokens. */
  context_window_32k: ProbeCapabilityResult;
  /** Raw context window value as string, or "unknown" if not advertised. */
  context_window_value: string;
  parallel_tool_calls: ProbeCapabilityResult;
}

export interface ProbeReport {
  url: string;
  provider_type: string;
  results: ProbeResults;
}

// ─── Runs ────────────────────────────────────────────────────────────────────

export interface TokenUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface DailyTokenUsage {
  day: string;   // "YYYY-MM-DD"
  tokens: number;
}

export interface DailyTokenUsageResponse {
  days: DailyTokenUsage[];
}

export type RunStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';

export interface Run {
  id: string;
  session_id: string;
  sandbox_id: string | null;
  status: RunStatus;
  task_description: string | null;
  result_summary: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  /**
   * Immutable snapshot of the App harness + resolved inputs, present only on
   * runs triggered via `POST /apps/{id}/run`. The orchestrator returns it in
   * get_run/list_runs. Used by AppResultPage to render the read-only form +
   * model/version that produced the output.
   */
  app_snapshot?: AppRunSnapshot | null;
}

// ─── Run State ───────────────────────────────────────────────────────────────

export interface RunState {
  run_id: string;
  plan: string;
  step: number;
  status: string;
  cursor: string;
}

// ─── Notification Preferences ────────────────────────────────────────────────

export interface NotifPreferenceItem {
  event_key: string;
  label: string;
  sub: string;
  email_enabled: boolean;
  push_enabled: boolean;
}

// ─── Active login sessions (identity) ────────────────────────────────────────

export interface ActiveSession {
  id: string;
  ip: string | null;
  user_agent: string | null;
  device: string;
  last_seen_at: string | null;
  issued_at: string;
  current: boolean;
}

// ─── MFA ─────────────────────────────────────────────────────────────────────

export type MfaKind = 'TOTP' | 'SMS_OTP' | 'WEBAUTHN' | 'PUSH';

export interface MfaMethod {
  id: string;
  kind: MfaKind;
  label: string | null;
  enrolled_at: string;
  last_used_at: string | null;
}

// ─── Fleet ──────────────────────────────────────────────────────────────────

export interface PoolTemplateInfo {
  idle: number;
  total: number;
}

export interface PoolSnapshot {
  total: number;
  by_template: Record<string, PoolTemplateInfo>;
  // Tiered warm-pool breakdown keyed by memory tier (e.g. "small", "large").
  // Each tier reports idle/total bucket counts independently.
  by_memory_tier?: Record<string, PoolTemplateInfo>;
}

// ─── Large-attachment entitlement (→ foundation lifecycle) ───────────────────

export interface LargeAttachmentTenantSetting {
  id?: string | null;
  tenant_id?: string;
  large_attachment_enabled: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface LargeAttachmentUserAllow {
  id?: string | null;
  tenant_id?: string;
  user_id: string;
  allowed: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export type LargeAttachmentSubjectType = 'USER' | 'TEAM' | 'ORG_UNIT';

export interface LargeAttachmentSubjectAllow {
  id?: string | null;
  tenant_id?: string;
  subject_type: LargeAttachmentSubjectType;
  subject_id: string;
  allowed: boolean;
  updated_at?: string | null;
  updated_by?: string | null;
}

export interface SandboxItem {
  // The scheduler returns `container_id`; keep `id` optional for other sources.
  container_id?: string;
  id?: string;
  name?: string;
  image?: string;
  status: string;
  tenant_id: string;
  session_id?: string;
  run_id?: string;
  created_at?: string;
}

// ─── Fleet Topology ──────────────────────────────────────────────────────────

export type TopologyNodeType = 'worker' | 'task' | 'mcp';

/** A single node in the fleet topology graph.
 *  x/y are NOT present on the wire — the frontend layout algorithm adds them.
 */
export interface TopologyNode {
  id: string;
  type: TopologyNodeType;
  label: string;
  /** Worker: container status ('running', 'idle', etc.)
   *  Task:   'active'
   *  Mcp:    'enabled' | 'disabled'
   */
  status: string;
}

/** A node with layout coordinates computed by the frontend. */
export interface TopologyNodeWithPos extends TopologyNode {
  x: number;
  y: number;
}

export interface TopologyEdge {
  from: string;
  to: string;
  kind: 'worker→task' | 'worker→mcp';
}

export interface TopologyGraph {
  nodes: TopologyNode[];
  edges: TopologyEdge[];
}

// ─── API Keys ────────────────────────────────────────────────────────────────

/** List item — never contains the raw key or hash. */
export interface ApiKey {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

/** Creation response — includes the raw key exactly once. */
export interface ApiKeyCreated extends ApiKey {
  /** The raw key — display once and discard; never retrievable again. */
  key: string;
}

// ─── Model Defaults & User Preferences ──────────────────────────────────────

/** Purpose categories for model defaults / preferences. */
export type ModelPurpose = 'llm' | 'embedding' | 'rerank' | 'tts' | 'stt';

/** Admin-configured default model for a given purpose. */
export interface TenantDefaultModel {
  id: string;
  purpose: ModelPurpose;
  model_id: string;
  provider_id: string;
  provider_name?: string;
  updated_at: string;
  created_by?: string;
}

/** Per-user model preference for a given purpose. */
export interface UserModelPreference {
  id: string;
  purpose: ModelPurpose;
  model_id: string;
  provider_id: string;
  provider_name?: string;
  updated_at: string;
}

/** Resolved model for a purpose — indicates which layer provided it. */
export interface ResolvedDefault {
  purpose: ModelPurpose;
  model_id: string;
  provider_id: string;
  source: 'user' | 'tenant' | 'system';
}

// ─── Admin Console v2 ─────────────────────────────────────────────────────────
//
// DTOs for the enterprise admin console (RBAC / org / billing / audit / flags /
// platform governance). Modelled loosely against the saas-foundation OpenAPI
// contracts and agent-platform service schemas — most fields are optional since
// the BFF re-shapes upstream payloads and may omit some on cross-tenant calls.

// ─── RBAC (→ foundation-authz) ────────────────────────────────────────────────

export type RoleScope = 'TENANT' | 'ORG_UNIT' | 'TEAM' | 'RESOURCE';
export type BindingSubjectType = 'USER' | 'TEAM' | 'ORG_UNIT';

/** A role: a named bundle of permission strings, scoped to a level. */
export interface AdminRole {
  id: string;
  tenant_id?: string;
  name: string;
  description?: string | null;
  scope?: RoleScope;
  permissions?: string[];
  /** System-builtin roles cannot be edited or deleted. */
  is_system?: boolean;
  created_at?: string;
  updated_at?: string;
}

export interface CreateRoleRequest {
  name: string;
  description?: string;
  scope: RoleScope;
  permissions: string[];
}

export interface UpdateRoleRequest {
  description?: string | null;
  permissions?: string[];
}

/** One entry in the permission catalogue (the universe of grantable actions). */
export interface PermissionDefinition {
  name: string;
  description?: string;
  owner_service?: string;
  /** true → the permission must route through OPA policy evaluation. */
  requires_policy?: boolean;
}

/** A grant of a role to a subject (user / team / org-unit), optionally scoped. */
export interface RoleBinding {
  id: string;
  tenant_id?: string;
  subject_type?: BindingSubjectType;
  subject_id?: string;
  role_id?: string;
  /** Restricts the role's scope to a single resource id. */
  scope_resource_id?: string | null;
  granted_by?: string;
  granted_at?: string;
  /** JIT elevation: auto-revoked at this time when set. */
  expires_at?: string | null;
  reason?: string | null;
}

export interface CreateBindingRequest {
  subject_type: BindingSubjectType;
  subject_id: string;
  role_id: string;
  scope_resource_id?: string;
  expires_at?: string;
  reason?: string;
}

// ─── Organisation structure (→ foundation-org) ────────────────────────────────

/** A node in the org-unit tree. `path` is a materialised path like /eng/infra. */
export interface OrgUnit {
  id: string;
  tenant_id?: string;
  parent_id?: string | null;
  name: string;
  path?: string;
  depth?: number;
  descendants_count?: number;
  members_count?: number;
  created_at?: string;
  updated_at?: string;
}

export interface CreateOrgUnitRequest {
  name: string;
  parent_id?: string;
}

export interface UpdateOrgUnitRequest {
  name?: string;
}

export interface MoveOrgUnitRequest {
  /** New parent org-unit id; null moves the node to the root. */
  new_parent_id: string | null;
}

/** A team — a flat membership group, optionally attached to an org-unit. */
export interface Team {
  id: string;
  tenant_id?: string;
  org_unit_id?: string | null;
  name: string;
  description?: string | null;
  members_count?: number;
  created_at?: string;
}

export interface CreateTeamRequest {
  name: string;
  code: string;
  org_unit_id: string;
  description?: string;
}

export type TeamRole = 'OWNER' | 'MAINTAINER' | 'MEMBER' | 'VIEWER';

/** A membership row linking a user to a team with a team-scoped role. */
export interface TeamMember {
  team_id?: string;
  user_id: string;
  role?: TeamRole;
  joined_at?: string;
}

export interface AddTeamMemberRequest {
  user_id: string;
  role?: TeamRole;
}

// ─── User management actions (→ foundation-identity) ──────────────────────────

export interface InviteUserRequest {
  email: string;
  display_name?: string;
  roles?: string[];
  send_invite_email?: boolean;
}

// ─── Billing & quota (writeable, → foundation-billing) ────────────────────────

export type PlanKind = 'FREE' | 'PRO' | 'BUSINESS' | 'ENTERPRISE' | 'CUSTOM';
export type BillingCycle = 'MONTHLY' | 'YEARLY' | 'ONE_TIME';
export type SubscriptionStatus = 'TRIAL' | 'ACTIVE' | 'PAST_DUE' | 'CANCELED' | 'SUSPENDED';

/** A purchasable plan from the billing catalogue. */
export interface BillingPlanDef {
  id: string;
  name: string;
  description?: string;
  kind?: PlanKind;
  billing_cycle?: BillingCycle;
  /** Price in the smallest currency unit (cents). */
  price_amount?: number;
  currency?: string;
  /** Per-dimension included quota allotments. */
  included_quotas?: Record<string, number>;
  features?: string[];
  active?: boolean;
}

/** A tenant's current subscription to a plan. */
export interface TenantSubscription {
  id: string;
  tenant_id?: string;
  plan_id: string;
  status?: SubscriptionStatus;
  provider?: string;
  provider_subscription_id?: string | null;
  current_period_start?: string;
  current_period_end?: string;
  trial_ends_at?: string | null;
  canceled_at?: string | null;
  grace_period_ends_at?: string | null;
  created_at?: string;
}

export interface ChangePlanRequest {
  /** The subscription to mutate (BFF may resolve current when omitted). */
  subscription_id?: string;
  new_plan_id: string;
  proration?: boolean;
  /** Optional target tenant for cross-tenant super-admin operations. */
  tenant_id?: string;
}

export interface CancelSubscriptionRequest {
  subscription_id?: string;
  immediate?: boolean;
  reason?: string;
  tenant_id?: string;
}

export interface CreatePlanRequest {
  name: string;
  tier: string;
  quotas: Record<string, number>;
  price_monthly_usd: number;
}

export interface CreateSubscriptionRequest {
  plan_id: string;
  mode?: string;
}

/** One bucket in a usage time-series response. */
export interface UsageSeriesPoint {
  ts?: string;
  amount?: number;
}

/** Aggregated usage query result (a series plus a total). */
export interface UsageRecord {
  dimension?: string;
  series?: UsageSeriesPoint[];
  total?: number;
}

// ─── Audit enhancements (→ foundation-audit) ──────────────────────────────────

export interface AuditChainVerifyRequest {
  /**
   * Target tenant — drives the forwarded X-Tenant-Id on the BFF and is
   * required by the upstream verifier (cross-tenant checks are blocked).
   */
  tenant_id?: string;
  /** Optional — restrict verification to a single service's events. */
  service?: string;
  /** Optional — start of the event range (event_id for keyset scan). */
  from_event_id?: string;
  /** Optional — max number of events to check (upstream default 1000). */
  limit?: number;
}

/**
 * Result of verifying the integrity of a hash-chain segment.
 * Mirrors foundation-audit ``VerifyIntegrityResponse`` plus the
 * BFF-injected ``effective_tenant_id``.
 */
export interface AuditChainVerifyResult {
  valid?: boolean;
  events_checked?: number;
  first_break_event_id?: string | null;
  signatures_verified?: number;
  signatures_failed?: number;
  tsa_present?: number;
  message?: string;
  /** Tenant the BFF actually scoped the verification to. */
  effective_tenant_id?: string;
}

export type AuditExportFormat = 'CSV' | 'PDF' | 'NDJSON';
export type AuditExportPurpose = 'REGULATORY_AUDIT' | 'INTERNAL_AUDIT' | 'INCIDENT' | 'LEGAL_DISCOVERY';
export type AuditExportStatus = 'QUEUED' | 'RUNNING' | 'READY' | 'FAILED';

export interface AuditExportRequest {
  format: AuditExportFormat;
  time_start: string;
  time_end: string;
  filter?: Record<string, unknown>;
  include_signatures?: boolean;
  purpose?: AuditExportPurpose;
  tenant_id?: string;
}

/** An async audit export job — poll getExport(id) until status is READY. */
export interface AuditExport {
  export_id: string;
  status?: AuditExportStatus;
  download_url?: string | null;
  signature_url?: string | null;
  expires_at?: string | null;
}

// ─── Feature flags (→ foundation-feature-flag) ────────────────────────────────

/** A feature flag view — the read model surfaced by the admin console. */
export interface FeatureFlag {
  key: string;
  name?: string;
  description?: string | null;
  enabled: boolean;
  default_value?: boolean;
  fail_open?: boolean;
  user_overridable?: boolean;
  user_override?: boolean | null;
  owner_team?: string | null;
  tags?: Record<string, unknown> | null;
  kill_switch_active?: boolean;
  unleash_synced_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** Body for PUT /admin/flags/{key} — partial flag upsert. */
export interface FeatureFlagUpdate {
  enabled?: boolean;
  name?: string;
  description?: string | null;
  default_value?: boolean;
  fail_open?: boolean;
  user_overridable?: boolean;
  owner_team?: string | null;
  tags?: Record<string, unknown> | null;
}

export interface UserFlagOverride {
  flag_key: string;
  enabled: boolean;
  updated_at?: string | null;
}

export interface KillSwitchRequest {
  /** true → trip the kill switch (force-off); false → release it. */
  active: boolean;
  reason?: string;
}

// ─── Platform governance: Skills (→ skill-store) ──────────────────────────────

/** Admin view of a stored skill (governance/listing — not the toolbox view). */
export interface AdminSkill {
  id: string;
  tenant_id?: string;
  name: string;
  description?: string | null;
  owner?: string | null;
  visibility?: 'private' | 'tenant' | 'public';
  created_at?: string;
}

/** A stored skill version with its blob + manifest metadata. */
export interface AdminSkillVersion {
  id: string;
  skill_id: string;
  version: string;
  manifest?: Record<string, unknown>;
  blob_url?: string;
  sha256?: string;
  uploaded_by?: string | null;
  uploaded_at?: string;
}

// ─── Platform governance: MCP servers (→ mcp-registry) ────────────────────────
//
// The wire shape matches the existing `McpServer` type above; admin
// register / patch bodies are declared here.

export interface RegisterMcpServerRequest {
  name: string;
  transport: McpTransport;
  endpoint?: string | null;
  command?: string | null;
  env?: Record<string, string>;
  scopes?: string[];
  enabled?: boolean;
}

export interface PatchMcpServerRequest {
  endpoint?: string | null;
  command?: string | null;
  env?: Record<string, string> | null;
  scopes?: string[] | null;
  enabled?: boolean | null;
}

// ─── Platform governance: Ops / runs / sandboxes ──────────────────────────────
//
// Ops listing reuses the orchestrator run shape (`Run`), the sandbox-pool
// snapshot (`PoolSnapshot`) and sandbox rows (`SandboxItem`) declared above.
// Aliases are exported under the spec's names for page-level clarity.

/** A run row in the ops dashboard (orchestrator-shaped). */
export type OpsRun = Run;

/** A live sandbox container row (scheduler / sandbox-pool shaped). */
export type SandboxInfo = SandboxItem;

/** Per-day token usage point for the ops usage chart. */
export type DailyUsage = DailyTokenUsage;

// ─── Notification center (→ foundation-notification) ──────────────────────────
//
// Wire shapes are kept intentionally loose — list payloads vary by upstream and
// pages render defensively.

export interface NotificationTemplate {
  id?: string;
  key?: string;
  name?: string;
  event_type?: string;
  channel?: string;
  subject?: string;
  /** Real foundation field: the rendered subject line template. */
  subject_template?: string;
  /** Real foundation field: whether the template is active. */
  enabled?: boolean;
  body?: string;
  locale?: string;
  created_at?: string;
}

/** POST /admin/notification/send body (foundation SendRequest). */
export interface NotificationSendRequest {
  event_type: string;
  recipients: string[];
  channel?: string;
  template?: string | null;
  context?: Record<string, unknown>;
  priority?: string;
}

// ─── API platform (→ foundation-api-platform) ─────────────────────────────────

export interface PlatformApiKey {
  id?: string;
  name?: string;
  scopes?: string[];
  rate_limit_rpm?: number;
  /** Returned only on creation. */
  api_key?: string;
  created_at?: string;
}

export interface CreatePlatformApiKeyRequest {
  name: string;
  scopes: string[];
  ip_whitelist?: string[] | null;
  rate_limit_rpm?: number;
  expires_in_days?: number | null;
}

export interface OAuthApp {
  id?: string;
  name?: string;
  client_id?: string;
  scopes?: string[];
  redirect_uris?: string[];
}

export interface RegisterOAuthAppRequest {
  name: string;
  redirect_uris: string[];
  scopes: string[];
}

export interface Webhook {
  id?: string;
  url?: string;
  event_types?: string[];
  enabled?: boolean;
}

export interface SubscribeWebhookRequest {
  url: string;
  event_types: string[];
  secret?: string | null;
}

// ─── SSO — identity source management (→ foundation-identity) ────────────────

export type SsoKind = 'OIDC' | 'SAML' | 'AD' | 'LDAP' | 'DINGTALK_ENTERPRISE' | 'FEISHU_ENTERPRISE' | 'WECOM';

export type SsoScope = 'TENANT' | 'PLATFORM';

export type SsoMatchType = 'EMAIL_DOMAIN' | 'EQUALS' | 'CONTAINS' | 'REGEX';

export interface SsoProvider {
  id: string;
  kind: SsoKind;
  name: string;
  enabled: boolean;
  config: Record<string, unknown>;
  created_at: string;
  scope?: SsoScope;
  tenant_id?: string | null;
  default_tenant_id?: string | null;
}

export interface CreateSsoProviderRequest {
  kind: SsoKind;
  name: string;
  config: Record<string, unknown>;
}

export interface SsoRoutingRule {
  id: string;
  provider_id: string;
  priority: number;
  match_attribute: string;
  match_type: SsoMatchType;
  match_value: string;
  target_tenant_id: string;
  default_role?: string | null;
  enabled: boolean;
  created_at?: string;
}

export interface CreateSsoRoutingRuleRequest {
  priority?: number;
  match_attribute: string;
  match_type: SsoMatchType;
  match_value: string;
  target_tenant_id: string;
  default_role?: string | null;
  enabled?: boolean;
}

export interface CreatePlatformSsoProviderRequest {
  kind: SsoKind;
  name: string;
  config: Record<string, unknown>;
  scope: SsoScope;
  tenant_id?: string | null;
  default_tenant_id?: string | null;
}

// ─── PAM — privileged access (→ foundation-pam) ───────────────────────────────

export interface PamElevation {
  id?: string;
  user_id?: string;
  reason?: string;
  status?: string;
  duration_minutes?: number;
  requested_at?: string;
  approved_by?: string;
  expires_at?: string | null;
}

export interface PamSession {
  id?: string;
  user_id?: string;
  permissions?: string[];
  expires_at?: string | null;
}

export interface RequestElevationRequest {
  reason: string;
  duration_minutes?: number;
  target_resources?: string[];
}

export interface ApproveElevationRequest {
  justification?: string;
}

// ─── DLP — data-loss prevention (→ foundation-dlp) ────────────────────────────

export type DlpSensitivity = 'L1' | 'L2' | 'L3' | 'L4' | 'L5';

export interface DlpScanRequest {
  content: string;
  resource_type: string;
  resource_id: string;
}

export interface DlpScanResult {
  matches?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface DlpTagRequest {
  resource_type: string;
  resource_id: string;
  sensitivity: DlpSensitivity;
  tags?: string[] | null;
  reason?: string | null;
}

export interface DlpClassification {
  id?: string;
  sensitivity?: string;
  tags?: string[] | null;
  downgradable?: boolean;
  [key: string]: unknown;
}

// Governance read-models (→ foundation-dlp /core/events|classifications|stats).

export interface DlpEvent {
  id: string;
  check_type: string;
  decision: string;
  user_id?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  reason_code?: string | null;
  occurred_at: string;
  detail?: Record<string, unknown> | null;
  request_id?: string | null;
  [key: string]: unknown;
}

export interface DlpEventList {
  items: DlpEvent[];
  next_before: string | null;
}

export interface DlpClassificationRow {
  resource_type: string;
  resource_id: string;
  sensitivity: string;
  tags?: string[] | null;
  reason?: string | null;
  tagged_by?: string | null;
  tagged_at: string;
  downgradable?: boolean;
  [key: string]: unknown;
}

export interface DlpClassificationList {
  items: DlpClassificationRow[];
  next_before: string | null;
}

export interface DlpStats {
  by_decision: Record<string, number>;
  by_check_type: Record<string, number>;
  by_sensitivity: Record<string, number>;
  total_events: number;
}

// ─── Compliance center (→ foundation-compliance) ──────────────────────────────

export interface RopaRecord {
  id?: string;
  service?: string;
  purpose?: string;
  legal_basis?: string;
  data_category?: string;
}

export interface RecordProcessingRequest {
  service: string;
  data_category: string;
  purpose: string;
  legal_basis: string;
  data_subjects: string[];
  recipients: string[];
  retention_days?: number;
  cross_border?: boolean;
  destination_countries?: string[];
}

export interface ComplianceReport {
  id?: string;
  report_type?: string;
  compliance_modes?: string[];
  content?: Record<string, unknown>;
}

export interface GenerateReportRequest {
  report_type: string;
  compliance_modes: string[];
}

export interface DsarRecord {
  id?: string;
  user_id?: string;
  request_type?: string;
  status?: string;
}

export interface RequestDsarRequest {
  user_id: string;
  request_type: string;
  details: string;
}

export interface DpiaRecord {
  id?: string;
  project_name?: string;
  conclusion?: string;
  residual_risks?: string;
}

export interface AssessDpiaRequest {
  project_name: string;
  description: string;
  data_categories: string[];
  risks: Array<Record<string, string>>;
  mitigations: Array<Record<string, string>>;
}

export interface CompleteDpiaRequest {
  conclusion: string;
  residual_risks: string;
}

// ─── Security operations (→ foundation-security-ops) ────────────────────────────

export type VulnSeverity = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW';

export interface VulnSlaDashboard {
  overdue_by_severity?: Record<string, number>;
  by_owner?: Record<string, Record<string, number>>;
  mttr_days?: Record<string, number>;
}

export interface Vulnerability {
  id?: string;
  cve_id?: string;
  title?: string;
  severity?: string;
  cvss_score?: number;
  package_affected?: string;
  status?: string;
  due_at?: string;
  assignee?: string;
  fix_pr_url?: string;
  notes_md?: string;
}

export interface RiskAcceptRequest {
  rationale: string;
  mitigations: string[];
  expires_at?: string | null;
}

export interface SecurityIncident {
  id?: string;
  severity?: string;
  category?: string;
  title?: string;
  status?: string;
  detected_at?: string;
  ic?: string;
  customers_notified?: boolean;
  regulators_notified?: boolean;
}

export interface CreateIncidentRequest {
  severity: string;
  category: string;
  title: string;
  ic?: string | null;
  affected_tenants: string[];
}

export interface ResolveIncidentRequest {
  root_cause: string;
  pir_s3_key: string;
}

export interface ScanRun {
  id?: string;
  type?: string;
  tool?: string;
  target?: string;
  status?: string;
  started_at?: string;
  findings_count?: number;
}

export interface SbomWatch {
  image_ref?: string;
  registered_at?: string;
  last_scanned_at?: string;
  notify?: string[];
  new_cves_count?: number;
}

export interface RegisterSbomWatchRequest {
  image_ref: string;
  notify: string[];
}

export interface SecurityDrill {
  id?: string;
  kind?: string;
  scheduled_at?: string;
  status?: string;
  participants?: string[];
}

export interface ScheduleDrillRequest {
  kind: string;
  scheduled_at: string;
  participants: string[];
  scenario_runbook?: string | null;
}

// ─── Data lifecycle (→ foundation-lifecycle) ────────────────────────────────────

export interface RecycleBinItem {
  id?: string;
  resource_type?: string;
  resource_id?: string;
  deleted_at?: string;
  expires_at?: string;
}

export interface RecycleBinPage {
  items?: RecycleBinItem[];
  next_cursor?: string | null;
}

export interface SoftDeleteRequest {
  resource_type_id: string;
  resource_id: string;
  resource_name?: string | null;
}

export interface HardDeleteRequest {
  destroyed_by: string;
  witness_user_id?: string | null;
}

export interface SweepResult {
  destroyed?: number;
  [key: string]: unknown;
}

export interface LegalHold {
  id?: string;
  resource_type?: string;
  resource_id?: string;
  reason?: string;
  placed_at?: string;
}

export interface PlaceLegalHoldRequest {
  resource_type_id: string;
  resource_id: string;
  reason: string;
  case_reference?: string | null;
}

export interface ReleaseLegalHoldRequest {
  released_by: string;
}

// ─── Data protection (→ foundation-data-protection) ─────────────────────────────

export interface MaskingRule {
  name?: string;
  method?: string;
}

export interface MaskingRulesResponse {
  rules?: MaskingRule[];
}

export interface DataResidency {
  primary_region?: string;
  restricted_destinations?: string[];
  policy?: string;
}

export interface CrossBorderCheckRequest {
  destination_country: string;
  data_categories: string[];
}

export interface CrossBorderResult {
  allowed?: boolean;
  decision?: string;
  risks?: string[];
  required_actions?: string[];
}

export interface DestructionRequest {
  id?: string;
  status?: string;
  resource_ids?: string[];
  method?: string;
  [key: string]: unknown;
}

export interface CreateDestructionRequest {
  resource_ids: string[];
  method: string;
}

export interface ConsentRecord {
  id?: string;
  user_id?: string;
  consent_type?: string;
  purpose?: string;
  granted?: boolean;
  recorded_at?: string;
}

export interface RecordConsentRequest {
  user_id: string;
  consent_type: string;
  purpose: string;
  granted: boolean;
  ip_address?: string | null;
}

// ─── Storage admin (→ foundation-lifecycle Phase 7) ─────────────────────────────

export type BucketPurpose = 'audit' | 'workspace' | 'kb_raw' | 'kb_derived';

export interface StorageQuotaSummaryItem {
  tenant_id: string;
  bucket_purpose: BucketPurpose;
  used_bytes: number;
  limit_bytes: number;
  used_pct: number;
  soft_warn_pct: number;
  updated_at: string;
}

export interface StorageQuotaSummaryResponse {
  items: StorageQuotaSummaryItem[];
  total: number;
}

export interface StorageQuotaPatch {
  limit_bytes?: number;
  soft_warn_pct?: number;
}

export interface OrphanReport {
  id: string;
  started_at: string;
  completed_at: string;
  duration_ms: number;
  dry_run: boolean;
  grace_period_days: number;
  direction_a_orphans: number;
  direction_a_deleted: number;
  direction_b_missing: number;
}

export interface OrphanReportListResponse {
  items: OrphanReport[];
  next_cursor: string | null;
}

// Prometheus range-query response (just the slice the admin UI needs).
export interface PromSeries {
  metric: Record<string, string>;
  // Pairs of [unix_seconds, "value-as-string"] as Prometheus returns them.
  values: [number, string][];
}

export interface PromQueryRangeResponse {
  status: 'success' | 'error';
  data?: {
    resultType: 'matrix';
    result: PromSeries[];
  };
  errorType?: string;
  error?: string;
}
