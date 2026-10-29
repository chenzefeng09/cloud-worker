import { useId } from 'react';
import { Link, useInRouterContext } from 'react-router-dom';

/**
 * Small building blocks for the privacy screens. `PrivacySection` mirrors the
 * Profile page's card (PfSection) so the "Privacy & data" tab blends in with
 * the other tabs; `PrivacySwitch` is an accessible version of its toggle.
 */

export const linkCls = 'text-accent underline underline-offset-2 hover:opacity-80';

/** In-app link that degrades to a plain anchor outside a <Router>. */
export function AppLink({ to, children, className = linkCls }: { to: string; children: React.ReactNode; className?: string }) {
  const inRouter = useInRouterContext();
  return inRouter ? (
    <Link to={to} className={className}>{children}</Link>
  ) : (
    <a href={to} className={className}>{children}</a>
  );
}

/** HTTP status of a failed request (ApiError and look-alikes), if any. */
export function httpStatus(err: unknown): number | undefined {
  const s = err && typeof err === 'object' ? (err as { status?: unknown }).status : undefined;
  return typeof s === 'number' ? s : undefined;
}

/** A readable message for a failed request: the server's text when it has one. */
export function describeError(err: unknown, fallback: string): string {
  const msg = err instanceof Error ? err.message.trim() : '';
  return msg && msg !== 'Request failed' && !/^failed to fetch$/i.test(msg) ? `${fallback} (${msg})` : fallback;
}

export function PrivacySection({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className="border border-line rounded-md bg-surface overflow-hidden">
      <div className="flex items-center gap-2.5 px-4 py-[9px] border-b border-line-2 bg-paper">
        <h2 id={id} className="cw-label font-normal">{title}</h2>
        <div className="flex-1" />
        {action}
      </div>
      <div className="px-4 py-3.5 flex flex-col gap-3">{children}</div>
    </section>
  );
}

export function PrivacySwitch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-[17px] w-[30px] shrink-0 rounded-full transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-line disabled:opacity-50 disabled:cursor-not-allowed ${
        checked ? 'bg-accent' : 'bg-line'
      }`}
    >
      <span
        aria-hidden
        className={`absolute top-[2px] h-[13px] w-[13px] rounded-full bg-white shadow transition-[left] ${
          checked ? 'left-[15px]' : 'left-[2px]'
        }`}
      />
    </button>
  );
}

const ALERT_TONES = {
  error: 'border-accent-line bg-accent-soft text-accent',
  warn: 'border-warn/40 bg-warn/10 text-ink-2',
  success: 'border-ok/40 bg-ok/10 text-ink',
  info: 'border-line bg-paper text-ink-2',
} as const;

export function InlineAlert({
  tone = 'info',
  children,
}: {
  tone?: keyof typeof ALERT_TONES;
  children: React.ReactNode;
}) {
  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={`border rounded-sm px-3 py-2 text-[12.5px] leading-[1.55] ${ALERT_TONES[tone]}`}
    >
      {children}
    </div>
  );
}

/** Body copy used across the privacy sections. */
export function Prose({ children }: { children: React.ReactNode }) {
  return <p className="text-[13px] leading-[1.6] text-ink-2">{children}</p>;
}

/**
 * Button classes: the shared cw-btn atoms plus a visible disabled state.
 * `.cw-btn-accent` has no hover rule of its own, so `.cw-btn:hover` would turn
 * it beige under white text — the hover utilities keep it red.
 */
export const btn = {
  base: 'cw-btn cw-btn-sm disabled:opacity-50 disabled:cursor-not-allowed',
  primary: 'cw-btn cw-btn-sm cw-btn-primary disabled:opacity-50 disabled:cursor-not-allowed',
  danger:
    'cw-btn cw-btn-sm cw-btn-accent hover:bg-accent hover:border-accent hover:opacity-90 disabled:opacity-50 disabled:cursor-not-allowed',
  ghost: 'cw-btn cw-btn-sm cw-btn-ghost disabled:opacity-50 disabled:cursor-not-allowed',
};
