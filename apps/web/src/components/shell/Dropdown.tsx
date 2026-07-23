import { useEffect, useRef, useState } from 'react';
import type { Dispatch, RefObject, SetStateAction } from 'react';

export interface DropdownOption {
  id: string;
  label: string;
}

/**
 * Open/close state shared by every dropdown popover in the app: closes on an
 * outside mousedown. Escape handling is deliberately left to the caller —
 * a document-level Escape listener here would fight the modal Dialog's own
 * Escape-to-close listener.
 */
export function useDropdownState(
  ref: RefObject<HTMLElement | null>,
): readonly [boolean, Dispatch<SetStateAction<boolean>>] {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open, ref]);

  return [open, setOpen] as const;
}

/** Default sizing for the toolbar dropdown popover (fits its content). */
const DEFAULT_PANEL_CLASS = 'w-max min-w-[112px] max-w-[320px]';

interface DropdownMenuProps {
  value: string;
  options: DropdownOption[];
  /** Called with the picked option id; the caller closes the popover. */
  onSelect: (id: string) => void;
  align?: 'left' | 'right';
  dropDirection?: 'up' | 'down';
  /** Sizing/overflow classes for the popover panel (width, max-height). */
  panelClassName?: string;
}

/**
 * The popover panel itself — extracted so callers that need a different
 * trigger (e.g. the full-box `SelectField`) reuse the same markup, roles and
 * item styling instead of copying it. Must be rendered inside a
 * `position: relative` container.
 */
export function DropdownMenu({
  value,
  options,
  onSelect,
  align = 'right',
  dropDirection = 'up',
  panelClassName = DEFAULT_PANEL_CLASS,
}: DropdownMenuProps) {
  return (
    <div
      role="menu"
      className={`absolute ${dropDirection === 'down' ? 'top-full mt-1' : 'bottom-full mb-1'} ${align === 'right' ? 'right-0' : 'left-0'} ${panelClassName} z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 animate-pop-in ${dropDirection === 'down' ? 'origin-top' : 'origin-bottom'}`}
    >
      {options.map((o) => {
        const on = o.id === value;
        return (
          <button
            key={o.id || '__off'}
            type="button"
            role="menuitemradio"
            aria-checked={on}
            onClick={() => onSelect(o.id)}
            className={`w-full text-left px-2 py-1.5 rounded-md font-mono text-[11.5px] whitespace-nowrap overflow-hidden text-ellipsis hover:bg-paper ${
              on ? 'text-ink font-medium' : 'text-ink-2'
            }`}
          >
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

interface Props {
  value: string;
  options: DropdownOption[];
  onChange: (id: string) => void;
  disabled?: boolean;
  title?: string;
  ariaLabel?: string;
  /** Which edge the popover aligns to. Defaults to 'right'. */
  align?: 'left' | 'right';
  /** Render as a full-width bordered input-like trigger. */
  fullWidth?: boolean;
  /** Which direction the popover opens. Defaults to 'up' (bottom toolbar). */
  dropDirection?: 'up' | 'down';
  /** Optional visual override for the trigger while preserving dropdown behavior. */
  triggerClassName?: string;
}

/**
 * Small composer dropdown matching the design system's mono toolbar controls.
 * Opens upward by default (it lives in a bottom toolbar); pass
 * `dropDirection="down"` for mid-page or in-dialog placement. Closes on outside click and on
 * selection — same pattern as InputBar's "+" popover.
 */
export function Dropdown({
  value,
  options,
  onChange,
  disabled,
  title,
  ariaLabel,
  align = 'right',
  fullWidth,
  dropDirection = 'up',
  triggerClassName = '',
}: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdownState(ref);

  const current = options.find((o) => o.id === value)?.label ?? value;

  return (
    <div ref={ref} className={`relative shrink-0${fullWidth ? ' w-full' : ''}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        title={title}
        aria-label={ariaLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        className={`${
          fullWidth
            ? 'w-full border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper text-left flex items-center justify-between gap-1 hover:border-ink-2 focus:outline-none focus:border-accent'
            : 'font-mono text-[10.5px] text-dim hover:text-ink disabled:opacity-50 leading-none inline-flex items-center gap-0.5 whitespace-nowrap'
        } ${triggerClassName}`}
      >
        {current}
        <span aria-hidden className="text-[8px]">▾</span>
      </button>
      {open && (
        <DropdownMenu
          value={value}
          options={options}
          onSelect={(id) => {
            onChange(id);
            setOpen(false);
          }}
          align={align}
          dropDirection={dropDirection}
        />
      )}
    </div>
  );
}
