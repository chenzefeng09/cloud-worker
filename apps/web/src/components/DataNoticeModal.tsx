import { PrivacyNoticeDialog } from './privacy/PrivacyNoticeDialog';
import { useDataNotice } from './privacy/useDataNotice';

/**
 * Privacy notice gate for the signed-in workspace (GDPR Art. 13 information).
 *
 * Mounted once in the workspace shell, under its DataNoticeProvider, so it
 * shows on whichever page the user opens first — before anything they type is
 * processed — not only in chat. Renders nothing until it knows whether the
 * signed-in user has acknowledged the *current* notice version, then shows the
 * notice if not. Content comes from `GET /privacy/notice` (controller / DPO,
 * purposes and legal bases, retention, memory, processors and transfers,
 * rights), with a built-in summary if that endpoint fails. "Got it" records
 * the acknowledgement server-side, so a new notice version is shown again on
 * every device; ESC / backdrop only closes it until next time.
 *
 * The same dialog is available on demand from Profile → Privacy & data.
 */
export function DataNoticeModal() {
  const n = useDataNotice({ autoOpen: true });
  if (!n.open) return null;
  return (
    <PrivacyNoticeDialog
      notice={n.notice}
      onClose={n.dismiss}
      onAcknowledge={n.acknowledge}
      busy={n.ackBusy}
      error={n.ackError}
    />
  );
}
