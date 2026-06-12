import { Dialog } from '../admin/components/Dialog';
import type { Message } from '../api/types';

/**
 * App-level confirmation modal for the "rewind to this step" action.
 *
 * Replaces the native window.confirm: rewinding deletes this message and
 * everything after it, then prefills the message content back into the input
 * box. The modal stays open on failure so the user notices the error.
 *
 * The overlay shell (backdrop, ESC-to-close, scroll lock, focus) is delegated
 * to the shared `Dialog` base; this component only owns the confirm body and
 * the busy / error states. ESC and backdrop dismissal are disabled while a
 * delete is in flight via `dismissable={!busy}`.
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
  // Short preview of the message being rewound to, for context.
  const preview =
    message.content.length > 120
      ? `${message.content.slice(0, 120)}…`
      : message.content;

  return (
    <Dialog
      title="回退到这一步"
      onClose={onCancel}
      dismissable={!busy}
      className="w-full max-w-[420px]"
    >
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

      <div className="flex items-center justify-end gap-2 pt-1">
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
    </Dialog>
  );
}
