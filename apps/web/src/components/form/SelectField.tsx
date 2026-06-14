/**
 * SelectField — stateless controlled select primitive wrapping Dropdown.
 *
 * Renders the same "bordered box + inline Dropdown trigger" pattern that
 * AppRunPage uses for 'select' fields. The Dropdown popover opens downward
 * by default (align='left' matches the AppRunPage pattern).
 */
import { Dropdown } from '../shell/Dropdown';

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
  /** Display title for the Dropdown trigger (accessibility). */
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
  const dropdownOptions = options.map((o) => ({ id: o.value, label: o.label }));
  const selected = options.find((o) => o.value === value);

  return (
    <div className="flex flex-col gap-1.5">
      {label && (
        <label className={size === 'sm' ? 'cw-label' : 'text-[13px] font-medium text-ink'}>
          {label}
          {required && <span className="text-accent ml-1">*</span>}
        </label>
      )}
      {help && (
        <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{help}</div>
      )}
      <div
        className={`flex items-center gap-2 bg-surface border rounded-md px-3 py-2 ${error ? 'border-accent' : 'border-line'}`}
      >
        <span className={`${size === 'sm' ? 'text-[12px]' : 'text-[13px]'} text-ink flex-1 truncate`}>
          {selected?.label ?? <span className="text-dim">{placeholder ?? '请选择…'}</span>}
        </span>
        <Dropdown
          value={value}
          options={dropdownOptions}
          onChange={onChange}
          disabled={disabled}
          align="left"
          title={title}
        />
      </div>
      {error && <div className="text-[11.5px] text-accent">{error}</div>}
    </div>
  );
}
