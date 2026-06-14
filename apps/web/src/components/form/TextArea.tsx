/**
 * TextArea — stateless controlled multi-line text input primitive.
 */

interface TextAreaProps {
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

export function TextArea({
  value,
  onChange,
  label,
  help,
  required,
  error,
  placeholder,
  disabled,
  size = 'md',
}: TextAreaProps) {
  const textareaCls =
    size === 'sm'
      ? `w-full min-h-[80px] resize-y border rounded-sm px-2 py-1.5 text-[12px] bg-paper text-ink outline-none focus:border-accent ${error ? 'border-accent' : 'border-line'}`
      : `w-full min-h-[110px] resize-y bg-surface border rounded-md px-3 py-2 text-[14px] leading-[1.5] text-ink outline-none focus:border-ink-2 ${error ? 'border-accent' : 'border-line'}`;

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
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className={textareaCls}
      />
      {error && <div className="text-[11.5px] text-accent">{error}</div>}
    </div>
  );
}
