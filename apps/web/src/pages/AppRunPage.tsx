import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
import { api, ApiError } from '../api/client';
import type { App, AppVersion, Attachment, InputField } from '../api/types';
import { modelLabel, effortLabel } from '../lib/modelOptions';

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

const isImage = (mime: string) => mime.startsWith('image/');

export function AppRunPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [app, setApp] = useState<App | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Per-field form state. text/textarea/select hold a string; multiselect holds
  // string[]; file holds Attachment[] (uploaded to the draft session).
  const [textValues, setTextValues] = useState<Record<string, string>>({});
  const [multiValues, setMultiValues] = useState<Record<string, string[]>>({});
  const [fileValues, setFileValues] = useState<Record<string, Attachment[]>>({});

  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [uploadingKey, setUploadingKey] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // One draft session shared by all file uploads + the final run call.
  const sessionRef = useRef<string | null>(null);
  // One hidden <input type=file> per field key.
  const fileInputsRef = useRef<Record<string, HTMLInputElement | null>>({});

  const version = useMemo(() => resolvePublishedVersion(app), [app]);
  const schema: InputField[] = version?.input_schema ?? [];

  useEffect(() => {
    let alive = true;
    if (!id) {
      setLoadError('未指定应用。');
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
          const tv: Record<string, string> = {};
          const mv: Record<string, string[]> = {};
          for (const f of v.input_schema ?? []) {
            if (f.type === 'multiselect') {
              mv[f.key] = Array.isArray(f.default) ? f.default : [];
            } else if (f.type !== 'file') {
              tv[f.key] = typeof f.default === 'string' ? f.default : '';
            }
          }
          setTextValues(tv);
          setMultiValues(mv);
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
          setFieldErrors((prev) => ({ ...prev, [field.key]: `文件类型不支持：${file.name}` }));
          return;
        }
      }
      if (constraint?.max_size_bytes && file.size > constraint.max_size_bytes) {
        setFieldErrors((prev) => ({
          ...prev,
          [field.key]: `文件超过大小上限（${humanSize(constraint.max_size_bytes)}）：${file.name}`,
        }));
        return;
      }
    }

    setUploadingKey(field.key);
    try {
      const sid = await ensureSession();
      if (!sid) {
        setFieldErrors((prev) => ({ ...prev, [field.key]: '创建上传会话失败，请重试。' }));
        return;
      }
      const single = !constraint?.multiple;
      const toUpload = single ? files.slice(0, 1) : files;
      for (const file of toUpload) {
        const att = await api.attachments.upload(sid, file);
        setFileValues((prev) => {
          const existing = single ? [] : prev[field.key] ?? [];
          return { ...prev, [field.key]: [...existing, att] };
        });
      }
    } catch (err) {
      setFieldErrors((prev) => ({ ...prev, [field.key]: '附件上传失败，请重试。' }));
      console.warn('app input upload failed', err);
    } finally {
      setUploadingKey(null);
      const el = fileInputsRef.current[field.key];
      if (el) el.value = '';
    }
  };

  const removeFile = (key: string, attId: string) => {
    setFileValues((prev) => ({
      ...prev,
      [key]: (prev[key] ?? []).filter((a) => a.id !== attId),
    }));
  };

  const toggleMulti = (key: string, value: string) => {
    setMultiValues((prev) => {
      const cur = prev[key] ?? [];
      return {
        ...prev,
        [key]: cur.includes(value) ? cur.filter((v) => v !== value) : [...cur, value],
      };
    });
  };

  const validate = (): boolean => {
    const errs: Record<string, string> = {};
    for (const f of schema) {
      if (f.type === 'file') {
        const files = fileValues[f.key] ?? [];
        const hasDefault = typeof f.default === 'string' && f.default.trim() !== '';
        if (f.required && files.length === 0 && !hasDefault) errs[f.key] = '此项为必填。';
      } else if (f.type === 'multiselect') {
        const vals = multiValues[f.key] ?? [];
        const allowed = new Set((f.options ?? []).map((o) => o.value));
        if (f.required && vals.length === 0) errs[f.key] = '此项为必填。';
        else if (vals.some((v) => !allowed.has(v))) errs[f.key] = '存在无效的选项。';
      } else {
        const val = (textValues[f.key] ?? '').trim();
        if (f.required && !val) errs[f.key] = '此项为必填。';
        else if (f.type === 'select' && val) {
          const allowed = new Set((f.options ?? []).map((o) => o.value));
          if (!allowed.has(val)) errs[f.key] = '存在无效的选项。';
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
          const files = fileValues[f.key] ?? [];
          if (files.length) fileInputs[f.key] = files.map((a) => a.id);
        } else if (f.type === 'multiselect') {
          inputs[f.key] = multiValues[f.key] ?? [];
        } else {
          inputs[f.key] = textValues[f.key] ?? '';
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
              setSubmitError('输入校验未通过，请检查表单。');
              return;
            }
            if (Array.isArray(parsed.errors)) {
              setSubmitError(`输入校验未通过：${parsed.errors.join('；')}`);
              return;
            }
          } catch {
            /* fall through to generic message */
          }
          setSubmitError('输入校验未通过，请检查表单。');
          return;
        }
        if (err.status === 409) {
          setSubmitError('该应用当前不可运行（未发布）。');
          return;
        }
        setSubmitError(err.message || '执行失败，请稍后重试。');
        return;
      }
      setSubmitError('执行失败，请稍后重试。');
      console.warn('app run failed', err);
    } finally {
      setSubmitting(false);
    }
  };

  // ─── Render ────────────────────────────────────────────────────────────────

  const crumbs = [
    { label: '首页', to: '/home' },
    { label: '应用' },
    { label: app?.name || '应用' },
  ];

  if (loading) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex items-center justify-center font-mono text-[12px] text-dim">
          加载中…
        </div>
      </div>
    );
  }

  if (loadError || !app) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex items-center justify-center text-dim text-[13px]">
          {loadError || '未找到应用。'}
        </div>
      </div>
    );
  }

  if (!version) {
    return (
      <div className="flex flex-col h-full min-w-0 bg-paper">
        <PageHead crumbs={crumbs} />
        <div className="flex-1 flex flex-col items-center justify-center gap-2 text-dim">
          <div className="text-[14px] text-ink font-medium">该应用尚未发布</div>
          <div className="text-[12.5px]">暂无可运行的已发布版本。</div>
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
            <Chip>模型 · {modelLabel(version.model)}</Chip>
            {version.reasoning_effort != null && version.reasoning_effort !== '' && (
              <Chip>推理 · {effortLabel(version.reasoning_effort)}</Chip>
            )}
            <Chip>版本 v{version.version}</Chip>
            {version.forced_skills.map((s) => (
              <Chip key={s} dashed>
                {s}
              </Chip>
            ))}
          </div>

          <div className="h-px bg-line" />

          {/* Dynamic form */}
          <div className="flex flex-col gap-5">
            {schema.length === 0 && (
              <div className="font-mono italic text-dim text-[12px]">该应用无需输入，直接执行即可。</div>
            )}
            {schema.map((field) => {
              const err = fieldErrors[field.key];
              return (
                <div key={field.key} className="flex flex-col gap-1.5">
                  <label className="text-[13px] font-medium text-ink">
                    {field.label}
                    {field.required && <span className="text-accent ml-1">*</span>}
                  </label>
                  {field.help && (
                    <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{field.help}</div>
                  )}

                  {field.type === 'text' && (
                    <input
                      type="text"
                      value={textValues[field.key] ?? ''}
                      onChange={(e) =>
                        setTextValues((p) => ({ ...p, [field.key]: e.target.value }))
                      }
                      className="w-full bg-surface border border-line rounded-md px-3 py-2 text-[14px] text-ink outline-none focus:border-ink-2"
                    />
                  )}

                  {field.type === 'textarea' && (
                    <textarea
                      value={textValues[field.key] ?? ''}
                      onChange={(e) =>
                        setTextValues((p) => ({ ...p, [field.key]: e.target.value }))
                      }
                      className="w-full min-h-[110px] resize-y bg-surface border border-line rounded-md px-3 py-2 text-[14px] leading-[1.5] text-ink outline-none focus:border-ink-2"
                    />
                  )}

                  {field.type === 'select' && (
                    <div className="flex items-center gap-2 bg-surface border border-line rounded-md px-3 py-2">
                      <span className="text-[13px] text-ink flex-1 truncate">
                        {(field.options ?? []).find((o) => o.value === textValues[field.key])
                          ?.label || <span className="text-dim">请选择…</span>}
                      </span>
                      <Dropdown
                        value={textValues[field.key] ?? ''}
                        options={(field.options ?? []).map((o) => ({ id: o.value, label: o.label }))}
                        onChange={(v) => setTextValues((p) => ({ ...p, [field.key]: v }))}
                        align="left"
                        title={field.label}
                      />
                    </div>
                  )}

                  {field.type === 'multiselect' && (
                    <div className="flex flex-wrap gap-1.5">
                      {(field.options ?? []).map((o) => {
                        const on = (multiValues[field.key] ?? []).includes(o.value);
                        return (
                          <Chip
                            key={o.value}
                            accent={on}
                            dashed={!on}
                            onClick={() => toggleMulti(field.key, o.value)}
                          >
                            {o.label}
                          </Chip>
                        );
                      })}
                    </div>
                  )}

                  {field.type === 'file' && (
                    <div className="flex flex-col gap-1.5">
                      <input
                        ref={(el) => {
                          fileInputsRef.current[field.key] = el;
                        }}
                        type="file"
                        multiple={!!field.file?.multiple}
                        accept={(field.file?.accept ?? []).join(',')}
                        className="hidden"
                        onChange={(e) =>
                          handleFilePick(field, Array.from(e.target.files || []))
                        }
                        disabled={uploadingKey === field.key}
                      />
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => fileInputsRef.current[field.key]?.click()}
                          disabled={uploadingKey === field.key}
                          className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-50"
                        >
                          <span className="font-mono">⊕</span> 选择文件
                        </button>
                        {uploadingKey === field.key && (
                          <span className="font-mono text-[11px] text-dim animate-pulse">
                            上传中…
                          </span>
                        )}
                        {field.file?.max_size_bytes && (
                          <span className="font-mono text-[10.5px] text-dim">
                            ≤ {humanSize(field.file.max_size_bytes)}
                          </span>
                        )}
                        {typeof field.default === 'string' && field.default && (
                          <span className="font-mono text-[10.5px] text-dim truncate">
                            默认 {field.default}
                          </span>
                        )}
                      </div>
                      {(fileValues[field.key] ?? []).length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {(fileValues[field.key] ?? []).map((att) => (
                            <div
                              key={att.id}
                              className="flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface"
                            >
                              <span className="text-ink-2">{isImage(att.mime) ? '🖼' : '📎'}</span>
                              <span className="max-w-[160px] truncate text-ink">{att.filename}</span>
                              <button
                                type="button"
                                onClick={() => removeFile(field.key, att.id)}
                                className="font-mono text-dim hover:text-ink ml-0.5 leading-none"
                                aria-label="移除附件"
                              >
                                ×
                              </button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  {err && <div className="text-[11.5px] text-accent">{err}</div>}
                </div>
              );
            })}
          </div>

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
              {submitting ? '执行中…' : '开始执行'}
            </button>
            <span className="font-mono text-[10.5px] text-dim">单轮运行 · 执行后查看进度与产出</span>
          </div>
        </div>
      </div>
    </div>
  );
}
