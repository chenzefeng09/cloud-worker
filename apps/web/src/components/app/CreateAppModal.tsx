import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, ApiError } from '../../api/client';
import type { App, AppVisibility } from '../../api/types';
import {
  DEFAULT_MODEL,
  DEFAULT_EFFORT,
} from '../../lib/modelOptions';
import { EmojiPicker, VisibilitySelect, appInputCls as inputCls } from './AppMetaFields';
import { TagInput } from './TagInput';

// ─── Modal ────────────────────────────────────────────────────────────────────

interface CreateAppModalProps {
  open: boolean;
  onClose: () => void;
  onCreated?: (app: App) => void;
}

export function CreateAppModal({ open, onClose, onCreated }: CreateAppModalProps) {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [icon, setIcon] = useState('');
  const [description, setDescription] = useState('');
  const [visibility, setVisibility] = useState<AppVisibility>('private');
  const [tags, setTags] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Reset on open
  useEffect(() => {
    if (open) {
      setName('');
      setIcon('');
      setDescription('');
      setVisibility('private');
      setTags([]);
      setError(null);
    }
  }, [open]);

  // Esc to close
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !saving) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, saving, onClose]);

  const handleCreate = async () => {
    if (!name.trim()) {
      setError('Enter an app name');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const app = await api.apps.create({
        name: name.trim(),
        description: description.trim() || null,
        icon: icon.trim() || null,
        visibility,
        tags,
        version: {
          version: '1',
          system_prompt: '',
          user_prompt_template: '',
          model: DEFAULT_MODEL,
          reasoning_effort: DEFAULT_EFFORT,
          sampling_params: {},
          forced_skills: [],
          forced_mcp_server_ids: [],
          input_schema: [],
        },
      });
      onCreated?.(app);
      onClose();
      navigate(`/apps/${app.id}/edit`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Failed to create app');
    } finally {
      setSaving(false);
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={() => { if (!saving) onClose(); }}
    >
      <div
        className="w-full max-w-[480px] border border-line bg-surface rounded-md shadow-lg"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-line">
          <div className="text-[13.5px] font-medium">Create app</div>
          <button
            className="font-mono text-[12px] text-dim hover:text-ink transition-colors disabled:opacity-40"
            onClick={onClose}
            disabled={saving}
          >
            ✕
          </button>
        </div>

        {/* Body */}
        <div className="flex flex-col gap-4 px-4 py-4">
          {error && (
            <div className="border border-accent-line bg-accent-soft rounded-md px-3 py-2 text-[13px] text-accent">
              {error}
            </div>
          )}

          {/* Name + Icon */}
          <div className="flex gap-3 items-end">
            <div className="flex flex-col gap-1">
              <label className="cw-label">Icon</label>
              <EmojiPicker value={icon} onChange={setIcon} />
            </div>
            <div className="flex-1 flex flex-col gap-1">
              <label className="cw-label">Name *</label>
              <input
                className={inputCls}
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Contract review assistant"
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !saving) handleCreate();
                }}
              />
            </div>
          </div>

          {/* Description */}
          <div className="flex flex-col gap-1">
            <label className="cw-label">Description</label>
            <textarea
              className={`${inputCls} min-h-[60px] resize-y`}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="One sentence on what this app does"
            />
          </div>

          {/* Visibility */}
          <div className="flex flex-col gap-1">
            <label className="cw-label">Visibility</label>
            <VisibilitySelect value={visibility} onChange={setVisibility} />
          </div>

          {/* Tags */}
          <div className="flex flex-col gap-1">
            <label className="cw-label">Tags</label>
            <TagInput value={tags} onChange={setTags} />
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-4 py-3 border-t border-line">
          <button className="cw-btn cw-btn-sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button
            className="cw-btn cw-btn-primary cw-btn-sm"
            onClick={handleCreate}
            disabled={saving || !name.trim()}
          >
            {saving ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  );
}
