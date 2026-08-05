// Model + reasoning-effort options surfaced in the composer dropdowns.
// The backend routes through an OpenAI-compatible gateway to DeepSeek V4.
// DeepSeek "effort" requires the gateway to send reasoning_effort + a thinking
// flag; "off" sends neither (non-thinking mode).

// Re-export ModelPurpose so consumers can import from either location.
export type { ModelPurpose } from '../api/types';

export interface ModelOption {
  /** Model id sent to the backend (gateway routing key). */
  id: string;
  /** Short label shown in the dropdown. */
  label: string;
}

export const MODEL_OPTIONS: ModelOption[] = [
  { id: 'deepseek-v4-pro', label: 'V4 Pro' },
  { id: 'deepseek-v4-flash', label: 'V4 Flash' },
];

/**
 * Hard-coded ultimate fallback used when neither user preferences nor
 * tenant defaults resolve a model.  Renamed from the original
 * `DEFAULT_MODEL` — always prefer `useDefaultModel()` in new code.
 */
export const SYSTEM_FALLBACK_MODEL = 'deepseek-v4-flash';

/**
 * @deprecated Use `SYSTEM_FALLBACK_MODEL` (same value) or, preferably,
 * `useDefaultModel()` which resolves via user pref → tenant default → fallback.
 */
export const DEFAULT_MODEL = SYSTEM_FALLBACK_MODEL;

export type EffortId = '' | 'low' | 'medium' | 'high';

export interface EffortOption {
  /** Value sent as reasoning_effort; '' means off (non-thinking). */
  id: EffortId;
  label: string;
}

export const EFFORT_OPTIONS: EffortOption[] = [
  { id: '', label: 'Off' },
  { id: 'low', label: 'Low' },
  { id: 'medium', label: 'Medium' },
  { id: 'high', label: 'High' },
];

export const DEFAULT_EFFORT: EffortId = 'medium';

export function modelLabel(id: string): string {
  return MODEL_OPTIONS.find((m) => m.id === id)?.label ?? id;
}

export function effortLabel(id: string): string {
  return EFFORT_OPTIONS.find((e) => e.id === id)?.label ?? id;
}
