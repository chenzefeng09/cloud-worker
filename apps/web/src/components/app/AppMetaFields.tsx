import { useEffect, useRef, useState } from 'react';
import { Dropdown } from '../shell/Dropdown';
import type { AppVisibility } from '../../api/types';

// ─── Shared App meta field UI ─────────────────────────────────────────────────
//
// The icon emoji picker, visibility options and input class are reused by both
// the create flow (CreateAppModal) and the app-info block on the editor page's
// first step, so the two stay visually identical. Kept here rather than inlined
// so a future consumer (e.g. an admin quick-edit) can drop them in without
// copy-paste.

export const EMOJI_PRESETS = [
  '📝', '📊', '📋', '📁', '📎', '📌',
  '🔍', '🔧', '🛠', '⚙️', '🧩', '🧪',
  '🤖', '🧠', '💡', '🎯', '🚀', '✨',
  '📈', '📉', '💬', '📣', '🔔', '📅',
  '🏷', '🗂', '📦', '🔗', '🌐', '🛡',
  '💼', '🎨', '🖊', '📐', '🧮', '⚡',
];

/** Shared text-input class token used across the app dialogs. */
export const appInputCls =
  'border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent';

export const VISIBILITY_OPTIONS: { id: AppVisibility; label: string }[] = [
  { id: 'private', label: '私有（仅自己）' },
  { id: 'tenant', label: '租户（提交发布后可见）' },
];

export function EmojiPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (emoji: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-10 h-10 border border-line rounded-md flex items-center justify-center text-[18px] bg-paper hover:border-ink-2 transition-colors cursor-pointer"
        title="选择图标"
      >
        {value || '◐'}
      </button>
      {open && (
        <div className="absolute top-full left-0 mt-1 z-50 border border-line rounded-lg bg-surface shadow-[0_8px_24px_-6px_rgba(15,17,20,0.18)] p-2 w-[240px]">
          <div className="grid grid-cols-6 gap-1">
            {EMOJI_PRESETS.map((emoji) => (
              <button
                key={emoji}
                type="button"
                onClick={() => {
                  onChange(emoji);
                  setOpen(false);
                }}
                className={`w-8 h-8 flex items-center justify-center rounded-md text-[16px] hover:bg-paper transition-colors ${
                  value === emoji ? 'bg-paper ring-1 ring-ink-2' : ''
                }`}
              >
                {emoji}
              </button>
            ))}
          </div>
          <div className="mt-2 pt-2 border-t border-line">
            <input
              type="text"
              className="w-full border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent font-mono"
              value={value}
              onChange={(e) => onChange(e.target.value)}
              placeholder="自定义输入"
              maxLength={4}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/** Visibility dropdown wrapped in the bordered input-like frame used by the
 *  create / edit dialogs. */
export function VisibilitySelect({
  value,
  onChange,
}: {
  value: AppVisibility;
  onChange: (v: AppVisibility) => void;
}) {
  return (
    <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center w-fit">
      <Dropdown
        value={value}
        options={VISIBILITY_OPTIONS}
        onChange={(v) => onChange(v as AppVisibility)}
        align="left"
        title="可见性"
      />
    </div>
  );
}
