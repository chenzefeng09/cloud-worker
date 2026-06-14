/**
 * TextField — stateless controlled single-line text input primitive.
 *
 * Wraps a bare <input type="text"> with canonical class conventions
 * matching AppRunPage user-facing form sizing (14px body, md) and the
 * admin 12px sm variant.
 */

interface TextFieldProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  help?: string;
  required?: boolean;
  error?: string;
  placeholder?: string;
  disabled?: boolean;
  size?: 'sm' | 'md';
}

export function TextField({
  value,
  onChange,
  label,
  help,
  required,
  error,
  placeholder,
  disabled,
  size = 'md',
}: TextFieldProps) {
  const inputCls =
    size === 'sm'
      ? `w-full border rounded-sm px-2 py-1.5 text-[12px] bg-paper text-ink outline-none focus:border-accent ${error ? 'border-accent' : 'border-line'}`
      : `w-full bg-surface border rounded-md px-3 py-2 text-[14px] text-ink outline-none focus:border-ink-2 ${error ? 'border-accent' : 'border-line'}`;

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
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={inputCls}
      />
      {error && <div className="text-[11.5px] text-accent">{error}</div>}
    </div>
  );
}
