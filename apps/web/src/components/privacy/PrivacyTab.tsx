import { formatRelativeTime } from '../../lib/format';
import { DataErasureSection } from './DataErasureSection';
import { DataExportSection } from './DataExportSection';
import { MemorySection } from './MemorySection';
import { PrivacyNoticeDialog } from './PrivacyNoticeDialog';
import { FALLBACK_NOTICE_VERSION, useDataNotice } from './useDataNotice';
import { btn, PrivacySection, Prose } from './ui';

/**
 * Profile → "Privacy & data": the privacy notice (reread / acknowledge),
 * memory controls, data download and data deletion.
 */
export function PrivacyTab({ onErased }: { onErased?: () => void }) {
  const n = useDataNotice({ autoOpen: false });

  const status = n.loading
    ? 'Loading…'
    : !n.notice
      ? "Couldn't load the full notice right now — a short summary is available."
      : n.needsAck
        ? `Version ${n.notice.version} — you haven't acknowledged this version yet.`
        : n.acknowledged?.acknowledged_at && n.acknowledged.version !== FALLBACK_NOTICE_VERSION
          ? `Version ${n.notice.version} — acknowledged ${formatRelativeTime(n.acknowledged.acknowledged_at)}.`
          : `Version ${n.notice.version}`;

  return (
    <div className="flex flex-col gap-3.5">
      <PrivacySection
        title="Privacy notice"
        action={
          <button type="button" className={n.needsAck && n.notice ? btn.primary : btn.base} onClick={n.show} disabled={n.loading}>
            {n.needsAck && n.notice ? 'Review notice' : 'Read notice'}
          </button>
        }
      >
        <Prose>Who is responsible for your data, what it's used for, how long it's kept, who processes it, and your rights.</Prose>
        <div className="font-mono text-[10.5px] text-dim">{status}</div>
      </PrivacySection>

      <MemorySection platformMemoryEnabled={n.notice?.memory?.enabled} />
      <DataExportSection />
      <DataErasureSection onFinished={onErased} />

      {n.open && (
        <PrivacyNoticeDialog
          notice={n.notice}
          onClose={n.dismiss}
          onAcknowledge={n.needsAck && n.notice ? n.acknowledge : undefined}
          busy={n.ackBusy}
          error={n.ackError}
          showPrivacyLink={false}
        />
      )}
    </div>
  );
}
