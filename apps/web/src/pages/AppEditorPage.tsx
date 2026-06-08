import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { api, ApiError } from '../api/client';
import { PageHead } from '../components/shell/PageHead';
import { Chip } from '../components/shell/Chip';
import { Dropdown } from '../components/shell/Dropdown';
import { useChatStore } from '../stores/chatStore';
import {
  MODEL_OPTIONS,
  EFFORT_OPTIONS,
  DEFAULT_MODEL,
  DEFAULT_EFFORT,
} from '../lib/modelOptions';
import {
  InputSchemaBuilder,
  validateSchema,
} from '../components/app/InputSchemaBuilder';
import type {
  App,
  AppVersion,
  AppVersionCreate,
  AppVisibility,
  InputField,
  McpServer,
  SamplingParams,
  Skill,
} from '../api/types';

const VISIBILITY_OPTIONS: { id: AppVisibility; label: string }[] = [
  { id: 'private', label: '私有（仅自己）' },
  { id: 'tenant', label: '租户（提交发布后可见）' },
];

/** Local editor state for the version harness. */
interface HarnessForm {
  version: string;
  system_prompt: string;
  user_prompt_template: string;
  model: string;
  reasoning_effort: string;
  temperature: string;
  top_p: string;
  max_tokens: string;
  forced_skills: string[];
  forced_mcp_server_ids: string[];
  input_schema: InputField[];
}

function emptyHarness(): HarnessForm {
  return {
    version: '1',
    system_prompt: '',
    user_prompt_template: '',
    model: DEFAULT_MODEL,
    reasoning_effort: DEFAULT_EFFORT,
    temperature: '',
    top_p: '',
    max_tokens: '',
    forced_skills: [],
    forced_mcp_server_ids: [],
    input_schema: [],
  };
}

/** Pick the version we should edit: the latest draft, else the current/last. */
function pickEditableVersion(app: App): AppVersion | null {
  const versions = app.versions ?? [];
  if (versions.length === 0) return null;
  const draft = [...versions].reverse().find((v) => v.status === 'draft');
  if (draft) return draft;
  if (app.current_version_id) {
    const cur = versions.find((v) => v.id === app.current_version_id);
    if (cur) return cur;
  }
  return versions[versions.length - 1];
}

function harnessFromVersion(v: AppVersion): HarnessForm {
  const sp = v.sampling_params ?? {};
  return {
    version: v.version,
    system_prompt: v.system_prompt,
    user_prompt_template: v.user_prompt_template,
    model: v.model || DEFAULT_MODEL,
    reasoning_effort: v.reasoning_effort ?? DEFAULT_EFFORT,
    temperature: sp.temperature != null ? String(sp.temperature) : '',
    top_p: sp.top_p != null ? String(sp.top_p) : '',
    max_tokens: sp.max_tokens != null ? String(sp.max_tokens) : '',
    forced_skills: v.forced_skills ?? [],
    forced_mcp_server_ids: v.forced_mcp_server_ids ?? [],
    input_schema: v.input_schema ?? [],
  };
}

function buildSamplingParams(h: HarnessForm): SamplingParams {
  const sp: SamplingParams = {};
  const t = parseFloat(h.temperature);
  const p = parseFloat(h.top_p);
  const m = parseInt(h.max_tokens, 10);
  if (Number.isFinite(t)) sp.temperature = t;
  if (Number.isFinite(p)) sp.top_p = p;
  if (Number.isFinite(m)) sp.max_tokens = m;
  return sp;
}

function buildVersionCreate(h: HarnessForm): AppVersionCreate {
  return {
    version: h.version.trim() || '1',
    system_prompt: h.system_prompt,
    user_prompt_template: h.user_prompt_template,
    model: h.model,
    reasoning_effort: h.reasoning_effort || null,
    sampling_params: buildSamplingParams(h),
    forced_skills: h.forced_skills,
    forced_mcp_server_ids: h.forced_mcp_server_ids,
    input_schema: h.input_schema,
  };
}

// ─── Section wrapper ──────────────────────────────────────────────────────────

function Section({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border border-line rounded-md bg-surface p-4 flex flex-col gap-3">
      <div>
        <h2 className="font-mono text-[11px] uppercase tracking-[0.1em] text-dim">
          {title}
        </h2>
        {hint && <p className="text-[11.5px] text-dim mt-1">{hint}</p>}
      </div>
      {children}
    </section>
  );
}

const inputCls =
  'border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper focus:outline-none focus:border-accent';

function nextAutoVersion(app: App): string {
  const versions = app.versions ?? [];
  let maxNumeric = 0;
  for (const v of versions) {
    const n = parseInt(v.version, 10);
    if (Number.isFinite(n) && String(n) === v.version) maxNumeric = Math.max(maxNumeric, n);
  }
  if (maxNumeric > 0) return String(maxNumeric + 1);
  return String(versions.length + 1);
}

function StepTabs({
  step,
  onStep,
}: {
  step: 1 | 2;
  onStep: (step: 1 | 2) => void;
}) {
  return (
    <div className="grid grid-cols-2 gap-2">
      {[
        { id: 1 as const, title: '1 定义自定义参数', sub: '用户填写项、默认值、文件限制' },
        { id: 2 as const, title: '2 定义 Prompt 与模型', sub: '固定流程、Skills、MCP、发布权限' },
      ].map((item) => {
        const active = step === item.id;
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => onStep(item.id)}
            className={`text-left border rounded-md px-3 py-2.5 bg-surface ${
              active ? 'border-ink-2' : 'border-line hover:border-ink-2'
            }`}
          >
            <div className="text-[13px] font-semibold text-ink">{item.title}</div>
            <div className="text-[11.5px] text-dim mt-0.5">{item.sub}</div>
          </button>
        );
      })}
    </div>
  );
}

function fieldReferenceLabel(field: InputField): string {
  if (field.type === 'file') return `/${field.key} · 文件路径`;
  if (field.type === 'multiselect') return `/${field.key} · 多选值`;
  return `/${field.key}`;
}

function PromptTemplateEditor({
  label,
  value,
  onChange,
  fields,
  placeholder,
  minHeight = 120,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  fields: InputField[];
  placeholder: string;
  minHeight?: number;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  const [slash, setSlash] = useState<{ start: number; query: string } | null>(null);

  const detectSlash = (text: string, caret: number) => {
    const before = text.slice(0, caret);
    const match = before.match(/(^|\s)\/([a-z0-9_]*)$/);
    if (!match) return null;
    return {
      start: caret - (match[2]?.length ?? 0) - 1,
      query: match[2] ?? '',
    };
  };

  const updateSlash = (text?: string, caret?: number) => {
    const el = ref.current;
    if (!el) return;
    setSlash(detectSlash(text ?? el.value, caret ?? el.selectionStart ?? 0));
  };

  const matches = useMemo(() => {
    if (!slash) return [];
    const q = slash.query.toLowerCase();
    return fields
      .filter((f) => {
        if (!q) return true;
        return f.key.includes(q) || f.label.toLowerCase().includes(q);
      })
      .slice(0, 8);
  }, [fields, slash]);

  const insertField = (field: InputField, activeSlash = slash) => {
    const el = ref.current;
    const text = el?.value ?? value;
    const caret = el?.selectionStart ?? text.length;
    const start = activeSlash?.start ?? caret;
    const next = `${text.slice(0, start)}/${field.key} ${text.slice(caret)}`;
    onChange(next);
    setSlash(null);
    requestAnimationFrame(() => {
      const pos = start + field.key.length + 2;
      ref.current?.focus();
      ref.current?.setSelectionRange(pos, pos);
    });
  };

  return (
    <div className="flex flex-col gap-1">
      <label className="cw-label">{label}</label>
      <textarea
        ref={ref}
        className={`${inputCls} resize-y font-mono text-[12px] leading-[1.55]`}
        style={{ minHeight }}
        value={value}
        onChange={(e) => {
          const next = e.target.value;
          onChange(next);
          updateSlash(next, e.target.selectionStart ?? next.length);
        }}
        onClick={() => updateSlash()}
        onKeyUp={() => updateSlash()}
        onKeyDown={(e) => {
          const el = ref.current;
          const activeSlash = el ? detectSlash(el.value, el.selectionStart ?? 0) : slash;
          const q = activeSlash?.query.toLowerCase() ?? '';
          const activeMatches = activeSlash
            ? fields
                .filter((f) => !q || f.key.includes(q) || f.label.toLowerCase().includes(q))
                .slice(0, 8)
            : [];
          if ((e.key === 'Enter' || e.key === 'Tab') && activeMatches[0]) {
            e.preventDefault();
            insertField(activeMatches[0], activeSlash);
          }
          if (e.key === 'Escape') setSlash(null);
        }}
        placeholder={placeholder}
      />
      <div className="flex flex-wrap gap-1.5">
        {fields.map((f) => (
          <Chip key={f.key} dashed onClick={() => insertField(f)}>
            {fieldReferenceLabel(f)}
          </Chip>
        ))}
        {fields.length === 0 && (
          <span className="font-mono text-[11px] text-dim">先在上一步定义参数后，可在这里用 / 引用。</span>
        )}
      </div>
      {slash && matches.length > 0 && (
        <div className="border border-line rounded-md bg-surface shadow-[0_8px_24px_-12px_rgba(15,17,20,0.18)] p-1 w-[min(360px,100%)]">
          {matches.map((f) => (
            <button
              key={f.key}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                insertField(f);
              }}
              className="w-full flex items-center gap-2 px-2 py-1.5 rounded-sm hover:bg-paper text-left"
            >
              <span className="font-mono text-[12px] text-ink">/{f.key}</span>
              <span className="text-[11.5px] text-dim truncate">{f.label}</span>
              <span className="font-mono text-[10px] text-dim ml-auto">{f.type}</span>
            </button>
          ))}
        </div>
      )}
      <span className="text-[11px] text-dim">
        输入 <code className="font-mono">/</code> 选择参数。文件字段会渲染为沙盒路径，例如 <code className="font-mono">/workspace/inputs/file.pdf</code>。
      </span>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export function AppEditorPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const isEdit = Boolean(id);

  const userProfile = useChatStore((s) => s.userProfile);
  const isAdmin = Boolean(userProfile?.roles?.includes('super_admin'));
  const [step, setStep] = useState<1 | 2>(1);

  // App-level fields
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [icon, setIcon] = useState('');
  const [visibility, setVisibility] = useState<AppVisibility>('private');

  // Version harness
  const [harness, setHarness] = useState<HarnessForm>(emptyHarness());

  // Library pickers
  const [skills, setSkills] = useState<Skill[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);

  // The version id we edit in EDIT mode (if it's an editable draft).
  const [draftVersionId, setDraftVersionId] = useState<string | null>(null);
  const [draftEditable, setDraftEditable] = useState(false);

  const [loading, setLoading] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<number, string>>({});

  // ── Load library lists + (edit mode) the app ──────────────────────────────
  useEffect(() => {
    let cancelled = false;
    api.library
      .listSkills()
      .then((r) => { if (!cancelled) setSkills(r.items); })
      .catch(() => { /* picker degrades gracefully to empty */ });
    api.library
      .listMcpServers()
      .then((r) => { if (!cancelled) setMcpServers(r.items); })
      .catch(() => { /* picker degrades gracefully to empty */ });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.apps
      .get(id)
      .then((app) => {
        if (cancelled) return;
        setName(app.name);
        setDescription(app.description ?? '');
        setIcon(app.icon ?? '');
        setVisibility(app.visibility);
        const v = pickEditableVersion(app);
        if (v) {
          const next = harnessFromVersion(v);
          if (v.status !== 'draft') next.version = nextAutoVersion(app);
          setHarness(next);
          setDraftVersionId(v.id);
          setDraftEditable(v.status === 'draft');
        }
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : '加载应用失败');
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [id]);

  const skillSelected = useMemo(
    () => new Set(harness.forced_skills),
    [harness.forced_skills],
  );
  const mcpSelected = useMemo(
    () => new Set(harness.forced_mcp_server_ids),
    [harness.forced_mcp_server_ids],
  );

  const patchHarness = (patch: Partial<HarnessForm>) =>
    setHarness((h) => ({ ...h, ...patch }));

  const toggleSkill = (skillName: string) =>
    setHarness((h) => ({
      ...h,
      forced_skills: h.forced_skills.includes(skillName)
        ? h.forced_skills.filter((s) => s !== skillName)
        : [...h.forced_skills, skillName],
    }));

  const toggleMcp = (mcpId: string) =>
    setHarness((h) => ({
      ...h,
      forced_mcp_server_ids: h.forced_mcp_server_ids.includes(mcpId)
        ? h.forced_mcp_server_ids.filter((m) => m !== mcpId)
        : [...h.forced_mcp_server_ids, mcpId],
    }));

  // ── Validation ────────────────────────────────────────────────────────────
  const validateInputStep = (): boolean => {
    if (!name.trim()) {
      setError('请填写应用名称');
      setStep(1);
      return false;
    }
    const { errors, ok } = validateSchema(harness.input_schema);
    setFieldErrors(errors);
    if (!ok) {
      setError('输入字段定义有误，请检查标红项');
      setStep(1);
      return false;
    }
    setError(null);
    return true;
  };

  /** Returns true when the whole form is structurally valid; sets errors otherwise. */
  const validate = (): boolean => {
    if (!validateInputStep()) return false;
    if (!harness.system_prompt.trim()) {
      setError('请填写系统提示词');
      setStep(2);
      return false;
    }
    if (!harness.user_prompt_template.trim()) {
      setError('请填写用户提示词模板');
      setStep(2);
      return false;
    }
    const t = harness.temperature ? parseFloat(harness.temperature) : undefined;
    const p = harness.top_p ? parseFloat(harness.top_p) : undefined;
    const m = harness.max_tokens ? parseInt(harness.max_tokens, 10) : undefined;
    if (t != null && (!Number.isFinite(t) || t < 0)) {
      setError('temperature 必须为大于等于 0 的数字');
      setStep(2);
      return false;
    }
    if (p != null && (!Number.isFinite(p) || p < 0 || p > 1)) {
      setError('top_p 必须在 0 到 1 之间');
      setStep(2);
      return false;
    }
    if (m != null && (!Number.isFinite(m) || m < 1)) {
      setError('max_tokens 必须为大于 0 的整数');
      setStep(2);
      return false;
    }
    setError(null);
    return true;
  };

  // ── Save: returns {appId, versionId} or null on failure ───────────────────
  /**
   * CREATE mode → api.apps.create (App + initial draft, atomic).
   * EDIT mode  → api.apps.update for app-level fields; for the harness, if the
   *   picked version is an editable draft we updateVersion in place, otherwise
   *   (published/pending/rejected) we createVersion to produce a fresh draft.
   */
  const persist = async (): Promise<{ appId: string; versionId: string } | null> => {
    const vc = buildVersionCreate(harness);
    if (!isEdit) {
      const created = await api.apps.create({
        name: name.trim(),
        description: description.trim() || null,
        icon: icon.trim() || null,
        visibility,
        version: vc,
      });
      const v = pickEditableVersion(created) ?? created.versions?.[0] ?? null;
      return { appId: created.id, versionId: v?.id ?? '' };
    }
    // EDIT
    const appId = id!;
    await api.apps.update(appId, {
      name: name.trim(),
      description: description.trim() || null,
      icon: icon.trim() || null,
      visibility,
    });
    let versionId: string;
    if (draftEditable && draftVersionId) {
      const v = await api.apps.updateVersion(appId, draftVersionId, vc);
      versionId = v.id;
    } else {
      const v = await api.apps.createVersion(appId, vc);
      versionId = v.id;
      // The new draft is now the editable one — keep state consistent in case
      // the user keeps editing on this page.
      setDraftVersionId(v.id);
      setDraftEditable(true);
    }
    return { appId, versionId };
  };

  // ── Actions ───────────────────────────────────────────────────────────────
  const handleSaveDraft = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      const res = await persist();
      if (res) navigate(`/apps/${res.appId}/edit`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleNextStep = () => {
    if (validateInputStep()) setStep(2);
  };

  const handleSubmit = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      const res = await persist();
      if (res?.versionId) {
        await api.apps.submit(res.appId, res.versionId);
        navigate('/home');
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '提交审核失败');
    } finally {
      setSaving(false);
    }
  };

  const handlePublish = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      const res = await persist();
      if (res?.versionId) {
        await api.apps.publish(res.appId, res.versionId);
        navigate('/home');
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setError('没有发布权限（仅管理员可直接发布）');
      } else {
        setError(err instanceof ApiError ? err.message : '发布失败');
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col h-full bg-paper">
        <PageHead crumbs={[{ label: '首页', to: '/home' }, { label: '编辑应用' }]} />
        <div className="font-mono text-[12px] text-dim p-6">加载中…</div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: '首页', to: '/home' },
          { label: isEdit ? '编辑应用' : '创建应用' },
        ]}
        actions={
          <>
            <button
              onClick={() => navigate('/home')}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={saving}
            >
              取消
            </button>
            {step === 1 ? (
              <button
                onClick={handleNextStep}
                className="cw-btn cw-btn-primary cw-btn-sm"
                disabled={saving}
              >
                下一步
              </button>
            ) : (
              <>
                <button
                  onClick={() => setStep(1)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  disabled={saving}
                >
                  上一步
                </button>
                <button
                  onClick={handleSaveDraft}
                  className="cw-btn cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? '保存中…' : '保存草稿'}
                </button>
                <button
                  onClick={handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  提交审核
                </button>
                {isAdmin && (
                  <button
                    onClick={handlePublish}
                    className="cw-btn cw-btn-accent cw-btn-sm"
                    disabled={saving}
                  >
                    发布
                  </button>
                )}
              </>
            )}
          </>
        }
      />

      <div className="cw-scroll flex-1 min-h-0 overflow-auto px-[5vw] py-6">
        <div className="w-[min(880px,100%)] mx-auto flex flex-col gap-4">
          {error && (
            <div className="border border-accent-line bg-accent-soft rounded-md px-3 py-2 text-[13px] text-accent">
              {error}
            </div>
          )}

          <StepTabs
            step={step}
            onStep={(next) => {
              if (next === 2) handleNextStep();
              else setStep(1);
            }}
          />

          {step === 1 && (
            <>
          {/* ── App-level fields ── */}
          <Section title="基本信息">
            <div className="grid grid-cols-1 sm:grid-cols-[1fr_120px] gap-3">
              <div className="flex flex-col gap-1">
                <label className="cw-label">名称 *</label>
                <input
                  className={inputCls}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="合同审查助手"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="cw-label">图标 / Emoji</label>
                <input
                  className={inputCls}
                  value={icon}
                  onChange={(e) => setIcon(e.target.value)}
                  placeholder="◐"
                  maxLength={4}
                />
              </div>
            </div>
            <div className="flex flex-col gap-1">
              <label className="cw-label">描述</label>
              <textarea
                className={`${inputCls} min-h-[60px] resize-y`}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="一句话说明这个应用做什么"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label className="cw-label">可见性</label>
              <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center w-fit">
                <Dropdown
                  value={visibility}
                  options={VISIBILITY_OPTIONS}
                  onChange={(v) => setVisibility(v as AppVisibility)}
                  align="left"
                  title="可见性"
                />
              </div>
            </div>
          </Section>
            </>
          )}

          {step === 2 && (
            <>
          {/* ── Harness ── */}
          <Section
            title="运行配置 (Harness)"
            hint={`版本 ${harness.version}${
              isEdit && !draftEditable
                ? ' · 当前版本不可改，保存将创建新草稿版本'
                : ''
            }`}
          >
            <div className="grid grid-cols-1 sm:grid-cols-[120px_1fr] gap-3">
              <div className="flex flex-col gap-1">
                <label className="cw-label">版本号</label>
                <div className="border border-line rounded-sm px-2 py-1.5 text-[13px] bg-paper font-mono text-ink">
                  v{harness.version}
                </div>
              </div>
              <div className="flex items-end text-[11px] text-dim pb-1">
                版本号由服务端按应用自增分配；编辑草稿会保留当前版本，基于已发布版本保存会生成下一版草稿。
              </div>
            </div>
            <PromptTemplateEditor
              label="系统提示词 (system prompt)"
              value={harness.system_prompt}
              onChange={(system_prompt) => patchHarness({ system_prompt })}
              fields={harness.input_schema}
              placeholder="你是一名严谨的合同审查专家。必须按组织标准输出审查摘要、风险清单和修改建议…"
            />
            <PromptTemplateEditor
              label="用户提示词模板 (user prompt template)"
              value={harness.user_prompt_template}
              onChange={(user_prompt_template) => patchHarness({ user_prompt_template })}
              fields={harness.input_schema}
              placeholder="请审查 /contract，重点关注 /focus。若有附件，请读取 /attachment。"
              minHeight={140}
            />

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div className="flex flex-col gap-1">
                <label className="cw-label">模型</label>
                <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                  <Dropdown
                    value={harness.model}
                    options={MODEL_OPTIONS}
                    onChange={(m) => patchHarness({ model: m })}
                    align="left"
                    title="模型"
                  />
                </div>
              </div>
              <div className="flex flex-col gap-1">
                <label className="cw-label">推理强度</label>
                <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                  <Dropdown
                    value={harness.reasoning_effort}
                    options={EFFORT_OPTIONS}
                    onChange={(e) => patchHarness({ reasoning_effort: e })}
                    align="left"
                    title="推理强度"
                  />
                </div>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div className="flex flex-col gap-1">
                <label className="cw-label">temperature</label>
                <input
                  type="number"
                  step={0.1}
                  className={inputCls}
                  value={harness.temperature}
                  onChange={(e) => patchHarness({ temperature: e.target.value })}
                  placeholder="（默认）"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="cw-label">top_p</label>
                <input
                  type="number"
                  step={0.05}
                  className={inputCls}
                  value={harness.top_p}
                  onChange={(e) => patchHarness({ top_p: e.target.value })}
                  placeholder="（默认）"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="cw-label">max_tokens</label>
                <input
                  type="number"
                  step={1}
                  className={inputCls}
                  value={harness.max_tokens}
                  onChange={(e) => patchHarness({ max_tokens: e.target.value })}
                  placeholder="（默认）"
                />
              </div>
            </div>
          </Section>

          {/* ── Forced skills ── */}
          <Section
            title="强制启用 Skills"
            hint="运行该应用时固定开启这些技能，忽略用户个人工具箱。"
          >
            {skills.length === 0 ? (
              <span className="text-[12px] text-dim italic font-mono">
                无可用技能
              </span>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {skills.map((s) => (
                  <Chip
                    key={s.id}
                    accent={skillSelected.has(s.name)}
                    onClick={() => toggleSkill(s.name)}
                  >
                    {skillSelected.has(s.name) ? '✓ ' : ''}
                    {s.name}
                  </Chip>
                ))}
              </div>
            )}
          </Section>

          {/* ── Forced MCP servers ── */}
          <Section
            title="强制 MCP 服务（前向兼容，仅记录）"
            hint="v1 仅存储不强制过滤，运行时仍由租户全局注入。"
          >
            {mcpServers.length === 0 ? (
              <span className="text-[12px] text-dim italic font-mono">
                无可用 MCP 服务
              </span>
            ) : (
              <div className="flex flex-wrap gap-1.5">
                {mcpServers.map((m) => (
                  <Chip
                    key={m.id}
                    accent={mcpSelected.has(m.id)}
                    onClick={() => toggleMcp(m.id)}
                  >
                    {mcpSelected.has(m.id) ? '✓ ' : ''}
                    {m.name}
                  </Chip>
                ))}
              </div>
            )}
          </Section>
            </>
          )}

          {/* ── Input schema builder ── */}
          {step === 1 && (
            <Section
              title="输入表单字段"
              hint="定义用户运行该应用时需要填写的字段，后续 Prompt 中可用 /字段标识 引用。"
            >
              <InputSchemaBuilder
                fields={harness.input_schema}
                onChange={(input_schema) => patchHarness({ input_schema })}
                errors={fieldErrors}
              />
            </Section>
          )}

          {/* ── Footer actions (mirror header for long forms) ── */}
          <div className="flex items-center justify-end gap-2 pb-8">
            {step === 1 ? (
              <button
                onClick={handleNextStep}
                className="cw-btn cw-btn-primary cw-btn-sm"
                disabled={saving}
              >
                下一步
              </button>
            ) : (
              <>
                <button
                  onClick={() => setStep(1)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  disabled={saving}
                >
                  上一步
                </button>
                <button
                  onClick={handleSaveDraft}
                  className="cw-btn cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? '保存中…' : '保存草稿'}
                </button>
                <button
                  onClick={handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  提交审核
                </button>
                {isAdmin && (
                  <button
                    onClick={handlePublish}
                    className="cw-btn cw-btn-accent cw-btn-sm"
                    disabled={saving}
                  >
                    发布
                  </button>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
