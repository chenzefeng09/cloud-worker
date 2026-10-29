import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { api, ApiError } from '../api/client';
import type { App, AppVersion, InputField } from '../api/types';
import { modelLabel, effortLabel } from '../lib/modelOptions';
import { useCanUploadAttachment } from '../hooks/useCanUploadAttachment';
import { SchemaForm } from '../components/form/SchemaForm';
import type { SchemaFormValue } from '../components/form/SchemaForm';

/** Resolve the published version of an App (the one current_version_id points at). */
function resolvePublishedVersion(app: App | null): AppVersion | null {
  if (!app) return null;
  const versions = app.versions ?? [];
  if (app.current_version_id) {
    const v = versions.find((x) => x.id === app.current_version_id);
    if (v) return v;
  }
  return versions.find((x) => x.status === 'published') ?? null;
}

function humanSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export function AppRunPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const { checkFile } = useCanUploadAttachment();
  const [app, setApp] = useState<App | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Per-field form state. text/textarea/select hold a string; multiselect holds
  // string[]; file holds Attachment[] (uploaded to the draft session).
  const [formValue, setFormValue] = useState<SchemaFormValue>({
    text: {},
    multi: {},
    file: {},
  });

  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [uploadingKey, setUploadingKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // One draft session shared by all file uploads + the final run call.
  const sessionRef = useRef<string | null>(null);

  const version = useMemo(() => resolvePublishedVersion(app), [app]);
  const schema: InputField[] = version?.input_schema ?? [];

  useEffect(() => {
    let alive = true;
    if (!id) {
      setLoadError('No app specified.');
      setLoading(false);
      return;
    }
    setLoading(true);
    api.apps
      .get(id)
      .then((a) => {
        if (!alive) return;
        setApp(a);
        // Pre-fill defaults from the resolved version's schema.
        const v = resolvePublishedVersion(a);
        if (v) {
          const text: Record<string, string> = {};
          const multi: Record<string, string[]> = {};
          for (const f of v.input_schema ?? []) {
            if (f.type === 'multiselect') {
              multi[f.key] = Array.isArray(f.default) ? f.default : [];
            } else if (f.type !== 'file') {
              text[f.key] = typeof f.default === 'string' ? f.default : '';
            }
          }
          setFormValue({ text, multi, file: {} });
        }
      })
      .catch((e) => {
        if (alive) setLoadError((e as Error).message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  const ensureSession = async (): Promise<string | null> => {
    if (sessionRef.current) return sessionRef.current;
    if (!id || !app) return null;
    const session = await api.sessions.create(app.name, { kind: 'app_run', app_id: id });
    sessionRef.current = session.id;
    return session.id;
  };

  const handleFilePick = async (field: InputField, files: File[]) => {
    if (!files.length) return;
    const constraint = field.file;
    setFieldErrors((prev) => ({ ...prev, [field.key]: '' }));

    // Client-side type + size validation before any upload.
    for (const file of files) {
      if (constraint?.accept?.length) {
        const ok = constraint.accept.some((a) => {
          if (a.endsWith('/*')) return file.type.startsWith(a.slice(0, -1));
          if (a.startsWith('.')) return file.name.toLowerCase().endsWith(a.toLowerCase());
          return file.type === a;
        });
        if (!ok) {
          setFieldErrors((prev) => ({ ...prev, [field.key]: `Unsupported file type: ${file.name}` }));
          return;
        }
      }
      if (constraint?.max_size_bytes && file.size > constraint.max_size_bytes) {
        setFieldErrors((prev) => ({
          ...prev,
          [field.key]: `File exceeds the size limit (${humanSize(constraint.max_size_bytes)}): ${file.name}`,
        }));
        return;
      }
    }

    setUploadingKey(field.key);
    try {
      const sid = await ensureSession();
      if (!sid) {
        setFieldErrors((prev) => ({ ...prev, [field.key]: 'Couldn’t start the upload. Please try again.' }));
        return;
      }
      const single = !constraint?.multiple;
      const toUpload = single ? files.slice(0, 1) : files;
      for (const file of toUpload) {
        // Pre-upload gate: large attachments require the large-attachment
        // entitlement; check before the upload network call.
        const gate = await checkFile(file);
        if (!gate.allowed) {
          setFieldErrors((prev) => ({
            ...prev,
            [field.key]: 'This file is large and needs the large-attachment permission to upload. Please contact your administrator.',
          }));
          return;
        }
        const att = await api.attachments.upload(sid, file);
        setFormValue((prev) => {
          const existing = single ? [] : prev.file[field.key] ?? [];
          return {
            ...prev,
            file: { ...prev.file, [field.key]: [...existing, att] },
          };
        });
      }
    } catch (err) {
      setFieldErrors((prev) => ({ ...prev, [field.key]: 'Attachment upload failed. Please try again.' }));
      console.warn('app input upload failed', err);
    } finally {
      setUploadingKey(null);
    }
  };

  const handleFileRemove = (_field: InputField, attId: string) => {
    setFormValue((prev) => ({
      ...prev,
      file: {
        ...prev.file,
        [_field.key]: (prev.file[_field.key] ?? []).filter((a) => a.id !== attId),
      },
    }));
  };

  const validate = (): boolean => {
    const errs: Record<string, string> = {};
    for (const f of schema) {
      if (f.type === 'file') {
        const files = formValue.file[f.key] ?? [];
        const hasDefault = typeof f.default === 'string' && f.default.trim() !== '';
        if (f.required && files.length === 0 && !hasDefault) errs[f.key] = 'This field is required.';
      } else if (f.type === 'multiselect') {
        const vals = formValue.multi[f.key] ?? [];
        const allowed = new Set((f.options ?? []).map((o) => o.value));
        if (f.required && vals.length === 0) errs[f.key] = 'This field is required.';
        else if (vals.some((v) => !allowed.has(v))) errs[f.key] = 'Contains an invalid option.';
      } else {
        const val = (formValue.text[f.key] ?? '').trim();
        if (f.required && !val) errs[f.key] = 'This field is required.';
        else if (f.type === 'select' && val) {
          const allowed = new Set((f.options ?? []).map((o) => o.value));
          if (!allowed.has(val)) errs[f.key] = 'Contains an invalid option.';
        }
      }
    }
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const handleSubmit = async () => {
    if (!id) return;
    setSubmitError(null);
    if (!validate()) return;
    setSubmitting(true);
    try {
      const inputs: Record<string, unknown> = {};
      const fileInputs: Record<string, string[]> = {};
      for (const f of schema) {
        if (f.type === 'file') {
          const files = formValue.file[f.key] ?? [];
          if (files.length) fileInputs[f.key] = files.map((a) => a.id);
        } else if (f.type === 'multiselect') {
          inputs[f.key] = formValue.multi[f.key] ?? [];
        } else {
          inputs[f.key] = formValue.text[f.key] ?? '';
        }
      }
      const res = await api.apps.run(id, {
        inputs,
        file_inputs: fileInputs,
        session_id: sessionRef.current ?? undefined,
      });
      navigate(`/apps/runs/${res.session_id}/${res.run_id}`);
    } catch (err) {
      if (err instanceof ApiError) {
        if (err.status === 422) {
          // BFF returns detail.error==='invalid_inputs' with per-field errors.
          try {
            const parsed = JSON.parse(err.message) as {
              error?: string;
              errors?: Record<string, string> | string[];
            };
            if (parsed.errors && !Array.isArray(parsed.errors)) {
              setFieldErrors(parsed.errors);
              setSubmitError('Input validation failed. Please check the form.');
              return;
            }
            if (Array.isArray(parsed.errors)) {
              setSubmitError(`Input validation failed: ${parsed.errors.join('; ')}`);
              return;
            }
          } catch {
            /* fall through to generic message */
          }
          setSubmitError('Input validation failed. Please check the form.');
          return;
        }
        if (err.status === 409) {
          setSubmitError('This app can’t be run right now (not published).');
          return;
        }
        setSubmitError(err.message || 'Run failed. Please try again later.');
        return;
      }
      setSubmitError('Run failed. Please try again later.');
      console.warn('app run failed', err);
    } finally {
      setSubmitting(false);
    }
  };

  // ─── Render ────────────────────────────────────────────────────────────────

  const crumbs = [
    { label: 'Home', to: '/home' },
    { label: 'Apps' },
    { label: app?.name || 'App' },
  ];

  if (loading) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex items-center justify-center font-mono text-[12px] text-dim">
          Loading…
        </div>
      </div>
    );
  }

  if (loadError || !app) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex items-center justify-center text-dim text-[13px]">
          {loadError || 'App not found.'}
        </div>
      </div>
    );
  }

  if (!version) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex flex-col items-center justify-center gap-2 text-dim">
          <div className="text-[14px] text-ink font-medium">This app hasn’t been published yet</div>
          <div className="text-[12.5px]">There’s no published version to run.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead crumbs={crumbs} />

      <div className="cw-scroll flex-1 min-h-0 px-[5vw] py-8 flex flex-col items-center">
        <div className="w-[min(720px,95%)] flex flex-col gap-6">
          {/* App header */}
          <div className="flex items-start gap-3">
            <div className="w-10 h-10 border border-line rounded-md flex items-center justify-center font-mono text-[18px] text-ink bg-surface shrink-0">
              {app.icon || '▢'}
            </div>
            <div className="min-w-0 flex-1">
              <h1 className="m-0 font-display font-normal tracking-[-0.01em] text-[22px] leading-[1.2]">
                {app.name}
              </h1>
              {app.description && (
                <p className="mt-1 mb-0 text-[13px] text-dim leading-[1.5]">{app.description}</p>
              )}
            </div>
          </div>

          {/* Read-only harness chips (single-turn — no selectors) */}
          <div className="flex flex-wrap items-center gap-1.5">
            <Chip>Model · {modelLabel(version.model)}</Chip>
            {version.reasoning_effort != null && version.reasoning_effort !== '' && (
              <Chip>Reasoning · {effortLabel(version.reasoning_effort)}</Chip>
            )}
            <Chip>Version v{version.version}</Chip>
            {version.forced_skills.map((s) => (
              <Chip key={s} dashed>
                {s}
              </Chip>
            ))}
          </div>

          <div className="h-px bg-line" />

          {/* Dynamic form — delegated to SchemaForm */}
          <SchemaForm
            schema={schema}
            value={formValue}
            onChange={setFormValue}
            errors={fieldErrors}
            onFilePick={handleFilePick}
            onFileRemove={handleFileRemove}
            uploadingKey={uploadingKey}
          />

          {submitError && (
            <div className="px-3 py-2 border border-accent-line bg-accent-soft rounded-md text-[12.5px] text-accent">
              {submitError}
            </div>
          )}

          {/* Submit */}
          <div className="flex items-center gap-3 pt-1">
            <button
              onClick={handleSubmit}
              disabled={submitting || uploadingKey !== null}
              className="cw-btn cw-btn-accent disabled:opacity-50"
            >
              {submitting ? 'Running…' : 'Run'}
            </button>
            <span className="font-mono text-[10.5px] text-dim">Single-turn run · view progress and output after it starts</span>
          </div>
        </div>
      </div>
    </div>
  );
}
