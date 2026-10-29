/**
 * Display helpers for privacy data: turn backend enum-ish strings (legal bases,
 * rights, transfer mechanisms, count keys) into plain English. Unknown values
 * pass through — humanised when they look like identifiers, verbatim otherwise.
 */

const IDENTIFIER = /^[A-Za-z][A-Za-z0-9]*(?:[_\-.][A-Za-z0-9]+)*$/;

function norm(value: string): string {
  return value.trim().toLowerCase().replace(/[\s\-./]+/g, '_');
}

/** `tool_calls` → "Tool calls", `ACCESS` → "Access"; free text and short acronyms are kept. */
export function humanizeKey(value: string): string {
  const v = value.trim();
  if (!IDENTIFIER.test(v) || /^[A-Z0-9]{1,4}$/.test(v)) return v;
  const words = v
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-.]+/g, ' ')
    .toLowerCase();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const LEGAL_BASIS: Record<string, string> = {
  consent: 'Consent',
  contract: 'Performance of a contract',
  legal_obligation: 'Legal obligation',
  vital_interest: 'Vital interests',
  vital_interests: 'Vital interests',
  public_task: 'Public task',
  legitimate_interest: 'Legitimate interests',
  legitimate_interests: 'Legitimate interests',
};

export function legalBasisLabel(value: string): string {
  return LEGAL_BASIS[norm(value)] ?? humanizeKey(value);
}

const RIGHTS: Record<string, string> = {
  access: 'See the data we hold about you',
  rectification: 'Correct inaccurate data',
  erasure: 'Have your data deleted',
  restriction: 'Restrict how we use your data',
  portability: 'Get your data in a portable format',
  objection: 'Object to processing',
  object: 'Object to processing',
  automated_decision: 'Not be subject to decisions made only by automated means',
  automated_decisions: 'Not be subject to decisions made only by automated means',
  automated_decision_making: 'Not be subject to decisions made only by automated means',
  withdraw_consent: 'Withdraw consent at any time',
  complaint: 'Complain to a data protection supervisory authority',
  lodge_complaint: 'Complain to a data protection supervisory authority',
};

export function rightLabel(value: string): string {
  return RIGHTS[norm(value)] ?? humanizeKey(value);
}

/** True when the rights list already mentions complaining to a regulator. */
export function mentionsComplaint(rights: string[]): boolean {
  return rights.some((r) => /complain|supervisory/i.test(r));
}

const TRANSFER: Record<string, string> = {
  scc: 'Standard Contractual Clauses',
  sccs: 'Standard Contractual Clauses',
  standard_contractual_clauses: 'Standard Contractual Clauses',
  bcr: 'Binding Corporate Rules',
  bcrs: 'Binding Corporate Rules',
  binding_corporate_rules: 'Binding Corporate Rules',
  adequacy: 'EU adequacy decision',
  adequacy_decision: 'EU adequacy decision',
  dpf: 'EU-U.S. Data Privacy Framework',
  eu_us_dpf: 'EU-U.S. Data Privacy Framework',
  not_applicable: 'Not applicable',
  n_a: 'Not applicable',
};

/** Empty when the backend gave no mechanism — we never infer one. */
export function transferMechanismLabel(value: string | null | undefined): string {
  if (!value || !value.trim()) return '';
  return TRANSFER[norm(value)] ?? humanizeKey(value);
}

/** ISO 3166 alpha-2 code → country name ("US" → "United States"). */
export function countryLabel(value: string | null | undefined): string {
  const v = (value ?? '').trim();
  if (!/^[A-Za-z]{2}$/.test(v)) return v;
  try {
    return new Intl.DisplayNames(['en'], { type: 'region' }).of(v.toUpperCase()) ?? v;
  } catch {
    return v.toUpperCase();
  }
}

const COUNT_LABELS: Record<string, string> = {
  sessions: 'Tasks and conversations',
  conversations: 'Conversations',
  tasks: 'Tasks',
  messages: 'Messages',
  runs: 'Task runs',
  run_steps: 'Run steps',
  attachments: 'Files',
  files: 'Files',
  workspace_files: 'Workspace files',
  file_versions: 'File versions',
  canvases: 'Canvases',
  canvas_versions: 'Canvas versions',
  workspaces: 'Workspaces',
  memories: 'Memories',
  preferences: 'Preferences',
  model_preferences: 'Model preferences',
  notification_preferences: 'Notification preferences',
  flag_overrides: 'Feature settings',
  api_keys: 'API keys',
  schedules: 'Triggers',
  cron_jobs: 'Triggers',
  acknowledgements: 'Notice acknowledgements',
  privacy_settings: 'Privacy settings',
};

export function countLabel(key: string): string {
  return COUNT_LABELS[norm(key)] ?? humanizeKey(key);
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function pluralize(n: number, one: string, many = `${one}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? one : many}`;
}
