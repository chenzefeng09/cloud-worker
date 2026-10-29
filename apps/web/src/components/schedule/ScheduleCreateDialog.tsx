/**
 * ScheduleCreateDialog — 「设置触发器」for one fixed App.
 *
 * Lives on the App detail page (gear menu), so the App is a prop rather than a
 * picker. A type switch at the top chooses between two trigger kinds:
 *
 *   定时 (schedule) — cron preset/custom + timezone + SchemaForm input_values,
 *                     POSTed with trigger_type:'schedule' (the original flow).
 *   Webhook         — a payload field-mapping (incoming key → App field key),
 *                     POSTed with trigger_type:'webhook'. On success the dialog
 *                     flips to a one-time success view revealing the webhook URL,
 *                     the plaintext secret and a copy-pastable curl sample.
 *
 * 收口: an App whose published input_schema has ANY required file field cannot be
 * driven by a schedule (schedules can't upload files), so the 定时 tab is disabled
 * and the switch defaults to Webhook.
 */
import { useState } from 'react';
import { Dialog } from '../../admin/components/Dialog';
import { TextField } from '../form';
import { SelectField } from '../form';
import { SchemaForm } from '../form';
import type { SchemaFormValue } from '../form';
import { Chip } from '../shell/Chip';
import { useCopied, CopyIcon } from '../CopyButton';
import { api, ApiError } from '../../api/client';
import type {
  App,
  AppVersion,
  CronJobCreate,
  InputField,
  InputFieldOption,
  InputFieldType,
  TriggerType,
} from '../../api/types';
import { useTranslation } from '../../i18n';

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

export const TIMEZONE_OPTIONS = [
  { value: 'UTC',                 label: 'UTC' },
  { value: 'America/Los_Angeles', label: 'America/Los_Angeles (PT)' },
  { value: 'America/New_York',    label: 'America/New_York (ET)' },
  { value: 'Europe/London',       label: 'Europe/London (GMT/BST)' },
  { value: 'Europe/Berlin',       label: 'Europe/Berlin (CET/CEST)' },
  { value: 'Asia/Singapore',      label: 'Asia/Singapore (SGT)' },
  { value: 'Asia/Shanghai',       label: 'Asia/Shanghai (CST)' },
  { value: 'Asia/Tokyo',          label: 'Asia/Tokyo (JST)' },
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

/** Coarse field-type → chip label key (文本 / 选择 / 文件). */
function fieldTypeLabelKey(type: InputFieldType): string {
  if (type === 'file') return 'schedules.fieldType.file';
  if (type === 'select' || type === 'multiselect') return 'schedules.fieldType.select';
  return 'schedules.fieldType.text';
}

/**
 * One per-field row in the webhook tab. Each field is EITHER:
 *  - 'event' (from-event): its value arrives in the payload under `incoming`
 *    (contributes to field_mapping). File fields are always here (can't be fixed).
 *  - 'fixed': a value chosen here at config time (contributes to input_values).
 *    text/textarea/select use `fixedText`; multiselect uses `fixedMulti`.
 */
type RowMode = 'fixed' | 'event';

interface MappingRow {
  appKey: string;
  label: string;
  type: InputFieldType;
  required: boolean;
  options?: InputFieldOption[];
  mode: RowMode;
  incoming: string;
  fixedText: string;
  fixedMulti: string[];
}

/**
 * Build a copy-pastable multipart curl sample from ONLY the from-event rows —
 * fixed fields are set at config time and never sent in the payload.
 */
function buildCurl(url: string, secret: string, rows: MappingRow[]): string {
  const parts = rows
    .filter((r) => r.mode === 'event' && r.incoming.trim())
    .map((r) => {
      const key = r.incoming.trim();
      return r.type === 'file'
        ? `  -F '${key}=@/path/to/file'`
        : `  -F '${key}=value'`;
    });
  return [
    `curl -X POST '${url}'`,
    `  -H 'Authorization: Bearer ${secret}'`,
    ...parts,
  ].join(' \\\n');
}

// ─── Copy affordance ────────────────────────────────────────────────────────

function CopyBtn({ text, ariaLabel }: { text: string; ariaLabel: string }) {
  const { t } = useTranslation();
  const { copied, copy } = useCopied();
  return (
    <button
      type="button"
      onClick={() => copy(text)}
      aria-label={ariaLabel}
      className="cw-btn cw-btn-sm shrink-0 inline-flex items-center gap-1"
    >
      <CopyIcon />
      {copied ? t('schedules.copied') : t('schedules.copy')}
    </button>
  );
}

/** Label + monospace value + copy button, value wraps rather than overflows. */
function SecretRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-1">
      <label className="cw-label">{label}</label>
      <div className="flex items-start gap-2">
        <code className="flex-1 min-w-0 break-all px-2.5 py-1.5 border border-line rounded-sm bg-paper text-[12px] font-mono text-ink">
          {value}
        </code>
        <CopyBtn text={value} ariaLabel={label} />
      </div>
    </div>
  );
}

// ─── Success view (webhook only, shown once) ─────────────────────────────────

function WebhookSuccess({
  url,
  secret,
  curl,
  onDone,
}: {
  url: string;
  secret: string;
  curl: string;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div className="text-[13px] font-medium text-ink">{t('schedules.webhook.success.title')}</div>

      {/* Secret shown only once — loud warning. */}
      <div className="px-3 py-2 border border-yellow-300 bg-yellow-50 rounded-md text-[12.5px] text-yellow-800 leading-[1.5]">
        {t('schedules.webhook.success.warn')}
      </div>

      <SecretRow label={t('schedules.webhook.url')} value={url} />
      <SecretRow label={t('schedules.webhook.secret')} value={secret} />

      {/* Sample request */}
      <div className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <label className="cw-label">{t('schedules.webhook.curl')}</label>
          <CopyBtn text={curl} ariaLabel={t('schedules.webhook.curl')} />
        </div>
        <p className="text-[11.5px] text-dim leading-[1.45]">{t('schedules.webhook.curlHint')}</p>
        <pre className="overflow-x-auto px-2.5 py-2 border border-line rounded-sm bg-paper text-[11.5px] font-mono text-ink leading-[1.55] whitespace-pre">
          {curl}
        </pre>
      </div>

      <div className="flex items-center justify-end gap-2 pt-1">
        <button className="cw-btn cw-btn-accent cw-btn-sm" onClick={onDone}>
          {t('schedules.webhook.done')}
        </button>
      </div>
    </>
  );
}

// ─── Dialog ───────────────────────────────────────────────────────────────────

interface Props {
  /** The App this trigger runs — fixed, hence no picker. */
  app: App;
  onClose: () => void;
  onCreated: () => void;
}

export function ScheduleCreateDialog({ app, onClose, onCreated }: Props) {
  const { t } = useTranslation();

  const publishedVersion = resolvePublishedVersion(app);
  const schema: InputField[] = publishedVersion?.input_schema ?? [];

  // 收口: a schedule can't upload files, so an App that requires one can only be
  // driven by a webhook. Disable the 定时 tab and default the switch to Webhook.
  const requiresFile = schema.some((f) => f.type === 'file' && f.required);

  const [triggerType, setTriggerType] = useState<TriggerType>(requiresFile ? 'webhook' : 'schedule');

  const [name, setName] = useState('');
  const [presetKey, setPresetKey] = useState<PresetKey>('daily');
  const [customExpr, setCustomExpr] = useState('');
  const [timezone, setTimezone] = useState('UTC');
  // The App never changes here, so the defaults can be seeded once on mount.
  const [formValue, setFormValue] = useState<SchemaFormValue>(() => buildDefaultFormValue(schema));
  // Webhook per-field rows. Default mode is 'event' (identity mapping) so the
  // out-of-the-box behavior is unchanged / a superset; file fields are pinned to
  // 'event' (files can't be fixed). Fixed values seed from the field default.
  const [mappings, setMappings] = useState<MappingRow[]>(() =>
    schema.map((f) => ({
      appKey: f.key,
      label: f.label,
      type: f.type,
      required: f.required,
      options: f.options,
      mode: 'event',
      incoming: f.key,
      fixedText: typeof f.default === 'string' ? f.default : '',
      fixedMulti: Array.isArray(f.default) ? (f.default as string[]) : [],
    })),
  );

  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  // Set once a webhook is created — flips the body to the one-time success view.
  const [created, setCreated] = useState<{ url: string; secret: string } | null>(null);

  const cronExpr = presetKey === 'custom' ? customExpr : (CRON_PRESETS.find((p) => p.key === presetKey)?.expr ?? '');

  const updateIncoming = (appKey: string, value: string) => {
    setMappings((prev) => prev.map((m) => (m.appKey === appKey ? { ...m, incoming: value } : m)));
  };
  const updateMode = (appKey: string, mode: RowMode) => {
    setMappings((prev) => prev.map((m) => (m.appKey === appKey ? { ...m, mode } : m)));
  };
  const updateFixedText = (appKey: string, value: string) => {
    setMappings((prev) => prev.map((m) => (m.appKey === appKey ? { ...m, fixedText: value } : m)));
  };
  const toggleFixedMulti = (appKey: string, option: string) => {
    setMappings((prev) =>
      prev.map((m) => {
        if (m.appKey !== appKey) return m;
        const on = m.fixedMulti.includes(option);
        return {
          ...m,
          fixedMulti: on ? m.fixedMulti.filter((v) => v !== option) : [...m.fixedMulti, option],
        };
      }),
    );
  };

  const handleSubmit = async () => {
    const errs: Record<string, string> = {};
    if (!name.trim()) errs.name = t('schedules.dialog.error.noName');
    if (triggerType === 'schedule' && !cronExpr.trim()) errs.cron = t('schedules.dialog.error.noCron');
    if (Object.keys(errs).length > 0) { setErrors(errs); return; }

    setSubmitting(true);
    setErrors({});
    try {
      if (triggerType === 'webhook') {
        // Each field is either from-event (→ field_mapping) or fixed (→ input_values
        // buckets, matching the SchemaFormValue shape schedules use).
        const field_mapping: Record<string, string> = {};
        const text: Record<string, string> = {};
        const multi: Record<string, string[]> = {};
        for (const m of mappings) {
          if (m.mode === 'fixed' && m.type !== 'file') {
            if (m.type === 'multiselect') multi[m.appKey] = m.fixedMulti;
            else text[m.appKey] = m.fixedText;
          } else {
            // from-event (files always here): incoming payload key → App field key.
            const key = m.incoming.trim();
            if (key) field_mapping[key] = m.appKey;
          }
        }
        const body: CronJobCreate = {
          app_id: app.id,
          name: name.trim(),
          trigger_type: 'webhook',
          field_mapping,
          input_values: { text, multi },
        };
        const resp = await api.schedules.create(body);
        const url = window.location.origin + (resp.webhook_path ?? '');
        setCreated({ url, secret: resp.webhook_secret ?? '' });
        return; // stay open on the success view; onCreated fires from 完成
      }

      const body: CronJobCreate = {
        app_id: app.id,
        name: name.trim(),
        trigger_type: 'schedule',
        cron_expression: cronExpr.trim(),
        timezone,
        input_values: { text: formValue.text, multi: formValue.multi },
        enabled: true,
      };
      await api.schedules.create(body);
      onCreated();
    } catch (e) {
      setErrors({ submit: e instanceof ApiError ? e.message : t('schedules.error.createFailed') });
    } finally {
      setSubmitting(false);
    }
  };

  const curl = created ? buildCurl(created.url, created.secret, mappings) : '';

  return (
    <Dialog
      title={t('schedules.dialog.title.create')}
      onClose={onClose}
      dismissable={!submitting}
      className="w-full max-w-[560px]"
    >
      {created ? (
        <WebhookSuccess url={created.url} secret={created.secret} curl={curl} onDone={onCreated} />
      ) : (
        <>
          {/* Fixed app — read-only, the entry point already picked it */}
          <div className="flex flex-col gap-1.5">
            <label className="text-[13px] font-medium text-ink">{t('schedules.dialog.field.app')}</label>
            <div className="px-2.5 py-1.5 border border-line rounded-sm bg-paper text-[13px] text-ink-2 truncate">
              {app.name}
            </div>
          </div>

          {/* Trigger type switch */}
          <div className="flex flex-col gap-1.5">
            <label className="text-[13px] font-medium text-ink">{t('schedules.type.label')}</label>
            <div className="inline-flex rounded-sm border border-line overflow-hidden w-max">
              <button
                type="button"
                role="tab"
                aria-selected={triggerType === 'schedule'}
                disabled={requiresFile}
                title={requiresFile ? t('schedules.dialog.scheduleDisabledHint') : undefined}
                onClick={() => setTriggerType('schedule')}
                className={[
                  'px-3 py-1 text-[12px] transition-colors',
                  triggerType === 'schedule' ? 'bg-accent text-white' : 'bg-surface text-ink-2 hover:text-ink',
                  requiresFile ? 'opacity-40 cursor-not-allowed' : '',
                ].join(' ')}
              >
                {t('schedules.type.schedule')}
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={triggerType === 'webhook'}
                onClick={() => setTriggerType('webhook')}
                className={[
                  'px-3 py-1 text-[12px] border-l border-line transition-colors',
                  triggerType === 'webhook' ? 'bg-accent text-white' : 'bg-surface text-ink-2 hover:text-ink',
                ].join(' ')}
              >
                {t('schedules.type.webhook')}
              </button>
            </div>
            {requiresFile ? (
              <div className="text-[11.5px] text-dim">{t('schedules.dialog.scheduleDisabledHint')}</div>
            ) : (
              <div className="text-[11.5px] text-dim">
                {t(triggerType === 'webhook' ? 'schedules.type.webhook.hint' : 'schedules.type.schedule.hint')}
              </div>
            )}
          </div>

          {/* Name — shared by both trigger kinds */}
          <TextField
            label={t('schedules.dialog.field.name')}
            value={name}
            onChange={(v) => { setName(v); setErrors((e) => ({ ...e, name: '' })); }}
            placeholder={t('schedules.dialog.field.name.placeholder')}
            required
            error={errors.name}
          />

          {triggerType === 'schedule' ? (
            <>
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
            </>
          ) : (
            /* Webhook: payload field mapping (incoming key → App field). */
            <div className="flex flex-col gap-1.5">
              <label className="text-[13px] font-medium text-ink">
                {t('schedules.dialog.webhook.mappingTitle')}
              </label>
              <p className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">
                {t('schedules.dialog.webhook.mappingHint')}
              </p>
              {mappings.length === 0 ? (
                <div className="font-mono italic text-dim text-[12px]">
                  {t('schedules.dialog.webhook.mappingEmpty')}
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {mappings.map((m) => {
                    const isFile = m.type === 'file';
                    return (
                      <div
                        key={m.appKey}
                        className="flex flex-col gap-2 border border-line rounded-sm bg-paper px-2.5 py-2"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <div className="flex items-center gap-1.5 min-w-[120px] flex-1">
                            <span className="text-[13px] text-ink truncate">{m.label}</span>
                            <span className="cw-chip text-[9px] text-dim shrink-0">{t(fieldTypeLabelKey(m.type))}</span>
                            {m.required && <span className="text-accent text-[11px] shrink-0">*</span>}
                          </div>
                          {/* fixed | from-event toggle (files are pinned to from-event) */}
                          <div className="inline-flex rounded-sm border border-line overflow-hidden shrink-0">
                            <button
                              type="button"
                              aria-pressed={m.mode === 'fixed'}
                              disabled={isFile}
                              title={isFile ? t('schedules.webhook.mode.fileHint') : undefined}
                              onClick={() => updateMode(m.appKey, 'fixed')}
                              className={[
                                'px-2 py-0.5 text-[11px] transition-colors',
                                m.mode === 'fixed' ? 'bg-accent text-white' : 'bg-surface text-ink-2 hover:text-ink',
                                isFile ? 'opacity-40 cursor-not-allowed' : '',
                              ].join(' ')}
                            >
                              {t('schedules.webhook.mode.fixed')}
                            </button>
                            <button
                              type="button"
                              aria-pressed={m.mode === 'event'}
                              onClick={() => updateMode(m.appKey, 'event')}
                              className={[
                                'px-2 py-0.5 text-[11px] border-l border-line transition-colors',
                                m.mode === 'event' ? 'bg-accent text-white' : 'bg-surface text-ink-2 hover:text-ink',
                              ].join(' ')}
                            >
                              {t('schedules.webhook.mode.event')}
                            </button>
                          </div>
                        </div>
                        {/* from-event → incoming payload key; fixed → value by field type */}
                        {m.mode === 'event' ? (
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-[11px] text-dim shrink-0">←</span>
                            <div className="flex-1 min-w-[160px]">
                              <TextField
                                size="sm"
                                value={m.incoming}
                                onChange={(v) => updateIncoming(m.appKey, v)}
                                placeholder={t('schedules.dialog.webhook.incomingKey')}
                              />
                            </div>
                          </div>
                        ) : m.type === 'select' ? (
                          <SelectField
                            size="sm"
                            value={m.fixedText}
                            onChange={(v) => updateFixedText(m.appKey, v)}
                            options={(m.options ?? []).map((o) => ({ value: o.value, label: o.label }))}
                            title={m.label}
                            placeholder={t('schedules.dialog.webhook.fixedValue')}
                          />
                        ) : m.type === 'multiselect' ? (
                          <div className="flex flex-wrap gap-1.5">
                            {(m.options ?? []).map((o) => {
                              const on = m.fixedMulti.includes(o.value);
                              return (
                                <Chip
                                  key={o.value}
                                  accent={on}
                                  dashed={!on}
                                  onClick={() => toggleFixedMulti(m.appKey, o.value)}
                                >
                                  {o.label}
                                </Chip>
                              );
                            })}
                          </div>
                        ) : (
                          <TextField
                            size="sm"
                            value={m.fixedText}
                            onChange={(v) => updateFixedText(m.appKey, v)}
                            placeholder={t('schedules.dialog.webhook.fixedValue')}
                          />
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
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
        </>
      )}
    </Dialog>
  );
}
