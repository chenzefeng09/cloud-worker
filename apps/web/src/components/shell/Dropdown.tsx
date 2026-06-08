import { useEffect, useRef, useState } from 'react';

export interface DropdownOption {
  id: string;
  label: string;
}

interface Props {
  value: string;
  options: DropdownOption[];
  onChange: (id: string) => void;
  disabled?: boolean;
  title?: string;
  /** Which edge the popover aligns to. Defaults to 'right'. */
  align?: 'left' | 'right';
}

/**
 * Small composer dropdown matching the design system's mono toolbar controls.
 * Opens UPWARD (it lives in a bottom toolbar). Closes on outside click and on
 * selection — same pattern as InputBar's "+" popover.
 */
export function Dropdown({ value, options, onChange, disabled, title, align = 'right' }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [open]);

  const current = options.find((o) => o.id === value)?.label ?? value;

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        title={title}
        aria-haspopup="menu"
        aria-expanded={open}
        className="font-mono text-[10.5px] text-dim hover:text-ink disabled:opacity-50 leading-none inline-flex items-center gap-0.5"
      >
        {current}
        <span aria-hidden className="text-[8px]">▾</span>
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute bottom-full mb-1 ${align === 'right' ? 'right-0' : 'left-0'} min-w-[112px] z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1`}
        >
          {options.map((o) => {
            const on = o.id === value;
            return (
              <button
                key={o.id || '__off'}
                type="button"
                role="menuitemradio"
                aria-checked={on}
                onClick={() => {
                  onChange(o.id);
                  setOpen(false);
                }}
                className={`w-full text-left px-2 py-1.5 rounded-md font-mono text-[11.5px] hover:bg-paper ${
                  on ? 'text-ink font-medium' : 'text-ink-2'
                }`}
              >
                {o.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
