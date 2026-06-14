/**
 * SchedulesPage — user-facing CRUD for CronJobs (定时任务).
 *
 * List: table of cron jobs with enable toggle, last/next run, status badge,
 *       delete with inline confirm.
 * Create dialog: pick published app → name → cron preset/custom → timezone
 *                → SchemaForm for fixed input_values.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { PageHead } from '../components/shell/PageHead';
import { Dialog } from '../admin/components/Dialog';
import { TextField } from '../components/form';
import { SelectField } from '../components/form';
import { SchemaForm } from '../components/form';
import type { SchemaFormValue } from '../components/form';
import { api, ApiError } from '../api/client';
import type { App, AppVersion, CronJob, CronJobCreate, InputField } from '../api/types';
import { useSchedules } from '../hooks/useSchedules';
import { useTranslation } from '../i18n';
import { formatDateTime } from '../i18n/dates';

// ─── Cron presets ─────────────────────────────────────────────────────────────

type PresetKey = 'daily' | 'weekly' | 'hourly' | 'custom';

interface CronPreset {
  key: PresetKey;
  expr: string;
  labelKey: string;
  hint: string;
}

export const CRON_PRESETS: CronPreset[] = [
  { key: 'daily',  expr: '0 9 * * *',   labelKey: 'schedules.preset.daily',  hint: '0 9 * * *' },
  { key: 'weekly', expr: '0 9 * * 1',   labelKey: 'schedules.preset.weekly', hint: '0 9 * * 1' },
  { key: 'hourly', expr: '0 * * * *',   labelKey: 'schedules.preset.hourly', hint: '0 * * * *' },
  { key: 'custom', expr: '',            labelKey: 'schedules.preset.custom',  hint: '' },
];

const TIMEZONE_OPTIONS = [
  { value: 'Asia/Shanghai',   label: 'Asia/Shanghai (CST)' },
  { value: 'UTC',             label: 'UTC' },
  { value: 'America/New_York',label: 'America/New_York (ET)' },
  { value: 'Europe/London',   label: 'Europe/London (GMT/BST)' },
  { value: 'Asia/Tokyo',      label: 'Asia/Tokyo (JST)' },
];

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Resolve the published version of an App (same logic as AppRunPage). */
function resolvePublishedVersion(app: App): AppVersion | null {
  const versions = app.versions ?? [];
  if (app.current_version_id) {
    const v = versions.find((x) => x.id === app.current_version_id);
    if (v && v.status === 'published') return v;
  }
  return versions.find((x) => x.status === 'published') ?? null;
}

/** Build an empty SchemaFormValue prefilled with field defaults. */
function buildDefaultFormValue(schema: InputField[]): SchemaFormValue {
  const text: Record<string, string> = {};
  const multi: Record<string, string[]> = {};
  for (const f of schema) {
    if (f.type === 'multiselect') {
      multi[f.key] = Array.isArray(f.default) ? (f.default as string[]) : [];
    } else if (f.type !== 'file') {
      text[f.key] = typeof f.default === 'string' ? f.default : '';
    }
  }
  return { text, multi, file: {} };
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

// ─── Create dialog ────────────────────────────────────────────────────────────

interface CreateDialogProps {
  apps: App[];
  onClose: () => void;
  onCreated: () => void;
}

function CreateDialog({ apps, onClose, onCreated }: CreateDialogProps) {
  const { t } = useTranslation();

  // Published apps only
  const publishedApps = useMemo(
    () => apps.filter((a) => resolvePublishedVersion(a) !== null),
    [apps],
  );

  const [selectedAppId, setSelectedAppId] = useState('');
  const [name, setName] = useState('');
  const [presetKey, setPresetKey] = useState<PresetKey>('daily');
  const [customExpr, setCustomExpr] = useState('');
  const [timezone, setTimezone] = useState('Asia/Shanghai');
  const [formValue, setFormValue] = useState<SchemaFormValue>({ text: {}, multi: {}, file: {} });
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const selectedApp = useMemo(
    () => apps.find((a) => a.id === selectedAppId) ?? null,
    [apps, selectedAppId],
  );
  const publishedVersion = useMemo(
    () => (selectedApp ? resolvePublishedVersion(selectedApp) : null),
    [selectedApp],
  );
  const schema: InputField[] = publishedVersion?.input_schema ?? [];

  // Reset form values when app changes
  useEffect(() => {
    setFormValue(buildDefaultFormValue(schema));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedAppId]);

  const cronExpr = presetKey === 'custom' ? customExpr : (CRON_PRESETS.find((p) => p.key === presetKey)?.expr ?? '');

  const handleSubmit = async () => {
    const errs: Record<string, string> = {};
    if (!selectedAppId) errs.app = t('schedules.dialog.error.noApp');
    if (!name.trim()) errs.name = t('schedules.dialog.error.noName');
    if (!cronExpr.trim()) errs.cron = t('schedules.dialog.error.noCron');
    if (Object.keys(errs).length > 0) { setErrors(errs); return; }

    setSubmitting(true);
    setErrors({});
    try {
      const body: CronJobCreate = {
        app_id: selectedAppId,
        name: name.trim(),
        cron_expression: cronExpr.trim(),
        timezone,
        input_values: { text: formValue.text, multi: formValue.multi },
        enabled: true,
      };
      await api.schedules.create(body);
      onCreated();
    } catch (e) {
      setErrors({ submit: e instanceof ApiError ? e.message : '创建失败，请稍后重试' });
    } finally {
      setSubmitting(false);
    }
  };

  const appOptions = publishedApps.map((a) => ({ value: a.id, label: a.name }));

  return (
    <Dialog
      title={t('schedules.dialog.title.create')}
      onClose={onClose}
      dismissable={!submitting}
      className="w-full max-w-[520px]"
    >
      {/* App picker */}
      <SelectField
        label={t('schedules.dialog.field.app')}
        value={selectedAppId}
        onChange={(v) => { setSelectedAppId(v); setErrors((e) => ({ ...e, app: '' })); }}
        options={appOptions}
        placeholder={t('schedules.dialog.field.app.placeholder')}
        required
        error={errors.app}
      />

      {/* Name */}
      <TextField
        label={t('schedules.dialog.field.name')}
        value={name}
        onChange={(v) => { setName(v); setErrors((e) => ({ ...e, name: '' })); }}
        placeholder={t('schedules.dialog.field.name.placeholder')}
        required
        error={errors.name}
      />

      {/* Cron preset chips */}
      <div className="flex flex-col gap-1.5">
        <label className="text-[13px] font-medium text-ink">
          {t('schedules.dialog.field.cron')}
          <span className="text-accent ml-1">*</span>
        </label>
        <div className="flex flex-wrap gap-1.5">
          {CRON_PRESETS.map((p) => (
            <button
              key={p.key}
              type="button"
              onClick={() => { setPresetKey(p.key); setErrors((e) => ({ ...e, cron: '' })); }}
              className={[
                'px-2.5 py-1 rounded-sm border text-[12px] font-mono transition-colors',
                presetKey === p.key
                  ? 'bg-accent text-white border-accent'
                  : 'bg-surface border-line text-ink-2 hover:border-ink-2',
              ].join(' ')}
            >
              {t(p.labelKey)}
            </button>
          ))}
        </div>
        {presetKey !== 'custom' && (
          <div className="text-[11.5px] text-dim font-mono">
            {CRON_PRESETS.find((p) => p.key === presetKey)?.hint}
          </div>
        )}
        {presetKey === 'custom' && (
          <TextField
            value={customExpr}
            onChange={(v) => { setCustomExpr(v); setErrors((e) => ({ ...e, cron: '' })); }}
            placeholder={t('schedules.dialog.field.cron.custom')}
            error={errors.cron}
          />
        )}
        {presetKey !== 'custom' && errors.cron && (
          <div className="text-[11.5px] text-accent">{errors.cron}</div>
        )}
      </div>

      {/* Timezone */}
      <SelectField
        label={t('schedules.dialog.field.timezone')}
        value={timezone}
        onChange={setTimezone}
        options={TIMEZONE_OPTIONS}
      />

      {/* Input values (SchemaForm, no file upload) */}
      {schema.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <div className="text-[13px] font-medium text-ink">{t('schedules.dialog.field.inputs')}</div>
          <SchemaForm
            schema={schema}
            value={formValue}
            onChange={setFormValue}
            disabledFileUpload
          />
        </div>
      )}

      {errors.submit && (
        <div className="px-3 py-2 border border-red-300 bg-red-50 rounded-md text-[12.5px] text-red-700">
          {errors.submit}
        </div>
      )}

      <div className="flex items-center justify-end gap-2 pt-1">
        <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={submitting}>
          {t('common.cancel')}
        </button>
        <button
          className="cw-btn cw-btn-accent cw-btn-sm"
          onClick={handleSubmit}
          disabled={submitting}
        >
          {submitting ? t('schedules.dialog.submitting') : t('schedules.dialog.submit')}
        </button>
      </div>
    </Dialog>
  );
}

// ─── Main page ────────────────────────────────────────────────────────────────

export function SchedulesPage() {
  const { t, lang } = useTranslation();
  const { schedules, loading, error, refresh } = useSchedules();

  const [apps, setApps] = useState<App[]>([]);
  const [appsLoading, setAppsLoading] = useState(false);

  const [createOpen, setCreateOpen] = useState(false);

  // Delete confirm state
  const [deleteTarget, setDeleteTarget] = useState<CronJob | null>(null);
  const [deletebusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState('');

  // Toggle error toast (ephemeral)
  const [toggleError, setToggleError] = useState('');

  // App name lookup
  const appMap = useMemo(() => {
    const m: Record<string, string> = {};
    for (const a of apps) m[a.id] = a.name;
    return m;
  }, [apps]);

  const fetchApps = useCallback(() => {
    setAppsLoading(true);
    api.apps
      .list()
      .then((res) => setApps(res.items ?? []))
      .catch(() => {/* failure-open */})
      .finally(() => setAppsLoading(false));
  }, []);

  useEffect(() => {
    fetchApps();
  }, [fetchApps]);

  const handleToggle = async (job: CronJob) => {
    setToggleError('');
    try {
      await api.schedules.update(job.id, { enabled: !job.enabled });
      refresh();
    } catch {
      setToggleError(t('schedules.toggle.error'));
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

  const handleCreated = () => {
    setCreateOpen(false);
    refresh();
  };

  return (
    <div className="flex flex-col h-full">
      <PageHead
        crumbs={[{ label: t('schedules.title') }]}
        actions={
          <button
            className="cw-btn cw-btn-accent cw-btn-sm"
            onClick={() => { fetchApps(); setCreateOpen(true); }}
            disabled={appsLoading}
          >
            {t('schedules.createBtn')}
          </button>
        }
      />

      <div className="flex-1 overflow-auto px-6 py-4">
        {/* Error banner */}
        {error && (
          <div className="mb-4 px-4 py-2.5 border border-red-300 bg-red-50 rounded-md text-[13px] text-red-700">
            {error}
          </div>
        )}
        {toggleError && (
          <div className="mb-4 px-4 py-2.5 border border-yellow-300 bg-yellow-50 rounded-md text-[13px] text-yellow-700">
            {toggleError}
          </div>
        )}

        {loading && (
          <div className="text-[13px] text-dim py-6">{t('common.loading')}…</div>
        )}

        {!loading && schedules.length === 0 && !error && (
          <div className="text-[13px] text-dim py-6">{t('schedules.empty')}</div>
        )}

        {!loading && schedules.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-[13px]">
              <thead>
                <tr className="border-b border-line">
                  {[
                    'schedules.col.name',
                    'schedules.col.app',
                    'schedules.col.cron',
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
                    {/* Cron expr */}
                    <td className="px-3 py-2.5">
                      <span className="font-mono text-[11.5px] text-ink">{job.cron_expression}</span>
                    </td>
                    {/* Timezone */}
                    <td className="px-3 py-2.5 text-dim text-[12px] whitespace-nowrap">
                      {job.timezone}
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
                    {/* Actions */}
                    <td className="px-3 py-2.5">
                      <button
                        className="cw-btn cw-btn-sm text-[11px] text-red-600 hover:bg-red-50 border-transparent"
                        onClick={() => { setDeleteError(''); setDeleteTarget(job); }}
                      >
                        {t('common.delete')}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Create dialog */}
      {createOpen && (
        <CreateDialog
          apps={apps}
          onClose={() => setCreateOpen(false)}
          onCreated={handleCreated}
        />
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
