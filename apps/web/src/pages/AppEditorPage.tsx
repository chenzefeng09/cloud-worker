import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  blankField,
} from '../components/app/InputSchemaBuilder';
import type {
  App,
  AppVersion,
  AppVersionCreate,
  InputField,
  McpServer,
  SamplingParams,
  Skill,
} from '../api/types';

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
    input_schema: [blankField()],
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
    input_schema: v.input_schema && v.input_schema.length > 0 ? v.input_schema : [blankField()],
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
        { id: 1 as const, title: '1 定义任务输入', sub: '用户填写项、默认值、文件限制' },
        { id: 2 as const, title: '2 定义执行环境', sub: '固定流程、Skills、MCP、发布权限' },
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

/** Token type for PromptTemplateEditor internal representation */
const PARAM_RE = /(\/[a-zA-Z][a-zA-Z0-9_-]*)/g;

/** Parse plain text value into alternating text/param segments */
function parseTokens(text: string): { type: 'text' | 'param'; value: string }[] {
  const tokens: { type: 'text' | 'param'; value: string }[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = PARAM_RE.exec(text)) !== null) {
    if (m.index > lastIndex) {
      tokens.push({ type: 'text', value: text.slice(lastIndex, m.index) });
    }
    tokens.push({ type: 'param', value: m[1] });
    lastIndex = PARAM_RE.lastIndex;
  }
  if (lastIndex < text.length) {
    tokens.push({ type: 'text', value: text.slice(lastIndex) });
  }
  PARAM_RE.lastIndex = 0;
  return tokens;
}

/** Build innerHTML from the plain-text value, highlighting known param keys as chips */
function buildHtml(text: string, fieldKeys: Set<string>): string {
  const tokens = parseTokens(text);
  return tokens.map((t) => {
    if (t.type === 'param') {
      const key = t.value.slice(1); // remove leading /
      if (fieldKeys.has(key)) {
        return `<span class="prompt-chip" contenteditable="false" data-param-key="${key}">${t.value}</span>`;
      }
    }
    // Escape HTML for text nodes
    return t.value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }).join('');
}

/** Extract plain text from the contenteditable div's DOM */
function extractText(el: HTMLElement): string {
  let result = '';
  el.childNodes.forEach((node) => {
    if (node.nodeType === Node.TEXT_NODE) {
      result += node.textContent ?? '';
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const elem = node as HTMLElement;
      if (elem.dataset.paramKey) {
        result += `/${elem.dataset.paramKey}`;
      } else if (elem.tagName === 'BR') {
        result += '\n';
      } else if (elem.tagName === 'DIV' || elem.tagName === 'P') {
        // Browser sometimes wraps lines in divs
        if (result.length > 0 && !result.endsWith('\n')) result += '\n';
        result += extractText(elem);
      } else {
        result += extractText(elem);
      }
    }
  });
  return result;
}

/** Get caret pixel position for positioning the floating popup */
function getCaretCoords(): { top: number; left: number } | null {
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0) return null;
  const range = sel.getRangeAt(0).cloneRange();
  range.collapse(true);
  const rect = range.getBoundingClientRect();
  // If rect is zero (e.g. empty line), fall back
  if (rect.top === 0 && rect.left === 0) return null;
  return { top: rect.bottom, left: rect.left };
}

function PromptTemplateEditor({
  label,
  value,
  onChange,
  fields,
  placeholder,
  minHeight = 120,
  error,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  fields: InputField[];
  placeholder: string;
  minHeight?: number;
  error?: string;
}) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [popupPos, setPopupPos] = useState<{ top: number; left: number } | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);
  const suppressSync = useRef(false);

  const fieldKeys = useMemo(() => new Set(fields.map((f) => f.key)), [fields]);

  const matches = useMemo(() => {
    if (slashQuery === null) return [];
    const q = slashQuery.toLowerCase();
    return fields
      .filter((f) => !q || f.key.includes(q) || f.label.toLowerCase().includes(q))
      .slice(0, 8);
  }, [fields, slashQuery]);

  // Sync DOM from value when value changes externally
  useEffect(() => {
    const el = editorRef.current;
    if (!el || suppressSync.current) {
      suppressSync.current = false;
      return;
    }
    const html = buildHtml(value, fieldKeys);
    if (el.innerHTML !== html) {
      el.innerHTML = html;
    }
  }, [value, fieldKeys]);

  const detectSlashAtCaret = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) { setSlashQuery(null); return; }
    const node = sel.anchorNode;
    if (!node || node.nodeType !== Node.TEXT_NODE) { setSlashQuery(null); return; }
    const text = node.textContent ?? '';
    const offset = sel.anchorOffset;
    const before = text.slice(0, offset);
    // Match slash followed by optional filter chars at the end of the text before caret
    const m = before.match(/(^|[\s\n])\/([a-zA-Z0-9_-]*)$/);
    if (!m) { setSlashQuery(null); setPopupPos(null); return; }
    const newQuery = m[2];
    setSlashQuery((prev) => {
      if (prev !== newQuery) setActiveIdx(0);
      return newQuery;
    });
    const coords = getCaretCoords();
    if (coords && wrapperRef.current) {
      const wrapperRect = wrapperRef.current.getBoundingClientRect();
      setPopupPos({
        top: coords.top - wrapperRect.top + 4,
        left: coords.left - wrapperRect.left,
      });
    }
  }, []);

  const handleInput = useCallback(() => {
    const el = editorRef.current;
    if (!el) return;
    const plain = extractText(el);
    suppressSync.current = true;
    onChange(plain);
    detectSlashAtCaret();
  }, [onChange, detectSlashAtCaret]);

  const insertParamChip = useCallback((field: InputField) => {
    const el = editorRef.current;
    if (!el) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) return;

    // Find and delete the slash + query text
    const node = sel.anchorNode;
    if (node && node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent ?? '';
      const offset = sel.anchorOffset;
      const before = text.slice(0, offset);
      const m = before.match(/(^|[\s\n])\/([a-zA-Z0-9_-]*)$/);
      if (m) {
        const slashStart = offset - m[0].length + (m[1]?.length ?? 0);
        // Delete the /query text
        const range = document.createRange();
        range.setStart(node, slashStart);
        range.setEnd(node, offset);
        range.deleteContents();
      }
    }

    // Create the chip span
    const chip = document.createElement('span');
    chip.className = 'prompt-chip';
    chip.contentEditable = 'false';
    chip.dataset.paramKey = field.key;
    chip.textContent = `/${field.key}`;

    // Insert chip + trailing space
    const range2 = sel.getRangeAt(0);
    range2.collapse(true);
    range2.insertNode(chip);
    // Add a space after chip for caret placement
    const space = document.createTextNode(' ');
    chip.after(space);
    // Move caret after the space
    const newRange = document.createRange();
    newRange.setStartAfter(space);
    newRange.collapse(true);
    sel.removeAllRanges();
    sel.addRange(newRange);

    setSlashQuery(null);
    setPopupPos(null);

    // Sync plain text
    suppressSync.current = true;
    onChange(extractText(el));
  }, [onChange]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (slashQuery !== null && matches.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIdx((i) => Math.min(i + 1, matches.length - 1));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIdx((i) => Math.max(i - 1, 0));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        insertParamChip(matches[activeIdx]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setSlashQuery(null);
        setPopupPos(null);
        return;
      }
    }
  }, [slashQuery, matches, activeIdx, insertParamChip]);

  const handleChipClick = useCallback((field: InputField) => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    // Append at end
    const chip = document.createElement('span');
    chip.className = 'prompt-chip';
    chip.contentEditable = 'false';
    chip.dataset.paramKey = field.key;
    chip.textContent = `/${field.key}`;
    const space = document.createTextNode(' ');
    el.appendChild(chip);
    el.appendChild(space);
    const sel = window.getSelection();
    const range = document.createRange();
    range.setStartAfter(space);
    range.collapse(true);
    sel?.removeAllRanges();
    sel?.addRange(range);
    suppressSync.current = true;
    onChange(extractText(el));
  }, [onChange]);

  const showPlaceholder = !value || !value.trim();

  return (
    <div ref={wrapperRef} className="flex flex-col gap-1 relative">
      <label className="cw-label">{label}</label>
      <div className="relative">
        <div
          ref={editorRef}
          contentEditable
          suppressContentEditableWarning
          className={`${inputCls.replace('border-line', error ? 'border-accent' : 'border-line')} resize-y font-mono text-[12px] leading-[1.55] whitespace-pre-wrap break-words overflow-auto outline-none`}
          style={{ minHeight }}
          onInput={handleInput}
          onClick={() => detectSlashAtCaret()}
          onKeyUp={(e) => {
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === 'Tab') return;
            detectSlashAtCaret();
          }}
          onKeyDown={handleKeyDown}
        />
        {showPlaceholder && (
          <div
            className="absolute top-0 left-0 px-2 py-1.5 text-[12px] font-mono text-dim pointer-events-none select-none"
            style={{ lineHeight: '1.55' }}
          >
            {placeholder}
          </div>
        )}
      </div>
      {error && (
        <span className="text-[10.5px] text-accent mt-[-2px]">{error}</span>
      )}
      <div className="flex flex-wrap gap-1.5">
        {fields.map((f) => (
          <Chip key={f.key} dashed onClick={() => handleChipClick(f)}>
            {f.type === 'file' ? `/${f.key} \u00b7 文件路径` : f.type === 'multiselect' ? `/${f.key} \u00b7 多选值` : `/${f.key}`}
          </Chip>
        ))}
        {fields.length === 0 && (
          <span className="font-mono text-[11px] text-dim">先在上一步定义参数后，可在这里用 / 引用。</span>
        )}
      </div>
      {slashQuery !== null && matches.length > 0 && popupPos && (
        <div
          className="absolute z-50 border border-line rounded-md bg-surface shadow-[0_8px_24px_-12px_rgba(15,17,20,0.18)] p-1 w-[min(320px,90%)]"
          style={{ top: popupPos.top, left: popupPos.left }}
        >
          {matches.map((f, i) => (
            <button
              key={f.key}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                insertParamChip(f);
              }}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-sm text-left ${
                i === activeIdx ? 'bg-paper' : 'hover:bg-paper'
              }`}
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

  const userProfile = useChatStore((s) => s.userProfile);
  const isAdmin = Boolean(userProfile?.roles?.includes('super_admin'));
  const [step, setStep] = useState<1 | 2>(1);
  const [showAdvanced, setShowAdvanced] = useState(false);

  // App name for breadcrumb display (read-only; edited via the creation modal)
  const [appName, setAppName] = useState('');

  // Version harness
  const [harness, setHarness] = useState<HarnessForm>(emptyHarness());

  // Library pickers
  const [skills, setSkills] = useState<Skill[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServer[]>([]);

  // The version id we edit in EDIT mode (if it's an editable draft).
  const [draftVersionId, setDraftVersionId] = useState<string | null>(null);
  const [draftEditable, setDraftEditable] = useState(false);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<number, { path: string; msg: string }>>({});
  const [harnessErrors, setHarnessErrors] = useState<{
    system_prompt?: string;
    user_prompt_template?: string;
    temperature?: string;
    top_p?: string;
    max_tokens?: string;
  }>({});

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
    if (!id) {
      navigate('/home', { replace: true });
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.apps
      .get(id)
      .then((app) => {
        if (cancelled) return;
        setAppName(app.name);
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
  }, [id, navigate]);

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
    
    let ok = true;
    const he: typeof harnessErrors = {};

    if (!harness.system_prompt.trim()) {
      he.system_prompt = '请填写系统提示词';
      ok = false;
    }
    if (!harness.user_prompt_template.trim()) {
      he.user_prompt_template = '请填写用户提示词模板';
      ok = false;
    }
    const t = harness.temperature ? parseFloat(harness.temperature) : undefined;
    const p = harness.top_p ? parseFloat(harness.top_p) : undefined;
    const m = harness.max_tokens ? parseInt(harness.max_tokens, 10) : undefined;
    if (t != null && (!Number.isFinite(t) || t < 0)) {
      he.temperature = 'temperature 必须为大于等于 0 的数字';
      ok = false;
    }
    if (p != null && (!Number.isFinite(p) || p < 0 || p > 1)) {
      he.top_p = 'top_p 必须在 0 到 1 之间';
      ok = false;
    }
    if (m != null && (!Number.isFinite(m) || m < 1)) {
      he.max_tokens = 'max_tokens 必须为大于 0 的整数';
      ok = false;
    }
    
    setHarnessErrors(he);

    if (!ok) {
      setError('运行配置有误，请检查标红项');
      if (he.temperature || he.top_p || he.max_tokens) {
        setShowAdvanced(true);
      }
      setStep(2);
      return false;
    }

    setError(null);
    return true;
  };

  // ── Save: returns {appId, versionId} or null on failure ───────────────────
  /**
   * Update the harness version. If the picked version is an
   * editable draft we updateVersion in place, otherwise we createVersion
   * to produce a fresh draft.
   */
  const persist = async (): Promise<{ appId: string; versionId: string } | null> => {
    const appId = id!;
    const vc = buildVersionCreate(harness);
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


  const handleNextStep = () => {
    if (validateInputStep()) setStep(2);
  };

  const handleSaveDraft = async () => {
    if (!validate()) return;
    setSaving(true);
    try {
      await persist();
      window.alert('已保存为草稿。需要发布后更改才生效。');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    if (window.confirm('你所做的更改不会被保存，确认离开吗？')) {
      navigate('/home');
    }
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
          { label: appName || '编辑应用' },
        ]}
        actions={
          <>
            <button
              onClick={handleCancel}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={saving}
            >
              取消
            </button>
            <button
              onClick={handleSaveDraft}
              className="cw-btn cw-btn-sm"
              disabled={saving}
            >
              {saving ? '保存中…' : '保存'}
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
                  onClick={isAdmin ? handlePublish : handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? '发布中…' : '发布'}
                </button>
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

          {step === 2 && (
            <>
          {/* ── Harness ── */}
          <Section
            title="运行配置 (Harness)"
            hint={!draftEditable ? '当前配置不可改，保存将创建新草稿' : ''}
          >
            <PromptTemplateEditor
              label="系统提示词 (system prompt)"
              value={harness.system_prompt}
              onChange={(system_prompt) => patchHarness({ system_prompt })}
              fields={harness.input_schema}
              placeholder="你是一名严谨的合同审查专家。必须按组织标准输出审查摘要、风险清单和修改建议…"
              error={harnessErrors.system_prompt}
            />
            <PromptTemplateEditor
              label="用户提示词模板 (user prompt template)"
              value={harness.user_prompt_template}
              onChange={(user_prompt_template) => patchHarness({ user_prompt_template })}
              fields={harness.input_schema}
              placeholder="请审查 /contract，重点关注 /focus。若有附件，请读取 /attachment。"
              minHeight={140}
              error={harnessErrors.user_prompt_template}
            />

            <div className="flex flex-col gap-1 max-w-xs">
              <label className="cw-label">模型</label>
              <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                <Dropdown
                  value={harness.model}
                  options={MODEL_OPTIONS}
                  onChange={(m) => patchHarness({ model: m })}
                  align="left"
                  title="模型"
                  fullWidth
                />
              </div>
            </div>

            <div className="border-t border-line pt-4 mt-2">
              <button
                type="button"
                onClick={() => setShowAdvanced(!showAdvanced)}
                className="flex items-center gap-1.5 text-[12px] text-dim hover:text-ink font-medium select-none"
              >
                <span>{showAdvanced ? '▼' : '▶'}</span> 高级设置
              </button>
              {showAdvanced && (
                <div className="mt-3 flex flex-col gap-4">
                  <div className="flex flex-col gap-1 max-w-xs">
                    <label className="cw-label">推理强度</label>
                    <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                      <Dropdown
                        value={harness.reasoning_effort}
                        options={EFFORT_OPTIONS}
                        onChange={(e) => patchHarness({ reasoning_effort: e })}
                        align="left"
                        title="推理强度"
                        fullWidth
                      />
                    </div>
                  </div>

                  <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                    <div className="flex flex-col gap-1">
                      <label className="cw-label">temperature</label>
                      <input
                        type="number"
                        step={0.1}
                        className={harnessErrors.temperature ? inputCls.replace('border-line', 'border-accent') : inputCls}
                        value={harness.temperature}
                        onChange={(e) => patchHarness({ temperature: e.target.value })}
                        placeholder="（默认）"
                      />
                      {harnessErrors.temperature && (
                        <span className="text-[10.5px] text-accent">{harnessErrors.temperature}</span>
                      )}
                    </div>
                    <div className="flex flex-col gap-1">
                      <label className="cw-label">top_p</label>
                      <input
                        type="number"
                        step={0.05}
                        className={harnessErrors.top_p ? inputCls.replace('border-line', 'border-accent') : inputCls}
                        value={harness.top_p}
                        onChange={(e) => patchHarness({ top_p: e.target.value })}
                        placeholder="（默认）"
                      />
                      {harnessErrors.top_p && (
                        <span className="text-[10.5px] text-accent">{harnessErrors.top_p}</span>
                      )}
                    </div>
                    <div className="flex flex-col gap-1">
                      <label className="cw-label">max_tokens</label>
                      <input
                        type="number"
                        step={1}
                        className={harnessErrors.max_tokens ? inputCls.replace('border-line', 'border-accent') : inputCls}
                        value={harness.max_tokens}
                        onChange={(e) => patchHarness({ max_tokens: e.target.value })}
                        placeholder="（默认）"
                      />
                      {harnessErrors.max_tokens && (
                        <span className="text-[10.5px] text-accent">{harnessErrors.max_tokens}</span>
                      )}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </Section>

          {/* ── Skills ── */}
          <Section
            title="配置技能 (Skills)"
            hint="运行该应用时固定开启这些技能。"
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

          {/* ── MCP servers ── */}
          <Section
            title="MCP 服务配置（前向兼容，仅记录）"
            hint="v1 仅存储不进行过滤，运行时仍由租户全局注入。"
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
            <button
              onClick={handleCancel}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={saving}
            >
              取消
            </button>
            <button
              onClick={handleSaveDraft}
              className="cw-btn cw-btn-sm"
              disabled={saving}
            >
              {saving ? '保存中…' : '保存'}
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
                  onClick={isAdmin ? handlePublish : handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? '发布中…' : '发布'}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
