import type { ReactNode } from 'react';

interface Props {
  label: string;
  children: ReactNode;
  align?: 'left' | 'center' | 'right';
}

/** Compact hover/focus hint for composer controls. */
export function HoverHint({ label, children, align = 'center' }: Props) {
  const alignment =
    align === 'left'
      ? 'left-0'
      : align === 'right'
        ? 'right-0'
        : 'left-1/2 -translate-x-1/2';

  return (
    <span className="group/hover-hint relative inline-flex shrink-0">
      {children}
      <span
        role="tooltip"
        className={`pointer-events-none invisible absolute bottom-full ${alignment} z-50 mb-2 w-max max-w-[220px] rounded-md bg-ink px-2 py-1 font-sans text-[11px] font-normal leading-[1.4] text-surface opacity-0 shadow-lg transition-[opacity,transform,visibility] duration-150 translate-y-0.5 group-hover/hover-hint:visible group-hover/hover-hint:translate-y-0 group-hover/hover-hint:opacity-100 group-focus-within/hover-hint:visible group-focus-within/hover-hint:translate-y-0 group-focus-within/hover-hint:opacity-100 group-has-[[aria-expanded=true]]/hover-hint:invisible group-has-[[aria-expanded=true]]/hover-hint:opacity-0`}
      >
        {label}
      </span>
    </span>
  );
}
