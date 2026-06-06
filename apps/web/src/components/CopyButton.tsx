import { useState, useCallback } from 'react';

// ── Shared copy hook ──────────────────────────────────────────────────────────

export function useCopied(duration = 1500) {
  const [copied, setCopied] = useState(false);

  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), duration);
    } catch {
      // Clipboard unavailable (non-secure context, permission denied, etc.) — silently swallow.
    }
  }, [duration]);

  return { copied, copy };
}

// ── Inline SVG copy icon (14×14) ──────────────────────────────────────────────

export function CopyIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 14 14"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      {/* back rect */}
      <rect
        x="4"
        y="1"
        width="9"
        height="9"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
      />
      {/* front rect */}
      <rect
        x="1"
        y="4"
        width="9"
        height="9"
        rx="1.5"
        stroke="currentColor"
        strokeWidth="1.25"
        fill="none"
        style={{ fill: 'var(--paper)' }}
      />
    </svg>
  );
}
