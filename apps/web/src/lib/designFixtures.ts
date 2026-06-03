// DESIGN FIXTURE — remove when backend provides all values below.
// Pure static data mirroring the cloud-worker design prototype.
// No React, no app imports, no side effects.

// ── Toggle ────────────────────────────────────────────────────────
/** Set to false to stop rendering fixture data in the UI. */
export const SHOW_DESIGN_FIXTURES = true; // DESIGN FIXTURE — remove when backend provides feature flags

// ── Shared primitive interfaces ───────────────────────────────────

/** A single tool call inside a tools-kind transcript message. */
export interface ToolCallItem {
  name: string;
  target: string;
  lines: string;
}

// ── AgentPage rail fixtures ───────────────────────────────────────

export interface PlanStep {
  n: number;
  t: string;
  d: boolean;
  live?: boolean;
}

// DESIGN FIXTURE — remove when backend provides task plan steps
export const PLAN_STEPS: PlanStep[] = [
  { n: 1, t: 'Map auth surface',    d: true },
  { n: 2, t: 'Replace hash branch', d: true },
  { n: 3, t: 'Add credential store', d: false, live: true },
  { n: 4, t: 'Wire LoginForm',      d: false },
];

export interface AgentSkill {
  label: string;
  dashed?: boolean;
}

// DESIGN FIXTURE — remove when backend provides task skill attachments
export const AGENT_SKILLS: AgentSkill[] = [
  { label: '✦ typescript-expert' },
  { label: '✦ security-review' },
  { label: '▤ auth-spec.pdf', dashed: true },
];

export interface AgentWorker {
  id: string;
  glyph: string;
  ago: string;
  tokens: string;
}

// DESIGN FIXTURE — remove when backend provides worker-on-task card data
export const AGENT_WORKER: AgentWorker = {
  id: 'aria',
  glyph: 'A',
  ago: '34s',
  tokens: '4.2k',
};

export interface AgentLiveStats {
  elapsed: string;
  tokens: string;
  cost: string;
}

// DESIGN FIXTURE — remove when backend provides live task statistics
export const AGENT_LIVE_STATS: AgentLiveStats = {
  elapsed: '4m 18s',
  tokens: '4.2k',
  cost: '$0.34',
};

// ── Agent transcript ──────────────────────────────────────────────

interface TranscriptUserMessage {
  id: string;
  from: 'user';
  text: string;
}

interface TranscriptThought {
  id: string;
  from: 'agent';
  kind: 'thought';
  title: string;
  body: string[];
}

interface TranscriptTools {
  id: string;
  from: 'agent';
  kind: 'tools';
  items: ToolCallItem[];
}

interface TranscriptText {
  id: string;
  from: 'agent';
  kind: 'text';
  text: string;
}

interface TranscriptLive {
  id: string;
  from: 'agent';
  kind: 'live';
  label: string;
  body: string;
}

export type TranscriptMessage =
  | TranscriptUserMessage
  | TranscriptThought
  | TranscriptTools
  | TranscriptText
  | TranscriptLive;

// DESIGN FIXTURE — remove when backend provides agent transcript for passkey-migration task
export const AGENT_TRANSCRIPT_FIXTURE: TranscriptMessage[] = [
  {
    id: 'u1',
    from: 'user',
    text: "Migrate the auth flow to passkeys. Keep email as a fallback. The repo is github.com/acme/web — work on the `main` branch.",
  },
  {
    id: 'a1',
    from: 'agent',
    kind: 'thought',
    title: 'Plan the change in 4 steps',
    body: [
      "Map the current auth surface (session, login, middleware, schema).",
      "Add a WebAuthn registration + verification flow alongside the password flow, gated by a feature flag.",
      "Wire the new flow into LoginForm and add a fallback path.",
      "Update the user schema to store credentialId, publicKey, counter.",
    ],
  },
  {
    id: 'a2',
    from: 'agent',
    kind: 'tools',
    items: [
      { name: 'read',   target: 'src/auth/session.ts',    lines: '142 ln' },
      { name: 'read',   target: 'src/auth/login.tsx',     lines: '83 ln'  },
      { name: 'read',   target: 'src/auth/middleware.ts', lines: '47 ln'  },
      { name: 'search', target: 'regex "passwordHash"',   lines: '9 hits' },
    ],
  },
  {
    id: 'a3',
    from: 'agent',
    kind: 'text',
    text: "Reviewed `session.ts` — the bcrypt branch lives at lines 34-58 and is the only blocker. I'll keep `validateSession` as-is and add a parallel `validatePasskey` that the new `LoginForm` calls first.",
  },
  {
    id: 'a4',
    from: 'agent',
    kind: 'tools',
    items: [
      { name: 'edit',  target: 'src/auth/session.ts', lines: '+18 −12' },
      { name: 'write', target: 'src/auth/passkey.ts', lines: '+48'     },
    ],
  },
  {
    id: 'u2',
    from: 'user',
    text: "Looks good — also update the prisma schema.",
  },
  {
    id: 'a5',
    from: 'agent',
    kind: 'tools',
    items: [
      { name: 'edit', target: 'prisma/schema.prisma',  lines: '+28 −5' },
      { name: 'run',  target: 'pnpm prisma generate',  lines: '✓ done' },
    ],
  },
  {
    id: 'a6',
    from: 'agent',
    kind: 'live',
    label: 'Writing src/auth/passkey.ts · line 31 of 48',
    body: 'await db.user.update({ where: { id: userId }, data: { currentChallenge: opts…',
  },
];

// ── Fleet topology ────────────────────────────────────────────────

export interface TopologyNode {
  id: string;
  x: number;
  y: number;
  label: string;
}

export interface TopologyWorkerNode extends TopologyNode {
  glyph: string;
  status: 'thinking' | 'awaiting' | 'paused' | 'done' | 'idle';
}

export interface TopologyTaskNode extends TopologyNode {
  slug: string;
}

export interface TopologyMcpNode extends TopologyNode {
  mcpStatus: 'connected' | 'auth needed';
}

export interface TopologyLink {
  fromWorker: string;
  toTask?: string;
  toMcp?: string;
  live?: boolean;
}

export interface FleetTopology {
  workers: TopologyWorkerNode[];
  tasks: TopologyTaskNode[];
  mcps: TopologyMcpNode[];
  links: TopologyLink[];
}

// DESIGN FIXTURE — remove when backend provides fleet topology geometry
export const FLEET_TOPOLOGY: FleetTopology = {
  workers: [
    { id: 'aria', x: 290, y: 200, label: 'aria', glyph: 'A', status: 'thinking' },
    { id: 'noé',  x: 470, y: 130, label: 'noé',  glyph: 'N', status: 'awaiting' },
    { id: 'kai',  x: 410, y: 320, label: 'kai',  glyph: 'K', status: 'paused'   },
    { id: 'mei',  x: 560, y: 280, label: 'mei',  glyph: 'M', status: 'done'     },
    { id: 'rin',  x: 200, y: 320, label: 'rin',  glyph: 'R', status: 'idle'     },
    { id: 'jun',  x: 620, y: 150, label: 'jun',  glyph: 'J', status: 'idle'     },
  ],
  tasks: [
    { id: 'passkey-migration', x: 110, y: 200, label: 'passkey-migration', slug: 'passkey-migration' },
    { id: 'q3-launch-deck',    x: 540, y: 60,  label: 'q3-launch-deck',    slug: 'q3-launch-deck'    },
    { id: 'inbox-triage',      x: 360, y: 410, label: 'inbox-triage',      slug: 'inbox-triage'      },
    { id: 'design-audit',      x: 690, y: 320, label: 'design-audit',      slug: 'design-audit'      },
  ],
  mcps: [
    { id: 'github',   x: 130, y: 70,  label: 'github',   mcpStatus: 'connected'   },
    { id: 'figma',    x: 360, y: 50,  label: 'figma',    mcpStatus: 'connected'   },
    { id: 'linear',   x: 720, y: 60,  label: 'linear',   mcpStatus: 'connected'   },
    { id: 'postgres', x: 720, y: 410, label: 'postgres', mcpStatus: 'auth needed' },
  ],
  links: [
    { fromWorker: 'aria', toTask: 'passkey-migration', live: true },
    { fromWorker: 'aria', toMcp:  'github',            live: true },
    { fromWorker: 'noé',  toTask: 'q3-launch-deck' },
    { fromWorker: 'noé',  toMcp:  'figma' },
    { fromWorker: 'kai',  toTask: 'inbox-triage' },
    { fromWorker: 'mei',  toTask: 'design-audit' },
    { fromWorker: 'mei',  toMcp:  'figma' },
    { fromWorker: 'aria', toMcp:  'linear' },
  ],
};

// ── Profile fixtures ──────────────────────────────────────────────

export interface UsageDay {
  day: string;
  amount: number;
}

// DESIGN FIXTURE — remove when backend provides weekly usage data
export const PF_USAGE_WEEK: UsageDay[] = [
  { day: 'Mon', amount: 2.8  },
  { day: 'Tue', amount: 5.1  },
  { day: 'Wed', amount: 3.4  },
  { day: 'Thu', amount: 7.2  },
  { day: 'Fri', amount: 4.8  },
  { day: 'Sat', amount: 1.2  },
  { day: 'Sun', amount: 4.21 },
];

export interface ApiKey {
  name: string;
  prefix: string;
  created: string;
  lastUsed: string;
  scopes: string;
}

// DESIGN FIXTURE — remove when backend provides API key list
export const PF_API_KEYS: ApiKey[] = [
  { name: 'Production',          prefix: 'cw_live_4xKm…', created: 'Feb 14', lastUsed: '34s ago',  scopes: 'tasks · files · fleet' },
  { name: 'CI / GitHub Actions', prefix: 'cw_live_9rPq…', created: 'Mar 01', lastUsed: '11m ago',  scopes: 'tasks' },
  { name: 'Local dev',           prefix: 'cw_test_2vBn…', created: 'Apr 10', lastUsed: '2d ago',   scopes: 'tasks · files · fleet' },
];

export interface Session {
  device: string;
  location: string;
  time: string;
  current?: boolean;
}

// DESIGN FIXTURE — remove when backend provides active session list
export const PF_SESSIONS: Session[] = [
  { device: 'Chrome · macOS 15', location: 'San Francisco, CA', time: 'now',    current: true  },
  { device: 'Safari · iOS 18',   location: 'San Francisco, CA', time: '2h ago'                 },
  { device: 'Firefox · Win 11',  location: 'New York, NY',      time: '3d ago'                 },
];

export interface NotifSetting {
  event: string;
  sub: string;
  email: boolean;
  push: boolean;
}

// DESIGN FIXTURE — remove when backend provides notification preference list
export const PF_NOTIFS: NotifSetting[] = [
  { event: 'Task completed', sub: 'Worker finished a task',          email: true,  push: true  },
  { event: 'Needs review',   sub: 'Worker paused for your input',    email: true,  push: true  },
  { event: 'Budget at 80%',  sub: 'Daily spend nearing limit',       email: true,  push: false },
  { event: 'Budget hit',     sub: 'Worker stopped — limit reached',  email: true,  push: true  },
  { event: 'Worker error',   sub: 'Unrecoverable failure',           email: true,  push: true  },
  { event: 'Weekly digest',  sub: 'Summary of activity & spend',     email: true,  push: false },
];

export interface PlanInfo {
  name: string;
  pricePerMonth: number;
  renewsLabel: string;
  features: string[];
}

// DESIGN FIXTURE — remove when backend provides plan details
export const PF_PLAN: PlanInfo = {
  name: 'Pro',
  pricePerMonth: 49,
  renewsLabel: 'renews Jul 1',
  features: ['6 workers', '$200 compute / mo', 'all models', 'priority queue', 'API access'],
};

export interface ProfileStats {
  tasksRun: number;
  tokensThisMonth: string;
  workers: number;
  memberSince: string;
  todaySpend: number;
  dailyCap: number;
  monthSpend: number;
  monthlyCap: number;
}

// DESIGN FIXTURE — remove when backend provides profile usage statistics
export const PF_STATS: ProfileStats = {
  tasksRun: 247,
  tokensThisMonth: '2.1M',
  workers: 6,
  memberSince: 'Jan 2026',
  todaySpend: 4.21,
  dailyCap: 50,
  monthSpend: 88.4,
  monthlyCap: 200,
};
