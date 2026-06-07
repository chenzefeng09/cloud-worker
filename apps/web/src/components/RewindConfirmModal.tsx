import { useEffect } from 'react';
import type { Message } from '../api/types';

/**
 * App-level confirmation modal for the "rewind to this step" action.
 *
 * Replaces the native window.confirm: rewinding deletes this message and
 * everything after it, then prefills the message content back into the input
 * box. The modal stays open on failure so the user notices the error.
 */
export function RewindConfirmModal({
  message,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  message: Message;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  // Close on Esc (but not while a delete is in flight).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [busy, onCancel]);

  // Short preview of the message being rewound to, for context.
  const preview =
    message.content.length > 120
      ? `${message.content.slice(0, 120)}…`
      : message.content;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => {
        if (!busy) onCancel();
      }}
    >
      <div
        className="w-full max-w-[420px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">回退到这一步</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors disabled:opacity-40"
            onClick={onCancel}
            disabled={busy}
          >
            ✕
          </button>
        </div>

        <div className="flex flex-col gap-3 px-4 py-4">
          <p className="text-[13px] leading-[1.6] text-ink-2">
            这之后的对话（包括该消息本身）会被删除，该消息内容会回填到输入框以便重新编辑。
          </p>
          {preview && (
            <div className="px-3 py-2 border border-line bg-paper-2 rounded-md text-[12px] text-ink-2 whitespace-pre-wrap break-words">
              {preview}
            </div>
          )}
          {error && (
            <div className="px-3 py-2 border border-red-300 bg-red-50 rounded-md text-[12.5px] text-red-700">
              {error}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onCancel} disabled={busy}>
            取消
          </button>
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? '回退中…' : '确认回退'}
          </button>
        </div>
      </div>
    </div>
  );
}
