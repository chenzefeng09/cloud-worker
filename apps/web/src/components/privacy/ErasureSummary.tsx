import type { ErasureResult } from '../../api/privacy';
import { countLabel, humanizeKey } from './labels';

/**
 * Renders what an erasure removed (self-service and admin), and — for a 502
 * partial failure — which steps finished. The backend shapes are only loosely
 * specified, so everything here is defensive: numeric leaves become count
 * rows, unknown keys are humanised.
 */

interface CountRow {
  key: string;
  label: string;
  count: number;
}

interface CountGroup {
  title: string;
  rows: CountRow[];
}

function countRows(obj: unknown, path = '', depth = 0): CountRow[] {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj) || depth > 3) return [];
  const rows: CountRow[] = [];
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const key = path ? `${path}.${k}` : k;
    if (typeof v === 'number' && Number.isFinite(v)) rows.push({ key, label: countLabel(k), count: v });
    else rows.push(...countRows(v, key, depth + 1));
  }
  return rows;
}

/** `n` from a step's `{ deleted: n }`; null for a skipped or missing step. */
function deletedCount(outcome: unknown): number | null {
  if (!outcome || typeof outcome !== 'object') return null;
  const n = (outcome as Record<string, unknown>).deleted;
  return typeof n === 'number' && Number.isFinite(n) ? n : null;
}

const APP_COUNT_LABELS: Record<string, string> = {
  apps_deleted: 'Apps deleted',
  apps_deattributed: 'Shared apps kept, author removed',
  versions_deattributed: 'App versions no longer naming you',
};

function appRows(apps: unknown): CountRow[] {
  return countRows(apps, 'apps').map((row) => {
    const label = APP_COUNT_LABELS[row.key.slice('apps.'.length)];
    return label ? { ...row, label } : row;
  });
}

export function erasureGroups(result: Partial<ErasureResult> | null | undefined): CountGroup[] {
  if (!result || typeof result !== 'object') return [];
  const groups: CountGroup[] = [];
  const content = countRows(result.platform?.deleted, 'platform');
  if (typeof result.platform?.runs_cancelled === 'number') {
    content.push({ key: 'platform.runs_cancelled', label: 'Running tasks stopped', count: result.platform.runs_cancelled });
  }
  const triggers = deletedCount(result.schedules);
  if (triggers !== null) content.push({ key: 'schedules', label: 'Triggers', count: triggers });
  if (content.length) groups.push({ title: 'Tasks and content', rows: content });
  if (typeof result.memories_deleted === 'number') {
    groups.push({ title: 'Memory', rows: [{ key: 'memories_deleted', label: 'Memories', count: result.memories_deleted }] });
  }
  const apps = appRows(result.apps);
  if (apps.length) groups.push({ title: 'Apps', rows: apps });
  const account = countRows(result.bff, 'bff');
  const modelPreferences = deletedCount(result.llm_preferences);
  if (modelPreferences !== null) {
    account.push({ key: 'llm_preferences', label: 'Model preferences', count: modelPreferences });
  }
  if (account.length) groups.push({ title: 'Account settings', rows: account });
  return groups;
}

export interface SkippedStep {
  step: string;
  reason: string;
}

/**
 * Steps the backend skipped, from a `skipped: { step: reason }` map and from
 * `{ skipped: reason }` step results — either may be present, so both are read.
 */
export function skippedSteps(...sources: unknown[]): SkippedStep[] {
  const found = new Map<string, string>();
  const add = (step: string, reason: unknown) => {
    if (typeof reason === 'string' && reason && !found.has(step)) found.set(step, reason);
  };
  for (const source of sources) {
    if (!source || typeof source !== 'object' || Array.isArray(source)) continue;
    const o = source as Record<string, unknown>;
    if (o.skipped && typeof o.skipped === 'object' && !Array.isArray(o.skipped)) {
      for (const [step, reason] of Object.entries(o.skipped as Record<string, unknown>)) add(step, reason);
    }
    for (const [step, v] of Object.entries(o)) {
      if (v && typeof v === 'object' && !Array.isArray(v)) add(step, (v as Record<string, unknown>).skipped);
    }
  }
  return [...found].map(([step, reason]) => ({ step, reason }));
}

function CountGroups({ groups }: { groups: CountGroup[] }) {
  if (groups.length === 0) return null;
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {groups.map((g) => (
        <div key={g.title} className="flex flex-col gap-1">
          <div className="cw-label">{g.title}</div>
          <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-0.5 text-[12.5px]">
            {g.rows.map((r) => (
              <div key={r.key} className="contents">
                <dt className="text-ink-2">{r.label}</dt>
                <dd className="font-mono text-ink text-right">{r.count.toLocaleString()}</dd>
              </div>
            ))}
          </dl>
        </div>
      ))}
    </div>
  );
}

const SKIP_REASONS: Record<string, string> = {
  not_configured: 'not part of this deployment',
  not_supported: 'not supported for this request',
};

function skipReason({ reason }: SkippedStep): string {
  return SKIP_REASONS[reason] ?? humanizeKey(reason);
}

function SkippedStepList({ steps }: { steps: SkippedStep[] }) {
  if (steps.length === 0) return null;
  return (
    <div className="flex flex-col gap-0.5 text-[12.5px] text-ink-2">
      <div className="text-dim">Skipped:</div>
      <ul className="flex flex-col gap-0.5">
        {steps.map((s) => (
          <li key={s.step}>
            {stepLabel(s.step)}: {skipReason(s)}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Counts from a successful erasure, any skipped steps, and what happens to the sign-in account. */
export function ErasureSummary({ result }: { result: ErasureResult }) {
  const groups = erasureGroups(result);
  return (
    <div className="flex flex-col gap-3">
      {groups.length > 0 ? (
        <CountGroups groups={groups} />
      ) : (
        <p className="text-[12.5px] text-ink-2">There was nothing left to delete.</p>
      )}
      <SkippedStepList steps={skippedSteps(result)} />
      {result.account && (
        <p className="text-[12.5px] text-ink-2">
          <span className="text-dim">Sign-in account: </span>
          {humanizeKey(result.account)}
        </p>
      )}
    </div>
  );
}

const STEP_LABELS: Record<string, string> = {
  schedules: 'Triggers',
  platform: 'Tasks, files, canvases and workspaces',
  orchestrator: 'Tasks, files, canvases and workspaces',
  memories: 'Memories',
  memory: 'Memories',
  knowledge: 'Memories',
  llm_preferences: 'Model preferences',
  apps: 'Apps',
  bff: 'Preferences, API keys and sign-ins',
  account: 'Sign-in account',
};

function stepLabel(step: string): string {
  return STEP_LABELS[step.trim().toLowerCase()] ?? humanizeKey(step);
}

function stringList(v: unknown): string[] {
  if (typeof v === 'string' && v.trim()) return [v];
  if (Array.isArray(v)) return v.filter((x): x is string => typeof x === 'string' && !!x.trim());
  return [];
}

function stepStatus(v: unknown): string {
  if (typeof v === 'string') return humanizeKey(v);
  if (typeof v === 'boolean') return v ? 'Done' : 'Not finished';
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    if (typeof o.status === 'string') return humanizeKey(o.status);
    if (o.ok === false || o.error) return 'Not finished';
    return 'Done';
  }
  return '—';
}

/** Explains a 502 partial erasure from whatever step info the backend sent. */
export function ErasurePartialDetails({ details }: { details: unknown }) {
  const d = details && typeof details === 'object' ? (details as Record<string, unknown>) : {};
  const completed = stringList(d.completed ?? d.completed_steps ?? d.succeeded ?? d.done);
  const failed = stringList(d.failed ?? d.failed_steps ?? d.failed_step ?? d.pending ?? d.step);
  const notStarted = stringList(d.not_attempted);
  const steps =
    d.steps && typeof d.steps === 'object' && !Array.isArray(d.steps)
      ? Object.entries(d.steps as Record<string, unknown>)
      : [];
  // The BFF sends the finished steps' results under `results`.
  const partial = (d.partial ?? d.results ?? d.result ?? d) as Partial<ErasureResult>;
  const groups = erasureGroups(partial);
  const skipped = skippedSteps(d, partial);

  return (
    <div className="flex flex-col gap-2 text-[12.5px] text-ink-2">
      {completed.length > 0 && (
        <p>
          <span className="text-dim">Finished: </span>
          {completed.map(stepLabel).join(', ')}
        </p>
      )}
      {failed.length > 0 && (
        <p>
          <span className="text-dim">Not finished: </span>
          {failed.map(stepLabel).join(', ')}
        </p>
      )}
      {notStarted.length > 0 && (
        <p>
          <span className="text-dim">Not started: </span>
          {notStarted.map(stepLabel).join(', ')}
        </p>
      )}
      <SkippedStepList steps={skipped} />
      {steps.length > 0 && (
        <ul className="flex flex-col gap-0.5">
          {steps.map(([name, value]) => (
            <li key={name}>
              {stepLabel(name)}: <span className="font-mono">{stepStatus(value)}</span>
            </li>
          ))}
        </ul>
      )}
      {groups.length > 0 && (
        <>
          <div className="text-dim">Already deleted:</div>
          <CountGroups groups={groups} />
        </>
      )}
    </div>
  );
}
