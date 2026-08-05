/**
 * SchedulesPanel — the 定时任务 list, embedded as a tab on the App list page.
 *
 * Read + manage only (enable toggle, delete with inline confirm); creation now
 * lives on the App detail page's gear menu, so this panel carries no page head
 * and no 新建任务 button.
 */
import { useEffect, useMemo, useState } from 'react';
import { Dialog } from '../../admin/components/Dialog';
import { useCopied, CopyIcon } from '../CopyButton';
import { ActionMenu, type ActionMenuItem } from '../shell/ActionMenu';
import { api, ApiError } from '../../api/client';
import type { App, CronJob } from '../../api/types';
import { useSchedules } from '../../hooks/useSchedules';
import { useTranslation } from '../../i18n';
import { formatDateTime } from '../../i18n/dates';

// The path a webhook trigger is invoked at — same template cron-runner emits.
// The row only carries the id, so the full URL is rebuilt from it here.
const WEBHOOK_PATH_TEMPLATE = '/api/bff/v1/triggers/{id}/webhook';

function webhookUrl(id: string): string {
  const origin = typeof window !== 'undefined' ? window.location.origin : '';
  return origin + WEBHOOK_PATH_TEMPLATE.replace('{id}', id);
}

// ─── Copy affordance ─────────────────────────────────────────────────────────

function CopyBtn({ text, ariaLabel }: { text: string; ariaLabel: string }) {
  const { t } = useTranslation();
  const { copied, copy } = useCopied();
  return (
    <button
      type="button"
      onClick={() => copy(text)}
      aria-label={ariaLabel}
      className="cw-btn cw-btn-sm shrink-0 inline-flex items-center gap-1 text-[11px]"
    >
      <CopyIcon />
      {copied ? t('schedules.copied') : t('schedules.copy')}
    </button>
  );
}

// ─── Trigger-type badge ───────────────────────────────────────────────────────

function TriggerBadge({ type }: { type: string }) {
  const { t } = useTranslation();
  const isWebhook = type === 'webhook';
  const cls = isWebhook ? 'bg-purple-100 text-purple-700' : 'bg-blue-100 text-blue-700';
  const label = t(isWebhook ? 'schedules.type.webhook' : 'schedules.type.schedule');
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded-sm text-[11px] font-mono ${cls}`}>
      {label}
    </span>
  );
}

// ─── Status badge ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string | null }) {
  const { t } = useTranslation();
  if (!status) return <span className="text-dim text-[12px]">{t('schedules.status.none')}</span>;

  const colors: Record<string, string> = {
    ok:      'bg-green-100 text-green-700',
    error:   'bg-red-100 text-red-700',
    skipped: 'bg-yellow-100 text-yellow-700',
  };
  const cls = colors[status] ?? 'bg-paper-2 text-ink-2';
  const label = t(`schedules.status.${status}` as string, undefined) ?? status;

  return (
    <span className={`inline-block px-1.5 py-0.5 rounded-sm text-[11px] font-mono ${cls}`}>
      {label}
    </span>
  );
}

// ─── Delete confirm (RewindConfirmModal style) ───────────────────────────────

function DeleteConfirmDialog({
  name,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  name: string;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Dialog
      title={t('schedules.delete.title')}
      onClose={onCancel}
      dismissable={!busy}
      className="w-full max-w-[420px]"
    >
      <p className="text-[13px] leading-[1.6] text-ink-2">
        {t('schedules.delete.body', { name })}
      </p>
      {error && (
        <div className="px-3 py-2 border border-red-300 bg-red-50 rounded-md text-[12.5px] text-red-700">
          {error}
        </div>
      )}
      <div className="flex items-center justify-end gap-2 pt-1">
        <button className="cw-btn cw-btn-sm" onClick={onCancel} disabled={busy}>
          {t('schedules.delete.cancel')}
        </button>
        <button
          className="cw-btn cw-btn-accent cw-btn-sm"
          onClick={onConfirm}
          disabled={busy}
        >
          {busy ? t('schedules.delete.deleting') : t('schedules.delete.confirm')}
        </button>
      </div>
    </Dialog>
  );
}

// ─── Panel ────────────────────────────────────────────────────────────────────

export function SchedulesPanel() {
  const { t, lang } = useTranslation();
  const { schedules, loading, error, refresh } = useSchedules();

  const [apps, setApps] = useState<App[]>([]);

  // Copy affordance for the actions menu — the menu closes on select, so the
  // transient "已复制" confirmation surfaces in the panel banner instead.
  const { copied, copy } = useCopied();

  // Delete confirm state
  const [deleteTarget, setDeleteTarget] = useState<CronJob | null>(null);
  const [deletebusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  // Toggle error toast (ephemeral)
  const [toggleError, setToggleError] = useState('');

  // Secret rotation — revealed plaintext is kept per-row (shown once, until the
  // panel unmounts or the row is rotated again).
  const [rotatedSecret, setRotatedSecret] = useState<Record<string, string>>({});
  const [rotatingId, setRotatingId] = useState<string | null>(null);
  const [rotateError, setRotateError] = useState<Record<string, string>>({});

  // App name lookup — the cron job only carries app_id.
  const appMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of apps) m[a.id] = a.name;
    return m;
  }, [apps]);

  useEffect(() => {
    let cancelled = false;
    api.apps
      .list()
      .then((res) => { if (!cancelled) setApps(res.items ?? []); })
      .catch(() => {/* failure-open: fall back to the raw app_id */});
    return () => { cancelled = true; };
  }, []);

  const handleToggle = async (job: CronJob) => {
    setToggleError('');
    try {
      await api.schedules.update(job.id, { enabled: !job.enabled });
      refresh();
    } catch {
      setToggleError(t('schedules.toggle.error'));
    }
  };

  const handleRotate = async (job: CronJob) => {
    setRotatingId(job.id);
    setRotateError((prev) => ({ ...prev, [job.id]: '' }));
    try {
      const res = await api.schedules.rotateSecret(job.id);
      setRotatedSecret((prev) => ({ ...prev, [job.id]: res.webhook_secret }));
    } catch {
      setRotateError((prev) => ({ ...prev, [job.id]: t('schedules.webhook.rotate.error') }));
    } finally {
      setRotatingId(null);
    }
  };

  const handleDeleteConfirm = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    setDeleteError('');
    try {
      await api.schedules.remove(deleteTarget.id);
      setDeleteTarget(null);
      refresh();
    } catch (e) {
      setDeleteError(e instanceof ApiError ? e.message : '删除失败，请稍后重试');
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      {/* Error banner */}
      {error && (
        <div className="px-4 py-2.5 border border-red-300 bg-red-50 rounded-md text-[13px] text-red-700">
          {error}
        </div>
      )}
      {toggleError && (
        <div className="px-4 py-2.5 border border-yellow-300 bg-yellow-50 rounded-md text-[13px] text-yellow-700">
          {toggleError}
        </div>
      )}
      {copied && (
        <div className="px-4 py-2.5 border border-green-300 bg-green-50 rounded-md text-[13px] text-green-700">
          {t('schedules.copied')}
        </div>
      )}

      {loading && (
        <div className="font-mono text-[12px] text-dim">{t('common.loading')}…</div>
      )}

      {!loading && schedules.length === 0 && !error && (
        <div className="border border-line border-dashed bg-surface rounded-md p-4 flex items-center justify-center min-h-[120px]">
          <div className="font-mono italic text-dim text-[12px] text-center leading-[1.6]">
            {t('schedules.emptyHint')}
          </div>
        </div>
      )}

      {!loading && schedules.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse text-[13px]">
            <thead>
              <tr className="border-b border-line">
                {[
                  'schedules.col.name',
                  'schedules.col.app',
                  'schedules.col.type',
                  'schedules.col.trigger',
                  'schedules.col.timezone',
                  'schedules.col.enabled',
                  'schedules.col.lastRun',
                  'schedules.col.nextRun',
                  'schedules.col.status',
                  'schedules.col.actions',
                ].map((k) => (
                  <th
                    key={k}
                    className="text-left px-3 py-2 font-mono text-[11px] uppercase tracking-[0.08em] text-dim whitespace-nowrap"
                  >
                    {t(k)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {schedules.map((job) => (
                <tr key={job.id} className="border-b border-line-2 hover:bg-paper-2 transition-colors">
                  {/* Name */}
                  <td className="px-3 py-2.5 font-medium text-ink max-w-[160px] truncate">
                    {job.name}
                  </td>
                  {/* App */}
                  <td className="px-3 py-2.5 text-ink-2 max-w-[140px] truncate">
                    {appMap[job.app_id] ?? <span className="font-mono text-dim">{job.app_id.slice(0, 8)}</span>}
                  </td>
                  {/* Type badge */}
                  <td className="px-3 py-2.5">
                    <TriggerBadge type={job.trigger_type} />
                  </td>
                  {/* Trigger rule — cron expr for schedules, webhook URL + rotate for webhooks */}
                  <td className="px-3 py-2.5">
                    {job.trigger_type === 'webhook' ? (
                      <div className="flex flex-col gap-1.5 max-w-[240px]">
                        <code
                          className="block min-w-0 truncate font-mono text-[11px] text-ink"
                          title={webhookUrl(job.id)}
                        >
                          {webhookUrl(job.id)}
                        </code>
                        {rotateError[job.id] && (
                          <div className="text-[11px] text-red-600">{rotateError[job.id]}</div>
                        )}
                        {rotatedSecret[job.id] && (
                          <div className="flex flex-col gap-1 border border-yellow-300 bg-yellow-50 rounded-sm p-2">
                            <div className="text-[11px] text-yellow-800 leading-[1.45]">
                              {t('schedules.webhook.rotate.warn')}
                            </div>
                            <div className="flex items-center gap-1.5">
                              <code className="flex-1 min-w-0 break-all font-mono text-[11px] text-ink">
                                {rotatedSecret[job.id]}
                              </code>
                              <CopyBtn text={rotatedSecret[job.id]} ariaLabel={t('schedules.webhook.secret')} />
                            </div>
                          </div>
                        )}
                      </div>
                    ) : (
                      <span className="font-mono text-[11.5px] text-ink">{job.cron_expression}</span>
                    )}
                  </td>
                  {/* Timezone — meaningless for webhooks */}
                  <td className="px-3 py-2.5 text-dim text-[12px] whitespace-nowrap">
                    {job.trigger_type === 'webhook' ? '—' : job.timezone}
                  </td>
                  {/* Enabled toggle */}
                  <td className="px-3 py-2.5">
                    <button
                      role="switch"
                      aria-checked={job.enabled}
                      onClick={() => handleToggle(job)}
                      className={[
                        'relative inline-flex h-4.5 w-8 shrink-0 rounded-full border transition-colors focus:outline-none',
                        job.enabled
                          ? 'bg-accent border-accent'
                          : 'bg-paper-2 border-line',
                      ].join(' ')}
                      style={{ width: 32, height: 18 }}
                    >
                      <span
                        className="inline-block h-3 w-3 translate-y-[1.5px] rounded-full bg-white shadow transition-transform"
                        style={{
                          transform: `translateX(${job.enabled ? 14 : 2}px) translateY(1.5px)`,
                        }}
                      />
                    </button>
                  </td>
                  {/* Last run */}
                  <td className="px-3 py-2.5 text-dim text-[12px] whitespace-nowrap">
                    {job.last_run_at
                      ? formatDateTime(job.last_run_at, lang)
                      : <span className="text-dim">—</span>}
                  </td>
                  {/* Next run */}
                  <td className="px-3 py-2.5 text-dim text-[12px] whitespace-nowrap">
                    {job.next_run_at
                      ? formatDateTime(job.next_run_at, lang)
                      : <span className="text-dim">—</span>}
                  </td>
                  {/* Status */}
                  <td className="px-3 py-2.5">
                    <div className="flex flex-col gap-0.5">
                      <StatusBadge status={job.last_status} />
                      {job.last_status === 'error' && job.last_error && (
                        <span
                          className="text-[11px] text-red-600 max-w-[200px] truncate"
                          title={job.last_error}
                        >
                          {job.last_error}
                        </span>
                      )}
                    </div>
                  </td>
                  {/* Actions — copy / rotate / delete consolidated behind one
                      gear menu so the row stays narrow and nothing is clipped. */}
                  <td className="px-3 py-2.5 text-right">
                    <ActionMenu
                      floating
                      icon="gear"
                      align="right"
                      ariaLabel={t('schedules.col.actions')}
                      items={[
                        ...(job.trigger_type === 'webhook'
                          ? ([
                              {
                                id: 'copy',
                                label: t('schedules.copyUrl'),
                                onSelect: () => copy(webhookUrl(job.id)),
                              },
                              {
                                id: 'rotate',
                                label: rotatingId === job.id
                                  ? t('schedules.webhook.rotating')
                                  : t('schedules.webhook.rotate'),
                                disabled: rotatingId === job.id,
                                onSelect: () => handleRotate(job),
                              },
                            ] as ActionMenuItem[])
                          : []),
                        {
                          id: 'delete',
                          label: t('common.delete'),
                          danger: true,
                          onSelect: () => { setDeleteError(''); setDeleteTarget(job); },
                        },
                      ]}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Delete confirm dialog */}
      {deleteTarget && (
        <DeleteConfirmDialog
          name={deleteTarget.name}
          busy={deletebusy}
          error={deleteError}
          onConfirm={handleDeleteConfirm}
          onCancel={() => { setDeleteTarget(null); setDeleteError(''); }}
        />
      )}
    </div>
  );
}
