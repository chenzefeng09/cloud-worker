/**
 * SchemaForm — generic controlled form driven by InputField[].
 *
 * Owns no state — all three value buckets are passed in and mutated through
 * onChange. The host (AppRunPage, future SchedulesPage, …) keeps the state
 * and handles file upload; SchemaForm renders the picker UI + existing-file
 * chips and delegates actual upload/remove to callbacks.
 */
import { useRef } from 'react';
import { Chip } from '../shell/Chip';
import { TextField } from './TextField';
import { TextArea } from './TextArea';
import { SelectField } from './SelectField';
import type { InputField, Attachment } from '../../api/types';

// ─── Public types ─────────────────────────────────────────────────────────────

export interface SchemaFormValue {
  /** text / textarea / select field values */
  text: Record<string, string>;
  /** multiselect field values */
  multi: Record<string, string[]>;
  /** file field values (uploaded attachments) */
  file: Record<string, Attachment[]>;
}

export interface SchemaFormProps {
  schema: InputField[];
  value: SchemaFormValue;
  onChange: (next: SchemaFormValue) => void;
  /** Per field.key validation error messages (displayed beneath the field). */
  errors?: Record<string, string>;
  /**
   * When true: file fields are hidden and replaced with a hint text.
   * Use for contexts where file upload is not supported (e.g. scheduled runs).
   */
  disabledFileUpload?: boolean;
  /**
   * Called when the user picks files for a file-type field.
   * The host is responsible for uploading and updating value.file[field.key].
   * If omitted, file fields are rendered in a read-only / disabled state.
   */
  onFilePick?: (field: InputField, files: File[]) => void;
  /**
   * Called when the user removes an already-uploaded attachment chip.
   * If omitted, remove buttons are hidden.
   */
  onFileRemove?: (field: InputField, attachmentId: string) => void;
  /**
   * Key of the field currently uploading (shows uploading spinner).
   * Provided by the host that manages upload state.
   */
  uploadingKey?: string | null;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function humanSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

const isImage = (mime: string) => mime.startsWith('image/');

// ─── Component ────────────────────────────────────────────────────────────────

export function SchemaForm({
  schema,
  value,
  onChange,
  errors = {},
  disabledFileUpload = false,
  onFilePick,
  onFileRemove,
  uploadingKey = null,
}: SchemaFormProps) {
  // One hidden <input type=file> per field key — same pattern as AppRunPage.
  const fileInputsRef = useRef<Record<string, HTMLInputElement | null>>({});

  const setTextField = (key: string, val: string) =>
    onChange({ ...value, text: { ...value.text, [key]: val } });

  const toggleMulti = (key: string, option: string) => {
    const cur = value.multi[key] ?? [];
    const next = cur.includes(option) ? cur.filter((v) => v !== option) : [...cur, option];
    onChange({ ...value, multi: { ...value.multi, [key]: next } });
  };

  if (schema.length === 0) {
    return (
      <div className="font-mono italic text-dim text-[12px]">
        This app needs no input — just run it.
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {schema.map((field) => {
        const err = errors[field.key] || undefined;

        // ── file (hidden or rendered) ──────────────────────────────────────
        if (field.type === 'file') {
          if (disabledFileUpload) {
            return (
              <div key={field.key} className="flex flex-col gap-1.5">
                <div className="text-[13px] font-medium text-ink">
                  {field.label}
                  {field.required && <span className="text-accent ml-1">*</span>}
                </div>
                {field.help && (
                  <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{field.help}</div>
                )}
                <div className="font-mono text-[11px] text-dim italic">
                  Scheduled triggers don’t support file inputs
                </div>
                {err && <div className="text-[11.5px] text-accent">{err}</div>}
              </div>
            );
          }

          const existingFiles = value.file[field.key] ?? [];
          const isUploading = uploadingKey === field.key;

          return (
            <div key={field.key} className="flex flex-col gap-1.5">
              <label className="text-[13px] font-medium text-ink">
                {field.label}
                {field.required && <span className="text-accent ml-1">*</span>}
              </label>
              {field.help && (
                <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{field.help}</div>
              )}
              <div className="flex flex-col gap-1.5">
                {/* Hidden native file input */}
                {onFilePick && (
                  <input
                    ref={(el) => {
                      fileInputsRef.current[field.key] = el;
                    }}
                    type="file"
                    multiple={!!field.file?.multiple}
                    accept={(field.file?.accept ?? []).join(',')}
                    className="hidden"
                    onChange={(e) => {
                      if (e.target.files) {
                        onFilePick(field, Array.from(e.target.files));
                      }
                    }}
                    disabled={isUploading}
                  />
                )}
                <div className="flex items-center gap-2">
                  {onFilePick && (
                    <button
                      type="button"
                      onClick={() => fileInputsRef.current[field.key]?.click()}
                      disabled={isUploading}
                      className="cw-btn cw-btn-ghost cw-btn-sm disabled:opacity-50"
                    >
                      <span className="font-mono">⊕</span> Choose file
                    </button>
                  )}
                  {isUploading && (
                    <span className="font-mono text-[11px] text-dim animate-pulse">
                      Uploading…
                    </span>
                  )}
                  {field.file?.max_size_bytes && (
                    <span className="font-mono text-[10.5px] text-dim">
                      ≤ {humanSize(field.file.max_size_bytes)}
                    </span>
                  )}
                  {typeof field.default === 'string' && field.default && (
                    <span className="font-mono text-[10.5px] text-dim truncate">
                      Default: {field.default}
                    </span>
                  )}
                </div>
                {existingFiles.length > 0 && (
                  <div className="flex flex-wrap gap-1.5">
                    {existingFiles.map((att) => (
                      <div
                        key={att.id}
                        className="flex items-center gap-1 px-2 py-1 border border-line rounded-md text-[11px] bg-surface"
                      >
                        <span className="text-ink-2">{isImage(att.mime) ? '🖼' : '📎'}</span>
                        <span className="max-w-[160px] truncate text-ink">{att.filename}</span>
                        {onFileRemove && (
                          <button
                            type="button"
                            onClick={() => onFileRemove(field, att.id)}
                            className="font-mono text-dim hover:text-ink ml-0.5 leading-none"
                            aria-label="Remove attachment"
                          >
                            ×
                          </button>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
              {err && <div className="text-[11.5px] text-accent">{err}</div>}
            </div>
          );
        }

        // ── text ──────────────────────────────────────────────────────────
        if (field.type === 'text') {
          return (
            <TextField
              key={field.key}
              label={field.label}
              help={field.help}
              required={field.required}
              error={err}
              value={value.text[field.key] ?? ''}
              onChange={(v) => setTextField(field.key, v)}
              size="md"
            />
          );
        }

        // ── textarea ──────────────────────────────────────────────────────
        if (field.type === 'textarea') {
          return (
            <TextArea
              key={field.key}
              label={field.label}
              help={field.help}
              required={field.required}
              error={err}
              value={value.text[field.key] ?? ''}
              onChange={(v) => setTextField(field.key, v)}
              size="md"
            />
          );
        }

        // ── select ────────────────────────────────────────────────────────
        if (field.type === 'select') {
          return (
            <SelectField
              key={field.key}
              label={field.label}
              help={field.help}
              required={field.required}
              error={err}
              value={value.text[field.key] ?? ''}
              onChange={(v) => setTextField(field.key, v)}
              options={(field.options ?? []).map((o) => ({ value: o.value, label: o.label }))}
              title={field.label}
              size="md"
            />
          );
        }

        // ── multiselect ───────────────────────────────────────────────────
        if (field.type === 'multiselect') {
          return (
            <div key={field.key} className="flex flex-col gap-1.5">
              <label className="text-[13px] font-medium text-ink">
                {field.label}
                {field.required && <span className="text-accent ml-1">*</span>}
              </label>
              {field.help && (
                <div className="text-[11.5px] text-dim leading-[1.45] -mt-0.5">{field.help}</div>
              )}
              <div className="flex flex-wrap gap-1.5">
                {(field.options ?? []).map((o) => {
                  const on = (value.multi[field.key] ?? []).includes(o.value);
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
              {err && <div className="text-[11.5px] text-accent">{err}</div>}
            </div>
          );
        }

        // Unreachable — exhaustive by InputFieldType, but TypeScript guard:
        return null;
      })}
    </div>
  );
}
