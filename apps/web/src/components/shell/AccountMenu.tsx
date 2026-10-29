import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useChatStore } from '../../stores/chatStore';

/**
 * Bottom-of-sidebar account control. The trigger row (avatar initial + email)
 * opens a popover UPWARD — same pattern as the composer Dropdown, since this
 * lives at the very bottom of the rail and a downward menu would be clipped.
 * Closes on outside click and on Escape.
 */
export function AccountMenu() {
  const userProfile = useChatStore((s) => s.userProfile);
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDocClick);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDocClick);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const initial = userProfile?.display_name?.[0]?.toUpperCase() ?? 'Y';
  const email = userProfile?.email ?? 'you@acme.co';

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="menu"
        aria-expanded={open}
        className={[
          'flex w-full items-center gap-2 mt-1 rounded-sm text-left text-[12.5px]',
          open ? 'text-ink' : 'text-ink-2 hover:text-ink',
        ].join(' ')}
      >
        <span className="inline-flex w-[22px] h-[22px] items-center justify-center rounded-full border border-line bg-surface font-mono text-[10px] font-semibold text-ink shrink-0">
          {initial}
        </span>
        <span className="truncate">{email}</span>
      </button>

      {open && (
        <div
          role="menu"
          className="absolute bottom-full mb-1.5 left-0 right-0 z-30 bg-surface border border-line rounded-lg shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-1 animate-pop-in origin-bottom"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate('/profile');
            }}
            className="w-full text-left px-2 py-1.5 rounded-md text-[12.5px] text-ink-2 hover:bg-paper hover:text-ink"
          >
            My account
          </button>
        </div>
      )}
    </div>
  );
}
