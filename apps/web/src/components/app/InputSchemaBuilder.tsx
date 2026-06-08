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

/** Field key: lowercase letter then up to 39 [a-z0-9_]. Backend re-validates. */
export const KEY_RE = /^[a-z][a-z0-9_]{0,39}$/;

const DEFAULT_MAX_SIZE = 10 * 1024 * 1024; // 10 MiB

/** A blank field ready to append. */
export function blankField(): InputField {
  return {
    key: '',
    type: 'text',
    label: '',
    help: '',
    required: false,
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
  errors: Record<number, string>;
  ok: boolean;
} {
  const errors: Record<number, string> = {};
  const seen = new Map<string, number>();
  fields.forEach((f, i) => {
    if (!f.key.trim()) {
      errors[i] = '字段标识不能为空';
    } else if (!KEY_RE.test(f.key)) {
      errors[i] = '标识须以小写字母开头，仅含小写字母/数字/下划线（≤40 字符）';
    } else if (seen.has(f.key)) {
      errors[i] = `标识 "${f.key}" 重复`;
    } else {
      seen.set(f.key, i);
    }
    if (errors[i]) return;
    if (!f.label.trim()) {
      errors[i] = '显示名称不能为空';
      return;
    }
    if (f.type === 'select' || f.type === 'multiselect') {
      const opts = f.options ?? [];
      if (opts.length === 0) {
        errors[i] = '单选/多选至少需要一个选项';
        return;
      }
      if (opts.some((o) => !o.value.trim())) {
        errors[i] = '选项的值不能为空';
        return;
      }
      const allowed = new Set(opts.map((o) => o.value));
      if (f.type === 'select' && typeof f.default === 'string' && f.default && !allowed.has(f.default)) {
        errors[i] = '默认值需匹配某个选项 value';
        return;
      }
      if (f.type === 'multiselect' && Array.isArray(f.default)) {
        const bad = f.default.filter((v) => !allowed.has(String(v)));
        if (bad.length > 0) {
          errors[i] = '默认选项需匹配已定义的选项 value';
          return;
        }
      }
    }
    if (f.type === 'file') {
      const accept = f.file?.accept ?? [];
      if (accept.length === 0) {
        errors[i] = '文件类型需指定至少一个 accept（如 application/pdf）';
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
        <span className="cw-label">允许的类型（逗号分隔）</span>
        <input
          className="border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent"
          value={file.accept.join(', ')}
          onChange={(e) =>
            setFile({
              accept: e.target.value
                .split(',')
                .map((s) => s.trim())
                .filter(Boolean),
            })
          }
          placeholder="application/pdf, image/png"
        />
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
      <div className="flex flex-col gap-1">
        <span className="cw-label">默认文件路径（可选）</span>
        <input
          className="border border-line rounded-sm px-2 py-1 text-[12px] bg-paper focus:outline-none focus:border-accent"
          value={typeof field.default === 'string' ? field.default : ''}
          onChange={(e) => onChange({ default: e.target.value || null })}
          placeholder="/workspace/inputs/reference.pdf"
        />
        <span className="text-[10.5px] text-dim">
          未上传文件时使用该路径；运行沙盒必须能访问此文件。
        </span>
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
  error?: string;
  onChange: (patch: Partial<InputField>) => void;
  onRemove: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const isChoice = field.type === 'select' || field.type === 'multiselect';

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
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] font-mono bg-paper focus:outline-none focus:border-accent"
            value={field.key}
            onChange={(e) => onChange({ key: e.target.value })}
            placeholder="contract"
          />
        </div>
        <div className="flex flex-col gap-1">
          <span className="cw-label">类型</span>
          <div className="border border-line rounded-sm px-2 py-1.5 bg-paper flex items-center">
            <Dropdown
              value={field.type}
              options={TYPE_OPTIONS}
              onChange={(id) => changeType(id as InputFieldType)}
              align="left"
              title="字段类型"
            />
          </div>
        </div>
        <div className="flex flex-col gap-1">
          <span className="cw-label">显示名称</span>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent"
            value={field.label}
            onChange={(e) => onChange({ label: e.target.value })}
            placeholder="合同正文"
          />
        </div>
      </div>

      {/* help + required */}
      <div className="flex items-end gap-3">
        <div className="flex flex-col gap-1 flex-1">
          <span className="cw-label">帮助说明（可选）</span>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent"
            value={field.help ?? ''}
            onChange={(e) => onChange({ help: e.target.value })}
            placeholder="提示用户如何填写"
          />
        </div>
        <label className="flex items-center gap-1.5 text-[12px] text-ink-2 pb-1.5 cursor-pointer whitespace-nowrap">
          <input
            type="checkbox"
            checked={field.required}
            onChange={(e) => onChange({ required: e.target.checked })}
          />
          必填
        </label>
      </div>

      {/* default (text/textarea/select only) */}
      {(field.type === 'text' ||
        field.type === 'textarea' ||
        field.type === 'select') && (
        <div className="flex flex-col gap-1">
          <span className="cw-label">默认值（可选）</span>
          <input
            className="border border-line rounded-sm px-2 py-1.5 text-[12px] bg-paper focus:outline-none focus:border-accent"
            value={typeof field.default === 'string' ? field.default : ''}
            onChange={(e) => onChange({ default: e.target.value })}
            placeholder={field.type === 'select' ? '需匹配某个选项的 value' : ''}
          />
        </div>
      )}

      {isChoice && (
        <>
          <OptionsEditor
            options={field.options ?? []}
            onChange={(options) => onChange({ options })}
          />
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
            </div>
          )}
        </>
      )}

      {field.type === 'file' && (
        <FileConstraintEditor field={field} onChange={onChange} />
      )}

      {error && (
        <div className="text-[12px] text-accent bg-accent-soft px-2 py-1 rounded-sm">
          {error}
        </div>
      )}
    </div>
  );
}

// ─── Builder ──────────────────────────────────────────────────────────────────

interface Props {
  fields: InputField[];
  onChange: (fields: InputField[]) => void;
  /** Per-index validation errors, surfaced from the parent on submit. */
  errors?: Record<number, string>;
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
