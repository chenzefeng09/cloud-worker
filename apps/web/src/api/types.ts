export interface Session {
  id: string;
  tenant_id: string;
  user_id: string;
  title: string | null;
  status: 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ERROR' | 'ARCHIVED';
  current_run_id: string | null;
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

export interface AuditEvent {
  id: string;
  tenant_id: string;
  actor_id?: string;
  action: string;
  resource_type?: string;
  resource_id?: string;
  meta?: Record<string, unknown>;
  created_at: string;
}

export interface AuditQueryRequest {
  tenant_id?: string;
  actor_id?: string;
  action?: string;
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

// ─── LLM Endpoint Diagnostics ────────────────────────────────────────────────

export type ProviderType = 'openai_compatible' | 'anthropic' | 'vllm' | 'ollama' | 'bedrock' | 'azure';

export interface ProbeRequest {
  url: string;
  api_key: string;
  provider_type?: ProviderType;
}

/** Each capability result is boolean (true/false) or "unknown" or an error string. */
export type ProbeCapabilityResult = boolean | 'unknown' | string;

export interface ProbeResults {
  list_models: ProbeCapabilityResult;
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
