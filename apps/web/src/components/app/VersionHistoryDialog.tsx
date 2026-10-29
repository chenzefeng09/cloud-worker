/**
 * VersionHistoryDialog - the "Version history" dialog.
 *
 * Opened from the "Version history" button in the AppDetailPage hero; hosts the version list that used to live on the page.
 * Visible to all users (not just authors/admins); read-only, no write actions.
 */
import { Dialog } from '../../admin/components/Dialog';
import { Chip } from '../shell/Chip';
import { VersionStatusChip } from './VersionStatusChip';
import { formatAbsoluteTime } from '../../lib/format';
import type { App } from '../../api/types';

interface Props {
  /** The App whose versions are listed. */
  app: App;
  /** Whether the dialog is shown. */
  open: boolean;
  onClose: () => void;
}

export function VersionHistoryDialog({ app, open, onClose }: Props) {
  if (!open) return null;
  // Newest-first, same ordering the old inline section used.
  const versions = [...(app.versions ?? [])].reverse();

  return (
    <Dialog title="Version history" onClose={onClose} className="w-full max-w-[560px]">
      {versions.length === 0 ? (
        <div className="font-mono italic text-dim text-[12px]">No versions yet.</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {versions.map((v) => (
            <div
              key={v.id}
              className="border border-line rounded-md bg-paper px-3 py-2.5 flex flex-col gap-1.5"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-[12.5px] text-ink">v{v.version}</span>
                <VersionStatusChip status={v.status} />
                {app.current_version_id === v.id && <Chip>Current</Chip>}
                <span className="font-mono text-[10.5px] text-dim ml-auto">
                  {formatAbsoluteTime(v.created_at)}
                </span>
              </div>
              <div className="font-mono text-[10.5px] text-dim flex flex-col gap-0.5">
                {v.submitted_by && (
                  <span>
                    Submitted by {v.submitted_by}
                    {v.submitted_at ? ` · ${formatAbsoluteTime(v.submitted_at)}` : ''}
                  </span>
                )}
                {v.reviewed_by && (
                  <span>
                    Reviewed by {v.reviewed_by}
                    {v.reviewed_at ? ` · ${formatAbsoluteTime(v.reviewed_at)}` : ''}
                  </span>
                )}
                {v.review_note && <span className="text-ink-2">Note: {v.review_note}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
