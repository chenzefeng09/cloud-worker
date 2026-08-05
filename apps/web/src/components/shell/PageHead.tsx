import { Fragment, type ReactNode } from 'react';
import { useNavigate } from 'react-router-dom';

export interface Crumb {
  label: string;
  /** Optional path for clickable crumbs; the last crumb is rendered bold. */
  to?: string;
}

interface Props {
  crumbs: Crumb[];
  /** Optional status indicator rendered between crumbs and action slot. */
  status?: ReactNode;
  /** Right-aligned slot for page-level actions (buttons, chips, etc). */
  actions?: ReactNode;
}

/**
 * Page header with breadcrumbs + optional right-side action slot. Mirrors
 * cloud-worker's `PageHead` but uses react-router for navigation and is
 * pure presentation so pages can drop it in without owning state.
 */
export function PageHead({ crumbs, status, actions }: Props) {
  const navigate = useNavigate();
  return (
    <div className="flex items-center gap-3.5 px-6 py-3.5 min-h-[56px] border-b border-line bg-paper shrink-0">
      <div className="flex items-center gap-1.5 font-mono text-[11px] text-dim">
        {crumbs.map((c, i) => (
          <Fragment key={i}>
            {i > 0 && <span className="opacity-50">/</span>}
            {c.to ? (
              <a
                onClick={() => navigate(c.to!)}
                className="cursor-pointer text-dim hover:text-ink"
              >
                {c.label}
              </a>
            ) : (
              <b className="text-ink font-semibold">{c.label}</b>
            )}
          </Fragment>
        ))}
      </div>
      {status}
      <div className="flex-1" />
      <div className="flex items-center gap-1.5">{actions}</div>
    </div>
  );
}
