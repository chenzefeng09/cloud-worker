import { useState, type KeyboardEvent } from 'react';

interface TagInputProps {
  /** Current tag list (controlled). */
  value: string[];
  /** Called with the next tag list on any add/remove. */
  onChange: (tags: string[]) => void;
  placeholder?: string;
  /** Max number of tags allowed (default 20). */
  max?: number;
  /** Max characters per tag (default 32). */
  maxLength?: number;
  className?: string;
}

/**
 * Free-form tag input. Type and press Enter or comma to add a tag; click ✕ on
 * a chip (or Backspace on an empty input) to remove it. Tags are trimmed,
 * deduplicated case-insensitively, and capped at `max` entries / `maxLength`
 * chars each. Styled to match AppMetaFields' appInputCls + the cw-chip token.
 */
export function TagInput({
  value,
  onChange,
  placeholder = '输入后回车或逗号添加…',
  max = 20,
  maxLength = 32,
  className,
}: TagInputProps) {
  const [draft, setDraft] = useState('');

  const commit = () => {
    const raw = draft.trim();
    if (!raw) return;
    if (value.length >= max) return; // at cap; keep draft so the user sees it
    const tag = raw.slice(0, maxLength);
    const lower = tag.toLowerCase();
    if (value.some((t) => t.toLowerCase() === lower)) {
      setDraft('');
      return;
    }
    onChange([...value, tag]);
    setDraft('');
  };

  const remove = (tag: string) => {
    onChange(value.filter((t) => t !== tag));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      commit();
    } else if (e.key === 'Backspace' && draft === '' && value.length > 0) {
      onChange(value.slice(0, -1));
    }
  };

  return (
    <div
      className={`flex items-center gap-1 flex-wrap border border-line rounded-sm px-1.5 py-1 bg-paper focus-within:border-accent transition-colors ${
        className ?? ''
      }`}
    >
      {value.map((tag) => (
        <span key={tag} className="cw-chip">
          #{tag}
          <button
            type="button"
            onClick={() => remove(tag)}
            className="text-dim hover:text-accent transition-colors leading-none"
            aria-label={`移除标签 ${tag}`}
          >
            ✕
          </button>
        </span>
      ))}
      <input
        type="text"
        className="flex-1 min-w-[100px] bg-transparent border-none focus:outline-none text-[13px] py-0.5"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={value.length === 0 ? placeholder : ''}
      />
    </div>
  );
}
