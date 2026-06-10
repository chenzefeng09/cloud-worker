import { useState } from 'react';
import type {
  InputField,
  InputFieldOption,
  InputFieldType,
} from '../../api/types';
import { Dropdown } from '../shell/Dropdown';

// ─── Constants ──────────────────────────────────────────────────────────────

const TYPE_OPTIONS: { id: InputFieldType; label: string }[] = [
  { id: 'text', label: '单行文本' },
  { id: 'textarea', label: '多行文本' },
  { id: 'select', label: '单选' },
  { id: 'multiselect', label: '多选' },
  { id: 'file', label: '文件上传' },
];

/**
 * Field key: letters, digits, underscore, hyphen. Must start with a letter.
 * Backend re-validates.
 */
export const KEY_RE = /^[a-zA-Z][a-zA-Z0-9_-]{0,39}$/;

const DEFAULT_MAX_SIZE = 10 * 1024 * 1024; // 10 MiB

/**
 * File-type categories shown as checkboxes.
 * Each category maps to a set of common MIME types sent to the backend.
 */
const FILE_CATEGORIES: { id: string; label: string; mimes: string[] }[] = [
  {
    id: 'doc',
    label: '文档',
    mimes: [
      'application/pdf',
      'application/msword',
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
      'application/vnd.ms-excel',
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'application/vnd.ms-powerpoint',
      'application/vnd.openxmlformats-officedocument.presentationml.presentation',
      'text/plain',
      'text/csv',
      'text/markdown',
    ],
  },
  {
    id: 'image',
    label: '图片',
    mimes: ['image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml'],
  },
  {
    id: 'audio',
    label: '音频',
    mimes: ['audio/mpeg', 'audio/wav', 'audio/ogg', 'audio/webm'],
  },
  {
    id: 'video',
    label: '视频',
    mimes: ['video/mp4', 'video/webm', 'video/ogg'],
  },
  {
    id: 'archive',
    label: '压缩包',
    mimes: [
      'application/zip',
      'application/gzip',
      'application/x-tar',
      'application/x-7z-compressed',
      'application/x-rar-compressed',
    ],
  },
  {
    id: 'code',
    label: '代码',
    mimes: [
      'text/html',
      'text/css',
      'text/javascript',
      'application/javascript',
      'application/json',
      'application/xml',
      'text/xml',
    ],
  },
];

/** Check if a category is "selected" by seeing if all its mimes are in accept. */
function isCategorySelected(accept: string[], cat: typeof FILE_CATEGORIES[number]): boolean {
  return cat.mimes.every((m) => accept.includes(m));
}

/** Toggle a category on/off in the accept list. */
function toggleCategory(accept: string[], cat: typeof FILE_CATEGORIES[number]): string[] {
  if (isCategorySelected(accept, cat)) {
    // remove all mimes from this category
    const removeSet = new Set(cat.mimes);
    return accept.filter((m) => !removeSet.has(m));
  }
  // add all mimes from this category (dedup)
  const existing = new Set(accept);
  return [...accept, ...cat.mimes.filter((m) => !existing.has(m))];
}

/** A blank field ready to append — required is true by default. */
export function blankField(): InputField {
  return {
    key: '',
    type: 'text',
    label: '',
    help: '',
    required: true,
    default: '',
  };
}

// ─── Validation ───────────────────────────────────────────────────────────────

/**
 * Validate the whole schema. Returns a per-index map of error strings (only
 * indices with problems are present) plus a top-level message when the schema
 * is structurally unusable. Mirrors the backend rules:
 *   - key matches KEY_RE and is unique
 *   - (multi)select must have ≥1 option
 *   - file must declare at least one accept entry
 */
export function validateSchema(fields: InputField[]): {
  errors: Record<number, { path: string; msg: string }>;
  ok: boolean;
} {
  const errors: Record<number, { path: string; msg: string }> = {};
  const seen = new Map<string, number>();
  fields.forEach((f, i) => {
    if (!f.key.trim()) {
      errors[i] = { path: 'key', msg: '字段标识不能为空' };
    } else if (!KEY_RE.test(f.key)) {
      errors[i] = { path: 'key', msg: '标识须以字母开头，仅含字母、数字、下划线、中划线（≤40 字符）' };
    } else if (seen.has(f.key)) {
      errors[i] = { path: 'key', msg: `标识 "${f.key}" 重复` };
    } else {
      seen.set(f.key, i);
    }
    if (errors[i]) return;
    if (!f.label.trim()) {
      errors[i] = { path: 'label', msg: '显示名称不能为空' };
      return;
    }
    if (f.type === 'select' || f.type === 'multiselect') {
      const opts = f.options ?? [];
      if (opts.length === 0) {
        errors[i] = { path: 'options', msg: '单选/多选至少需要一个选项' };
        return;
      }
      if (opts.some((o) => !o.value.trim())) {
        errors[i] = { path: 'options', msg: '选项的值不能为空' };
        return;
      }
      const allowed = new Set(opts.map((o) => o.value));
      if (f.type === 'select' && typeof f.default === 'string' && f.default && !allowed.has(f.default)) {
        errors[i] = { path: 'default', msg: '默认值需匹配某个选项 value' };
        return;
      }
      if (f.type === 'multiselect' && Array.isArray(f.default)) {
        const bad = f.default.filter((v) => !allowed.has(String(v)));
        if (bad.length > 0) {
          errors[i] = { path: 'default', msg: '默认选项需匹配已定义的选项 value' };
          return;
        }
      }
    }
    if (f.type === 'file') {
      const accept = f.file?.accept ?? [];
      if (accept.length === 0) {
        errors[i] = { path: 'file', msg: '请至少选择一种文件类型' };
        return;
      }
    }
  });
  return { errors, ok: Object.keys(errors).length === 0 };
}

// ─── Sub-editors ──────────────────────────────────────────────────────────────

function OptionsEditor({
  options,
  onChange,
}: {
  options: InputFieldOption[];
  onChange: (o: InputFieldOption[]) => void;
}) {
  const update = (i: number, patch: Partial<InputFieldOption>) =>
    onChange(options.map((o, idx) => (idx === i ? { ...o, ...patch } : o)));
  const remove = (i: number) => onChange(options.filter((_, idx) => idx !== i));
  const add = () => onChange([...options, { value: '', label: '' }]);

  return (
    <div className="flex flex-col gap-1.5">
      <span className="cw-label">选项</span>
      {options.length === 0 && (
        <span className="text-[11px] text-dim italic font-mono">暂无选项</span>
      )}
      {options.map((o, i) => (
        <div key={i} className="flex items-center gap-1.5">
          <input
            className="border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent w-[40%]"
            value={o.value}
            onChange={(e) => update(i, { value: e.target.value })}
            placeholder="value"
          />
          <input
            className="border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent flex-1"
            value={o.label}
            onChange={(e) => update(i, { label: e.target.value })}
            placeholder="显示文本"
          />
          <button
            type="button"
            onClick={() => remove(i)}
            className="font-mono text-dim hover:text-accent text-[13px] leading-none px-1"
            aria-label="移除选项"
          >
            ×
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={add}
        className="cw-btn cw-btn-ghost cw-btn-sm self-start"
      >
        <span className="font-mono">＋</span> 添加选项
      </button>
    </div>
  );
}

function FileConstraintEditor({
  field,
  onChange,
}: {
  field: InputField;
  onChange: (patch: Partial<InputField>) => void;
}) {
  const file = field.file ?? {
    accept: [],
    max_size_bytes: DEFAULT_MAX_SIZE,
    multiple: false,
  };
  const setFile = (patch: Partial<typeof file>) =>
    onChange({ file: { ...file, ...patch } });

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-col gap-1">
        <span className="cw-label">允许的文件类型</span>
        <div className="flex flex-wrap gap-x-4 gap-y-1.5 px-1">
          {FILE_CATEGORIES.map((cat) => {
            const checked = isCategorySelected(file.accept, cat);
            return (
              <label
                key={cat.id}
                className="flex items-center gap-1.5 text-[12px] text-ink-2 cursor-pointer select-none"
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => setFile({ accept: toggleCategory(file.accept, cat) })}
                />
                {cat.label}
              </label>
            );
          })}
        </div>
      </div>
      <div className="flex items-end gap-3">
        <div className="flex flex-col gap-1">
          <span className="cw-label">最大大小 (MB)</span>
          <input
            type="number"
            min={0}
            step={0.5}
            className="border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent w-[120px]"
            value={(file.max_size_bytes / (1024 * 1024)).toString()}
            onChange={(e) => {
              const mb = parseFloat(e.target.value);
              setFile({
                max_size_bytes: Number.isFinite(mb)
                  ? Math.round(mb * 1024 * 1024)
                  : DEFAULT_MAX_SIZE,
              });
            }}
          />
        </div>
        <label className="flex items-center gap-1.5 text-[12px] text-ink-2 pb-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={file.multiple}
            onChange={(e) => setFile({ multiple: e.target.checked })}
          />
          允许多文件
        </label>
      </div>
    </div>
  );
}

// ─── Single field row ─────────────────────────────────────────────────────────

function FieldRow({
  field,
  index,
  total,
  error,
  onChange,
  onRemove,
  onMove,
}: {
  field: InputField;
  index: number;
  total: number;
  error?: { path: string; msg: string };
  onChange: (patch: Partial<InputField>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const isChoice = field.type === 'select' || field.type === 'multiselect';

  // Track whether user has manually edited the label
  const [labelTouched, setLabelTouched] = useState(false);

  // Live key validation state
  const [keyError, setKeyError] = useState<string | null>(null);

  // When switching type, drop now-irrelevant sub-config so we emit clean data.
  const changeType = (type: InputFieldType) => {
    const patch: Partial<InputField> = { type };
    if (type === 'select' || type === 'multiselect') {
      patch.options = field.options ?? [];
      patch.file = undefined;
      patch.default = type === 'multiselect' ? [] : '';
    } else if (type === 'file') {
      patch.file = field.file ?? {
        accept: [],
        max_size_bytes: DEFAULT_MAX_SIZE,
        multiple: false,
      };
      patch.options = undefined;
      patch.default = null;
    } else {
      patch.options = undefined;
      patch.file = undefined;
      patch.default = '';
    }
    onChange(patch);
  };

  /** Validate key on change — only allow valid characters */
  const handleKeyChange = (raw: string) => {
    // Strip invalid characters in real-time: allow letters, digits, underscore, hyphen
    const cleaned = raw.replace(/[^a-zA-Z0-9_-]/g, '');
    onChange({ key: cleaned });

    // Live validation feedback
    if (cleaned && !KEY_RE.test(cleaned)) {
      setKeyError('须以字母开头，仅含字母、数字、下划线、中划线');
    } else {
      setKeyError(null);
    }
  };

  /** Auto-fill label from key when key field loses focus */
  const handleKeyBlur = () => {
    if (field.key && !labelTouched && !field.label) {
      onChange({ label: field.key });
    }
  };

  const handleLabelChange = (val: string) => {
    setLabelTouched(true);
    onChange({ label: val });
  };

  return (
    <div className="border border-line rounded-md p-3 bg-surface flex flex-col gap-3">
      {/* Header line: index, move/remove */}
      <div className="flex items-center gap-2">
        <span className="font-mono text-[10.5px] text-dim">#{index + 1}</span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => onMove(-1)}
          disabled={index === 0}
          className="font-mono text-[12px] text-dim hover:text-ink disabled:opacity-30"
          title="上移"
        >
          ↑
        </button>
        <button
          type="button"
          onClick={() => onMove(1)}
          disabled={index === total - 1}
          className="font-mono text-[12px] text-dim hover:text-ink disabled:opacity-30"
          title="下移"
        >
          ↓
        </button>
        <button
          type="button"
          onClick={onRemove}
          className="font-mono text-[11px] text-dim hover:text-accent ml-1"
          title="删除字段"
        >
          删除
        </button>
      </div>

      {/* key + type + label */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="flex flex-col gap-1">
          <span className="cw-label">字段标识 (key)</span>
          <input
            className={`border rounded-sm px-2 py-1.5 text-[12px] font-mono bg-paper focus:outline-none ${
              keyError || error?.path === 'key' ? 'border-accent focus:border-accent' : 'border-line focus:border-accent'
            }`}
            value={field.key}
            onChange={(e) => handleKeyChange(e.target.value)}
            onBlur={handleKeyBlur}
            placeholder="contract_file"
          />
          {keyError && (
            <span className="text-[10.5px] text-accent">{keyError}</span>
          )}
          {!keyError && error?.path === 'key' && (
            <span className="text-[10.5px] text-accent">{error.msg}</span>
          )}
        </div>
        <div className="flex flex-col gap-1">
          <span className="cw-label">类型</span>
          <Dropdown
            value={field.type}
            options={TYPE_OPTIONS}
            onChange={(id) => changeType(id as InputFieldType)}
            align="left"
            title="字段类型"
            fullWidth
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="cw-label">显示名称</span>
          <input
            className={`border rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none ${
              error?.path === 'label' ? 'border-accent focus:border-accent' : 'border-line focus:border-accent'
            }`}
            value={field.label}
            onChange={(e) => handleLabelChange(e.target.value)}
            placeholder="合同正文"
          />
          {error?.path === 'label' && (
            <span className="text-[10.5px] text-accent">{error.msg}</span>
          )}
        </div>
      </div>

      {/* help */}
      <div className="flex flex-col gap-1">
        <span className="cw-label">帮助说明（可选）</span>
        <input
          className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent w-full"
          value={field.help ?? ''}
          onChange={(e) => onChange({ help: e.target.value })}
          placeholder="提示用户如何填写"
        />
      </div>

      {/* default (text/textarea/select only) */}
      {(field.type === 'text' ||
        field.type === 'textarea' ||
        field.type === 'select') && (
        <div className="flex flex-col gap-1">
          <span className="cw-label">默认值（可选）</span>
          <input
            className={`border rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none ${
              error?.path === 'default' ? 'border-accent focus:border-accent' : 'border-line focus:border-accent'
            }`}
            value={typeof field.default === 'string' ? field.default : ''}
            onChange={(e) => onChange({ default: e.target.value })}
            placeholder={field.type === 'select' ? '需匹配某个选项的 value' : ''}
          />
          {error?.path === 'default' && (
            <span className="text-[10.5px] text-accent">{error.msg}</span>
          )}
        </div>
      )}

      {isChoice && (
        <>
          <OptionsEditor
            options={field.options ?? []}
            onChange={(options) => onChange({ options })}
          />
          {error?.path === 'options' && (
            <div className="text-[10.5px] text-accent mt-[-4px]">{error.msg}</div>
          )}
          {field.type === 'multiselect' && (field.options ?? []).length > 0 && (
            <div className="flex flex-col gap-1">
              <span className="cw-label">默认选项（可选）</span>
              <div className="flex flex-wrap gap-1.5">
                {(field.options ?? []).map((o) => {
                  const cur = Array.isArray(field.default) ? field.default.map(String) : [];
                  const on = cur.includes(o.value);
                  return (
                    <button
                      key={o.value}
                      type="button"
                      onClick={() => {
                        const next = on
                          ? cur.filter((v) => v !== o.value)
                          : [...cur, o.value];
                        onChange({ default: next });
                      }}
                      className="cw-chip"
                      data-accent={on ? '1' : undefined}
                      data-dashed={!on ? '1' : undefined}
                    >
                      {o.label || o.value}
                    </button>
                  );
                })}
              </div>
              {error?.path === 'default' && (
                <div className="text-[10.5px] text-accent">{error.msg}</div>
              )}
            </div>
          )}
        </>
      )}

      {field.type === 'file' && (
        <div className="flex flex-col gap-1">
          <FileConstraintEditor field={field} onChange={onChange} />
          {error?.path === 'file' && (
            <div className="text-[10.5px] text-accent">{error.msg}</div>
          )}
        </div>
      )}

      <label className="flex items-center gap-1.5 text-[12px] text-ink-2 cursor-pointer select-none self-start">
        <input
          type="checkbox"
          checked={field.required}
          onChange={(e) => onChange({ required: e.target.checked })}
        />
        必填
      </label>
    </div>
  );
}

// ─── Builder ──────────────────────────────────────────────────────────────────

interface Props {
  fields: InputField[];
  onChange: (fields: InputField[]) => void;
  /** Per-index validation errors, surfaced from the parent on submit. */
  errors?: Record<number, { path: string; msg: string }>;
}

/**
 * Editable, ordered list of input-form fields. Produces an `InputField[]` that
 * conforms to the backend schema (the backend re-validates). Add / remove /
 * reorder controls plus per-type sub-config (options, file constraints).
 */
export function InputSchemaBuilder({ fields, onChange, errors = {} }: Props) {
  const update = (i: number, patch: Partial<InputField>) =>
    onChange(fields.map((f, idx) => (idx === i ? { ...f, ...patch } : f)));
  const remove = (i: number) => onChange(fields.filter((_, idx) => idx !== i));
  const add = () => onChange([...fields, blankField()]);
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= fields.length) return;
    const next = [...fields];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <div className="flex flex-col gap-3">
      {fields.length === 0 && (
        <div className="text-[12px] text-dim italic font-mono border border-dashed border-line rounded-md px-3 py-4 text-center">
          尚未定义任何输入字段。用户运行该应用时将看不到表单。
        </div>
      )}
      {fields.map((f, i) => (
        <FieldRow
          key={i}
          field={f}
          index={i}
          total={fields.length}
          error={errors[i]}
          onChange={(patch) => update(i, patch)}
          onRemove={() => remove(i)}
          onMove={(dir) => move(i, dir)}
        />
      ))}
      <button
        type="button"
        onClick={add}
        className="cw-btn cw-btn-ghost cw-btn-sm self-start"
      >
        <span className="font-mono">＋</span> 添加输入字段
      </button>
    </div>
  );
}
