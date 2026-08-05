/**
 * SelectField — stateless controlled select primitive.
 *
 * The whole bordered box is the trigger (a real `<button>`, so it is keyboard
 * focusable and opens with Enter/Space), not just the little ▾ glyph. The
 * popover itself is the shared `DropdownMenu` from the shell Dropdown, so the
 * open/close semantics and item styling stay in one place.
 */
import { useId, useRef } from 'react';

import { DropdownMenu, useDropdownState } from '../shell/Dropdown';

interface SelectOption {
  value: string;
  label: string;
}

interface SelectFieldProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  label?: string;
  help?: string;
  required?: boolean;
  error?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Display title for the trigger (accessibility / tooltip). */
  title?: string;
  size?: 'sm' | 'md';
}

export function SelectField({
  value,
  onChange,
  options,
  label,
  help,
  required,
  error,
  placeholder,
  disabled,
  title,
  size = 'md',
}: SelectFieldProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdownState(ref);
  const labelId = useId();

  const dropdownOptions = options.map((o) => ({ id: o.value, label: o.label }));
  const selected = options.find((o) => o.value === value);

  // Each visual concern picks exactly one class, so nothing depends on the
  // order classes happen to be written in (Tailwind has no such precedence).
  const borderCls = error ? 'border-accent' : 'border-line';
  const stateCls = disabled
    ? 'opacity-60 cursor-not-allowed'
    : error
      ? 'cursor-pointer'
      : 'cursor-pointer hover:border-ink-2';
  const textCls = size === 'sm' ? 'text-[12px]' : 'text-[13px]';

  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <label
          id={labelId}
          className={size === 'sm' ? 'cw-label' : 'text-[13px] font-medium text-ink'}
        >
          {label}
          {required && <span className="text-accent ml-1">*</span>}
        </label>
      )}
      {help && (
        <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{help}</div>
      )}
      <div ref={ref} className="relative">
        <button
          type="button"
          disabled={disabled}
          onClick={() => setOpen((o) => !o)}
          onKeyDown={(e) => {
            // Escape closes just the popover. `stopPropagation` keeps it from
            // reaching a surrounding modal Dialog's Escape-to-close handler.
            if (e.key === 'Escape' && open) {
              e.stopPropagation();
              setOpen(false);
            }
          }}
          title={title}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-labelledby={label ? labelId : undefined}
          aria-label={label ? undefined : (title ?? placeholder)}
          className={`w-full flex items-center gap-2 bg-surface border rounded-md px-3 py-2 text-left transition-colors focus:outline-none focus-visible:border-accent ${borderCls} ${stateCls}`}
        >
          <span className={`${textCls} flex-1 truncate ${selected ? 'text-ink' : 'text-dim'}`}>
            {selected?.label ?? placeholder ?? '请选择…'}
          </span>
          <span aria-hidden className="text-[9px] text-dim shrink-0 leading-none">
            ▾
          </span>
        </button>
        {open && (
          <DropdownMenu
            value={value}
            options={dropdownOptions}
            onSelect={(id) => {
              onChange(id);
              setOpen(false);
            }}
            align="left"
            dropDirection="down"
            panelClassName="min-w-full w-max max-w-[min(28rem,90vw)] max-h-[260px] overflow-auto"
          />
        )}
      </div>
      {error && <div className="text-[11.5px] text-accent">{error}</div>}
    </div>
  );
}
