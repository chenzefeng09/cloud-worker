/**
 * VersionHistoryDialog - 「版本历史」弹窗。
 *
 * AppDetailPage hero 的「版本历史」按钮打开此弹窗，承载原来铺在页面上的版本列表。
 * 入口对所有用户可见（不限于作者/管理员），纯展示，无写操作。
 */
import { Dialog } from '../../admin/components/Dialog';
import { Chip } from '../shell/Chip';
import { VersionStatusChip } from './VersionStatusChip';
import { formatAbsoluteTimeZh } from '../../lib/format';
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
    <Dialog title="版本历史" onClose={onClose} className="w-full max-w-[560px]">
      {versions.length === 0 ? (
        <div className="font-mono italic text-dim text-[12px]">暂无版本。</div>
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
                {app.current_version_id === v.id && <Chip>当前</Chip>}
                <span className="font-mono text-[10.5px] text-dim ml-auto">
                  {formatAbsoluteTimeZh(v.created_at)}
                </span>
              </div>
              <div className="font-mono text-[10.5px] text-dim flex flex-col gap-0.5">
                {v.submitted_by && (
                  <span>
                    提交人 {v.submitted_by}
                    {v.submitted_at ? ` · ${formatAbsoluteTimeZh(v.submitted_at)}` : ''}
                  </span>
                )}
                {v.reviewed_by && (
                  <span>
                    审核人 {v.reviewed_by}
                    {v.reviewed_at ? ` · ${formatAbsoluteTimeZh(v.reviewed_at)}` : ''}
                  </span>
                )}
                {v.review_note && <span className="text-ink-2">备注：{v.review_note}</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </Dialog>
  );
}
