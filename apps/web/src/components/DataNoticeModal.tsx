import { Dialog } from '../admin/components/Dialog';

/**
 * First-use data-usage notice shown the first time a user enters the chat page.
 *
 * For transparency / compliance, users are told that their conversations with
 * the agent are retained and may be replayed by administrators for auditing.
 * Once acknowledged, the dismissal is persisted to localStorage so the modal
 * never appears again on this device (see DATA_NOTICE_ACK_KEY).
 *
 * The overlay shell (backdrop, ESC, scroll lock, focus) is delegated to the
 * shared `Dialog` base so this stays visually consistent with other app modals.
 */
export function DataNoticeModal({ onAck }: { onAck: () => void }) {
  return (
    <Dialog
      title="Data usage notice"
      onClose={onAck}
      className="w-full max-w-[440px]"
    >
      <p className="text-[13px] leading-[1.6] text-ink-2">
        To ensure service quality and support security audits, your conversations with the agent are retained and may be replayed by administrators for auditing. Retention period: 30 days.
      </p>

      <div className="flex items-center justify-end gap-2 pt-1">
        <button className="cw-btn cw-btn-accent cw-btn-sm" onClick={onAck}>
          Got it
        </button>
      </div>
    </Dialog>
  );
}
