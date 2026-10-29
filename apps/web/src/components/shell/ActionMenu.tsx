import { useEffect, useRef, useState } from 'react';
import { useDropdownState } from './Dropdown';

// ─── Overflow action menu ─────────────────────────────────────────────────────
//
// A "⋯" trigger that opens a list of one-shot commands. Deliberately separate
// from `DropdownMenu`: that one is a value picker (menuitemradio + a selected
// `value`), which reads wrong to assistive tech for actions like 删除. Panel
// styling is copied from DropdownMenu so the two popovers stay identical.

/** Inline 16×16 gear, matching the repo's currentColor SVG convention. */
function GearIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3.1" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export interface ActionMenuItem {
  id: string;
  label: string;
  /** Renders in the accent (destructive) colour. */
  danger?: boolean;
  disabled?: boolean;
  /** Hover hint — mainly to explain why a disabled item is disabled. */
  title?: string;
  onSelect: () => void;
}

interface Props {
  items: ActionMenuItem[];
  /** Which edge the popover aligns to. Defaults to 'right' (page-head usage). */
  align?: 'left' | 'right';
  ariaLabel?: string;
  disabled?: boolean;
  /**
   * Trigger glyph. 'gear' renders a square icon button with a real hit target —
   * the bare '⋯' is easy to miss in a page head.
   */
  icon?: 'ellipsis' | 'gear';
  /**
   * When the trigger lives inside a scroll/overflow container (e.g. a table
   * with `overflow-x-auto`, whose `overflow-y` computes to `auto` too), an
   * absolutely-positioned popover gets clipped. `floating` positions the
   * popover `fixed` to the viewport instead — measured from the trigger's rect
   * on open — so it escapes the clip. Closes on scroll/resize to avoid drift.
   */
  floating?: boolean;
}

export function ActionMenu({
  items,
  align = 'right',
  ariaLabel = 'More actions',
  disabled,
  icon = 'ellipsis',
  floating = false,
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useDropdownState(ref);
  // Fixed-position coords for `floating` mode, measured from the trigger.
  const [pos, setPos] = useState<{ top: number; right: number }>({ top: 0, right: 0 });

  // In floating mode the popover is detached from the scroll flow, so a scroll
  // or resize would leave it stranded — close it rather than track it.
  useEffect(() => {
    if (!floating || !open) return;
    const close = () => setOpen(false);
    window.addEventListener('scroll', close, true);
    window.addEventListener('resize', close);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('resize', close);
    };
  }, [floating, open, setOpen]);

  if (items.length === 0) return null;

  const toggle = () => {
    if (!open && floating && btnRef.current) {
      const r = btnRef.current.getBoundingClientRect();
      setPos({ top: r.bottom + 4, right: Math.max(8, window.innerWidth - r.right) });
    }
    setOpen((o) => !o);
  };

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        ref={btnRef}
        type="button"
        disabled={disabled}
        onClick={toggle}
        aria-label={ariaLabel}
        title={ariaLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        className={
          icon === 'gear'
            ? 'cw-btn cw-btn-ghost w-[30px] h-[30px] p-0 justify-center text-ink-2 hover:text-ink'
            : 'cw-btn cw-btn-ghost cw-btn-sm'
        }
      >
        {icon === 'gear' ? <GearIcon /> : <span aria-hidden>⋯</span>}
      </button>
      {open && (
        <div
          role="menu"
          aria-label={ariaLabel}
          style={floating ? { position: 'fixed', top: pos.top, right: pos.right } : undefined}
          className={
            floating
              ? 'w-max min-w-[112px] max-w-[320px] z-50 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 animate-pop-in origin-top'
              : `absolute top-full mt-1 ${align === 'right' ? 'right-0' : 'left-0'} w-max min-w-[112px] max-w-[320px] z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 animate-pop-in origin-top`
          }
        >
          {items.map((item) => (
            <button
              key={item.id}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              title={item.title}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
              className={`w-full text-left px-2 py-1.5 rounded-md font-mono text-[11.5px] whitespace-nowrap overflow-hidden text-ellipsis hover:bg-paper disabled:opacity-40 disabled:cursor-not-allowed ${
                item.danger ? 'text-accent' : 'text-ink-2'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
