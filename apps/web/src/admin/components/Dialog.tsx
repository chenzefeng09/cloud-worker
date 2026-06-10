import { useEffect, useRef } from 'react';

// ─── Base modal shell ─────────────────────────────────────────────────────────
//
// A bare, accessible modal frame used by ConfirmDialog / PromptDialog and any
// page-level form dialog. Handles backdrop click, ESC-to-close, scroll lock and
// initial focus so callers don't reimplement them. Visual conventions match the
// admin console (bg-black/40 backdrop, border-line / bg-surface card).

interface DialogProps {
  /** Accessible label shown as the uppercase mono heading. */
  title: string;
  /** Called when the user dismisses via backdrop, ESC, or close affordance. */
  onClose: () => void;
  /** Disable dismissal (e.g. while an async action is in flight). */
  dismissable?: boolean;
  /** Ref to focus on mount; defaults to the dialog container. */
  initialFocusRef?: React.RefObject<HTMLElement | null>;
  className?: string;
  children: React.ReactNode;
}

export function Dialog({
  title,
  onClose,
  dismissable = true,
  initialFocusRef,
  className = 'w-full max-w-md',
  children,
}: DialogProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && dismissable) {
        e.stopPropagation();
        onClose();
      }
    };
    document.addEventListener('keydown', onKey);
    // Lock background scroll while the modal is open.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [onClose, dismissable]);

  useEffect(() => {
    (initialFocusRef?.current ?? containerRef.current)?.focus();
  }, [initialFocusRef]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={() => dismissable && onClose()}
    >
      <div
        ref={containerRef}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onClick={(e) => e.stopPropagation()}
        className={`border border-line rounded-md p-5 bg-surface flex flex-col gap-3 focus:outline-none ${className}`}
      >
        <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">{title}</div>
        {children}
      </div>
    </div>
  );
}
