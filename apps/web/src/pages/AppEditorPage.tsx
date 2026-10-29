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
import {
  EmojiPicker,
  VisibilitySelect,
  appInputCls,
} from '../components/app/AppMetaFields';
import { useConfirm, useToast } from '../admin/components/dialogs';
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

/** App-level metadata — takes effect immediately, unlike the version harness. */
interface MetaForm {
  name: string;
  icon: string;
  description: string;
  visibility: AppVisibility;
}

function metaFromApp(app: App): MetaForm {
  return {
    name: app.name ?? '',
    icon: app.icon ?? '',
    description: app.description ?? '',
    visibility: app.visibility ?? 'private',
  };
}

function metaEqual(a: MetaForm, b: MetaForm): boolean {
  return (
    a.name.trim() === b.name.trim() &&
    a.icon.trim() === b.icon.trim() &&
    a.description.trim() === b.description.trim() &&
    a.visibility === b.visibility
  );
}

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

/**
 * The system prompt is optional. Legacy drafts were created with a single
 * space as a placeholder (the API used to reject empty strings), so treat
 * whitespace-only values as empty — otherwise the textarea looks blank but
 * hides an invisible space and never shows its placeholder hint.
 */
function normalizeSystemPrompt(value: string | null | undefined): string {
  return value && value.trim() ? value : '';
}

function harnessFromVersion(v: AppVersion): HarnessForm {
  const sp = v.sampling_params ?? {};
  return {
    version: v.version,
    system_prompt: normalizeSystemPrompt(v.system_prompt),
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
    system_prompt: normalizeSystemPrompt(h.system_prompt),
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
        { id: 1 as const, title: '1 Define task inputs', sub: 'App info, user fields, file limits' },
        { id: 2 as const, title: '2 Define execution environment', sub: 'Fixed flow, skills, MCP, publishing' },
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
            {f.type === 'file' ? `/${f.key} \u00b7 file path` : f.type === 'multiselect' ? `/${f.key} \u00b7 multi-select values` : `/${f.key}`}
          </Chip>
        ))}
        {fields.length === 0 && (
          <span className="font-mono text-[11px] text-dim">Define parameters in the previous step, then reference them here with /.</span>
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
        Type <code className="font-mono">/</code> to pick a parameter. File fields render as sandbox paths, e.g. <code className="font-mono">/workspace/inputs/file.pdf</code>.
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

  const confirm = useConfirm();
  const toast = useToast();

  // App-level info, edited in step 1. `metaBaseline` is the last persisted
  // snapshot so we only PUT /apps/{id} when something actually changed.
  const [meta, setMeta] = useState<MetaForm>({
    name: '',
    icon: '',
    description: '',
    visibility: 'private',
  });
  const [metaBaseline, setMetaBaseline] = useState<MetaForm>(meta);
  const [metaError, setMetaError] = useState<string | null>(null);

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
        const m = metaFromApp(app);
        setMeta(m);
        setMetaBaseline(m);
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
          setError(err instanceof ApiError ? err.message : 'Failed to load app');
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
  // Both the app-info block and the schema builder live on step 1, so the
  // step-1 gate checks them together.
  const validateInputStep = (): boolean => {
    if (!meta.name.trim()) {
      setMetaError('Please enter an app name');
      setError('App info has errors — check the highlighted fields');
      setStep(1);
      return false;
    }
    setMetaError(null);
    const { errors, ok } = validateSchema(harness.input_schema);
    setFieldErrors(errors);
    if (!ok) {
      setError('Input field definitions have errors — check the highlighted fields');
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

    // system_prompt is optional — leaving it empty falls back to the worker's
    // default persona.
    if (!harness.user_prompt_template.trim()) {
      he.user_prompt_template = 'Please enter a user prompt template';
      ok = false;
    }
    const t = harness.temperature ? parseFloat(harness.temperature) : undefined;
    const p = harness.top_p ? parseFloat(harness.top_p) : undefined;
    const m = harness.max_tokens ? parseInt(harness.max_tokens, 10) : undefined;
    if (t != null && (!Number.isFinite(t) || t < 0)) {
      he.temperature = 'temperature must be a number ≥ 0';
      ok = false;
    }
    if (p != null && (!Number.isFinite(p) || p < 0 || p > 1)) {
      he.top_p = 'top_p must be between 0 and 1';
      ok = false;
    }
    if (m != null && (!Number.isFinite(m) || m < 1)) {
      he.max_tokens = 'max_tokens must be an integer > 0';
      ok = false;
    }
    
    setHarnessErrors(he);

    if (!ok) {
      setError('Run configuration has errors — check the highlighted fields');
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

    // App info is immediate-effect and independent of the version lifecycle —
    // push it first, and only when it actually differs from what's on file.
    if (!metaEqual(meta, metaBaseline)) {
      await api.apps.update(appId, {
        name: meta.name.trim(),
        icon: meta.icon.trim() || null,
        description: meta.description.trim() || null,
        visibility: meta.visibility,
      });
      setMetaBaseline(meta);
    }

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
      toast.success('Saved as draft. Version content takes effect once published.');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Save failed');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = async () => {
    const ok = await confirm({
      title: 'Discard changes',
      message: 'Your changes won’t be saved. Leave anyway?',
      tone: 'danger',
      confirmText: 'Leave',
    });
    if (ok) navigate('/home');
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
      setError(err instanceof ApiError ? err.message : 'Failed to submit for review');
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
        setError('You don’t have permission to publish (only admins can publish directly)');
      } else {
        setError(err instanceof ApiError ? err.message : 'Publish failed');
      }
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex flex-col h-full bg-paper">
        <PageHead crumbs={[{ label: 'Home', to: '/home' }, { label: 'Edit app' }]} />
        <div className="font-mono text-[12px] text-dim p-6">Loading…</div>
      </div>
    );
  }


  return (
    <div className="flex flex-col h-full min-w-0 bg-paper">
      <PageHead
        crumbs={[
          { label: 'Home', to: '/home' },
          { label: metaBaseline.name || 'Edit app' },
        ]}
        actions={
          <>
            <button
              onClick={handleCancel}
              className="cw-btn cw-btn-ghost cw-btn-sm"
              disabled={saving}
            >
              Cancel
            </button>
            <button
              onClick={handleSaveDraft}
              className="cw-btn cw-btn-sm"
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            {step === 1 ? (
              <button
                onClick={handleNextStep}
                className="cw-btn cw-btn-primary cw-btn-sm"
                disabled={saving}
              >
                Next
              </button>
            ) : (
              <>
                <button
                  onClick={() => setStep(1)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  disabled={saving}
                >
                  Back
                </button>
                <button
                  onClick={isAdmin ? handlePublish : handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? 'Publishing…' : 'Publish'}
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
            title="Run configuration (harness)"
            hint={!draftEditable ? 'This configuration is read-only; saving creates a new draft' : ''}
          >
            <PromptTemplateEditor
              label="System prompt (optional)"
              value={harness.system_prompt}
              onChange={(system_prompt) => patchHarness({ system_prompt })}
              fields={harness.input_schema}
              placeholder="You are a meticulous contract reviewer. Always output a review summary, risk list and suggested edits per our standards…"
              error={harnessErrors.system_prompt}
            />
            <PromptTemplateEditor
              label="User prompt template"
              value={harness.user_prompt_template}
              onChange={(user_prompt_template) => patchHarness({ user_prompt_template })}
              fields={harness.input_schema}
              placeholder="Review /contract, focusing on /focus. If there is an attachment, read /attachment."
              minHeight={140}
              error={harnessErrors.user_prompt_template}
            />

            <div className="flex flex-col gap-1 max-w-xs">
              <label className="cw-label">Model</label>
              <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                <Dropdown
                  value={harness.model}
                  options={MODEL_OPTIONS}
                  onChange={(m) => patchHarness({ model: m })}
                  align="left"
                  title="Model"
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
                <span>{showAdvanced ? '▼' : '▶'}</span> Advanced settings
              </button>
              {showAdvanced && (
                <div className="mt-3 flex flex-col gap-4">
                  <div className="flex flex-col gap-1 max-w-xs">
                    <label className="cw-label">Reasoning effort</label>
                    <div className="border border-line rounded-sm px-2 py-1.5 bg-paper inline-flex items-center">
                      <Dropdown
                        value={harness.reasoning_effort}
                        options={EFFORT_OPTIONS}
                        onChange={(e) => patchHarness({ reasoning_effort: e })}
                        align="left"
                        title="Reasoning effort"
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
                        placeholder="(default)"
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
                        placeholder="(default)"
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
                        placeholder="(default)"
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
            title="Skills"
            hint="These skills are always enabled when the app runs."
          >
            {skills.length === 0 ? (
              <span className="text-[12px] text-dim italic font-mono">
                No skills available
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
            title="MCP servers (forward-compatible, recorded only)"
            hint="v1 only stores this without filtering; at run time servers are still injected tenant-wide."
          >
            {mcpServers.length === 0 ? (
              <span className="text-[12px] text-dim italic font-mono">
                No MCP servers available
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

          {/* ── App info (immediate effect, unlike the version below) ── */}
          {step === 1 && (
            <Section
              title="App info"
              hint="Name, icon and description take effect as soon as you save. Input fields and prompts below are version content and take effect once published."
            >
              <div className="flex gap-3 items-end">
                <div className="flex flex-col gap-1">
                  <label className="cw-label">Icon</label>
                  <EmojiPicker
                    value={meta.icon}
                    onChange={(icon) => setMeta((m) => ({ ...m, icon }))}
                  />
                </div>
                <div className="flex-1 flex flex-col gap-1 min-w-0">
                  <label className="cw-label">Name *</label>
                  <input
                    className={metaError ? appInputCls.replace('border-line', 'border-accent') : appInputCls}
                    value={meta.name}
                    onChange={(e) => {
                      setMeta((m) => ({ ...m, name: e.target.value }));
                      if (metaError) setMetaError(null);
                    }}
                    placeholder="Contract review assistant"
                  />
                  {metaError && <span className="text-[10.5px] text-accent">{metaError}</span>}
                </div>
              </div>

              <div className="flex flex-col gap-1">
                <label className="cw-label">Description</label>
                <textarea
                  className={`${appInputCls} min-h-[60px] resize-y`}
                  value={meta.description}
                  onChange={(e) => setMeta((m) => ({ ...m, description: e.target.value }))}
                  placeholder="One line on what this app does"
                />
              </div>

              <div className="flex flex-col gap-1">
                <label className="cw-label">Visibility</label>
                <VisibilitySelect
                  value={meta.visibility}
                  onChange={(visibility) => setMeta((m) => ({ ...m, visibility }))}
                />
              </div>
            </Section>
          )}

          {/* ── Input schema builder ── */}
          {step === 1 && (
            <Section
              title="Input form fields"
              hint="Define the fields users fill in when running this app. Reference them in prompts with /field_key."
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
              Cancel
            </button>
            <button
              onClick={handleSaveDraft}
              className="cw-btn cw-btn-sm"
              disabled={saving}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
            {step === 1 ? (
              <button
                onClick={handleNextStep}
                className="cw-btn cw-btn-primary cw-btn-sm"
                disabled={saving}
              >
                Next
              </button>
            ) : (
              <>
                <button
                  onClick={() => setStep(1)}
                  className="cw-btn cw-btn-ghost cw-btn-sm"
                  disabled={saving}
                >
                  Back
                </button>
                <button
                  onClick={isAdmin ? handlePublish : handleSubmit}
                  className="cw-btn cw-btn-primary cw-btn-sm"
                  disabled={saving}
                >
                  {saving ? 'Publishing…' : 'Publish'}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
