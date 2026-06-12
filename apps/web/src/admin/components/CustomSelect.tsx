import { useEffect, useRef, useState } from 'react';

export interface SelectOption {
  value: string;
  label: string;
}

export interface SelectOptionGroup {
  label: string;
  options: SelectOption[];
}

interface CustomSelectProps {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

interface GroupedSelectProps {
  groups: SelectOptionGroup[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
}

function useDropdown(ref: React.RefObject<HTMLDivElement | null>) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onClickOutside);
    document.addEventListener('keydown', onEsc);
    return () => {
      document.removeEventListener('mousedown', onClickOutside);
      document.removeEventListener('keydown', onEsc);
    };
  }, [open]);

  return [open, setOpen] as const;
}

const triggerCls = (disabled: boolean) =>
  `w-full border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper text-left flex items-center justify-between gap-2 transition-colors ${
    disabled
      ? 'opacity-50 cursor-not-allowed'
      : 'hover:border-ink-2 focus:outline-none focus:border-accent'
  }`;

const dropdownCls =
  'absolute z-50 mt-1 min-w-full w-max border border-line rounded-md bg-surface shadow-lg max-h-[240px] overflow-auto';

const itemCls = (active: boolean) =>
  `w-full text-left px-3 py-1.5 text-[12px] transition-colors ${
    active ? 'bg-accent-soft text-ink font-medium' : 'text-ink hover:bg-paper-2'
  }`;

export function CustomSelect({
  options,
  value,
  onChange,
  placeholder = '请选择…',
  disabled = false,
  className = '',
}: CustomSelectProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdown(ref);
  const selected = options.find((o) => o.value === value);

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={triggerCls(disabled)}
      >
        <span className={selected ? 'text-ink truncate' : 'text-dim truncate'}>
          {selected?.label ?? placeholder}
        </span>
        <span className="text-[10px] text-dim shrink-0">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className={dropdownCls}>
          {options.map((o) => (
            <button
              key={o.value}
              type="button"
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
              className={itemCls(o.value === value)}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

interface SearchableSelectProps {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * Single-select dropdown with a type-to-filter search box. Matches the values
 * by substring against each option's `value` and `label` (case-insensitive).
 */
export function SearchableSelect({
  options,
  value,
  onChange,
  placeholder = '请选择…',
  searchPlaceholder = '搜索…',
  emptyText = '无匹配项',
  disabled = false,
  className = '',
}: SearchableSelectProps) {
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useDropdown(ref);
  const [query, setQuery] = useState('');
  const selected = options.find((o) => o.value === value);

  useEffect(() => {
    if (open) {
      setQuery('');
      // Defer focus until the input is mounted.
      const id = requestAnimationFrame(() => inputRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = q
    ? options.filter(
        (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
      )
    : options;

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={triggerCls(disabled)}
      >
        <span className={selected ? 'text-ink truncate' : 'text-dim truncate'}>
          {selected?.label ?? placeholder}
        </span>
        <span className="flex items-center gap-1 shrink-0">
          {value.trim().length > 0 && !disabled && (
            <span
              role="button"
              tabIndex={-1}
              onClick={(e) => { e.stopPropagation(); onChange(''); }}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onChange(''); } }}
              className="w-3.5 h-3.5 rounded-full border border-line flex items-center justify-center text-[10px] leading-none text-dim hover:text-ink cursor-pointer"
              aria-label="清空"
            >
              ×
            </span>
          )}
          <span className="text-[10px] text-dim">{open ? '▲' : '▼'}</span>
        </span>
      </button>

      {open && (
        <div className={dropdownCls}>
          <div className="sticky top-0 bg-surface border-b border-line p-1.5">
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={searchPlaceholder}
              className="w-full px-2 py-1 text-[12px] font-mono border border-line rounded-sm bg-paper text-ink focus:outline-none focus:border-accent"
            />
          </div>
          {filtered.length > 0 ? (
            filtered.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
                className={itemCls(o.value === value)}
              >
                {o.label}
              </button>
            ))
          ) : (
            <div className="px-3 py-2 text-[12px] text-dim italic">{emptyText}</div>
          )}
        </div>
      )}
    </div>
  );
}

interface ComboBoxProps {
  options: SelectOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  disabled?: boolean;
  className?: string;
}

/**
 * Single-select dropdown that also allows free-form input: the value is not
 * limited to `options`. Typing in the search box immediately becomes the value,
 * while the list below filters `options` by substring (case-insensitive against
 * each option's `value` and `label`). Clicking a list item or the
 * "use «input»" affordance sets the value and closes the dropdown.
 */
export function ComboBox({
  options,
  value,
  onChange,
  placeholder = '请选择或输入…',
  searchPlaceholder = '搜索或输入…',
  disabled = false,
  className = '',
}: ComboBoxProps) {
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useDropdown(ref);

  useEffect(() => {
    if (open) {
      const id = requestAnimationFrame(() => inputRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
  }, [open]);

  const q = value.trim().toLowerCase();
  const filtered = q
    ? options.filter(
        (o) => o.value.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
      )
    : options;
  const exactMatch = options.some((o) => o.value === value);
  const showCustom = value.trim().length > 0 && !exactMatch;

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={triggerCls(disabled)}
      >
        <span className={value ? 'text-ink truncate' : 'text-dim truncate'}>
          {value || placeholder}
        </span>
        <span className="flex items-center gap-1 shrink-0">
          {value.trim().length > 0 && !disabled && (
            <span
              role="button"
              tabIndex={-1}
              onClick={(e) => { e.stopPropagation(); onChange(''); }}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onChange(''); } }}
              className="w-3.5 h-3.5 rounded-full border border-line flex items-center justify-center text-[10px] leading-none text-dim hover:text-ink cursor-pointer"
              aria-label="清空"
            >
              ×
            </span>
          )}
          <span className="text-[10px] text-dim">{open ? '▲' : '▼'}</span>
        </span>
      </button>

      {open && (
        <div className={dropdownCls}>
          <div className="sticky top-0 bg-surface border-b border-line p-1.5">
            <input
              ref={inputRef}
              value={value}
              onChange={(e) => onChange(e.target.value)}
              placeholder={searchPlaceholder}
              className="w-full px-2 py-1 text-[12px] font-mono border border-line rounded-sm bg-paper text-ink focus:outline-none focus:border-accent"
            />
          </div>
          {filtered.map((o) => (
            <button
              key={o.value}
              type="button"
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
              className={itemCls(o.value === value)}
            >
              {o.label}
            </button>
          ))}
          {showCustom && (
            <button
              type="button"
              onClick={() => {
                onChange(value);
                setOpen(false);
              }}
              className={`${itemCls(false)} font-mono`}
            >
              + 使用 “{value}”
            </button>
          )}
        </div>
      )}
    </div>
  );
}

interface MultiSelectProps {
  options: string[];
  value: string[];
  onChange: (value: string[]) => void;
  placeholder?: string;
  className?: string;
}

export function MultiChipSelect({
  options,
  value,
  onChange,
  placeholder = '请选择…',
  className = '',
}: MultiSelectProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdown(ref);

  const toggle = (opt: string) => {
    onChange(value.includes(opt) ? value.filter((v) => v !== opt) : [...value, opt]);
  };

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={triggerCls(false)}
      >
        {value.length > 0 ? (
          <span className="flex flex-wrap gap-1 text-ink">
            {value.map((v) => (
              <span key={v} className="inline-flex items-center gap-0.5 bg-accent-soft border border-accent rounded-full px-1.5 py-px text-[10px] font-mono text-ink">
                {v}
                <span
                  role="button"
                  tabIndex={-1}
                  onClick={(e) => { e.stopPropagation(); toggle(v); }}
                  onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); toggle(v); } }}
                  className="text-dim hover:text-ink cursor-pointer ml-0.5"
                >
                  ×
                </span>
              </span>
            ))}
          </span>
        ) : (
          <span className="text-dim truncate">{placeholder}</span>
        )}
        <span className="text-[10px] text-dim shrink-0">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className={dropdownCls}>
          {options.map((o) => {
            const checked = value.includes(o);
            return (
              <button
                key={o}
                type="button"
                onClick={() => toggle(o)}
                className={`w-full text-left px-3 py-1.5 text-[12px] flex items-center gap-2 transition-colors ${
                  checked ? 'bg-accent-soft text-ink' : 'text-ink hover:bg-paper-2'
                }`}
              >
                <span className={`w-3.5 h-3.5 border rounded-sm flex items-center justify-center text-[10px] shrink-0 ${
                  checked ? 'border-accent bg-accent text-surface' : 'border-line'
                }`}>
                  {checked && '✓'}
                </span>
                {o}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function GroupedSelect({
  groups,
  value,
  onChange,
  placeholder = '请选择…',
  disabled = false,
  className = '',
}: GroupedSelectProps) {
  const ref = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useDropdown(ref);

  const allOptions = groups.flatMap((g) => g.options);
  const selected = allOptions.find((o) => o.value === value);

  return (
    <div ref={ref} className={`relative ${className}`}>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        className={triggerCls(disabled)}
      >
        <span className={selected ? 'text-ink truncate' : 'text-dim truncate'}>
          {selected?.label ?? placeholder}
        </span>
        <span className="text-[10px] text-dim shrink-0">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
        <div className={dropdownCls}>
          {groups.map((g) => (
            <div key={g.label}>
              <div className="px-3 py-1 text-[10px] font-mono text-dim uppercase tracking-wide bg-paper-2 sticky top-0">
                {g.label}
              </div>
              {g.options.map((o) => (
                <button
                  key={o.value}
                  type="button"
                  onClick={() => {
                    onChange(o.value);
                    setOpen(false);
                  }}
                  className={itemCls(o.value === value)}
                >
                  {o.label}
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
