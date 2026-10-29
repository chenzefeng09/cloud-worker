import type { AppVersionStatus } from '../../api/types';

// ─── App version status chip ──────────────────────────────────────────────────
//
// Renders the draft / pending_review / published / rejected lifecycle as a
// colored dot + Chinese label. Shared by AppListPage (my-apps cards),
// AppDetailPage (header), and VersionHistoryDialog (version list) so the
// styling stays consistent. The
// dot colors map to the design tokens: dim = draft, warn = pending, ok =
// published, accent = rejected.

const STATUS_LABEL: Record<AppVersionStatus, string> = {
  draft: 'Draft',
  pending_review: 'In review',
  published: 'Published',
  rejected: 'Rejected',
};

const STATUS_DOT: Record<AppVersionStatus, string> = {
  draft: 'bg-dim',
  pending_review: 'bg-warn',
  published: 'bg-ok',
  rejected: 'bg-accent',
};

export function appVersionStatusZh(status: AppVersionStatus): string {
  return STATUS_LABEL[status] ?? status;
}

export function VersionStatusChip({ status }: { status: AppVersionStatus }) {
  return (
    <span className="inline-flex items-center gap-1.5 border border-line rounded-sm px-1.5 py-0.5 bg-paper-2 text-[11px] text-ink-2 font-mono">
      <span className={`w-1.5 h-1.5 rounded-full ${STATUS_DOT[status] ?? 'bg-dim'}`} />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}
