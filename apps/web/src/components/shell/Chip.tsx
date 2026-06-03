import React from 'react';

interface ChipProps {
  children: React.ReactNode;
  accent?: boolean;
  dashed?: boolean;
  onClick?: () => void;
  className?: string;
}

export function Chip({ children, accent, dashed, onClick, className }: ChipProps) {
  const cls = ['cw-chip', className].filter(Boolean).join(' ');
  const dataAccent = accent ? '1' : undefined;
  const dataDashed = dashed ? '1' : undefined;

  if (onClick) {
    return (
      <button
        type="button"
        className={cls}
        data-accent={dataAccent}
        data-dashed={dataDashed}
        onClick={onClick}
      >
        {children}
      </button>
    );
  }

  return (
    <span
      className={cls}
      data-accent={dataAccent}
      data-dashed={dataDashed}
    >
      {children}
    </span>
  );
}
