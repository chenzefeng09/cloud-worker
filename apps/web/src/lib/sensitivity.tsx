import type { DlpSensitivity } from '../api/types';

export const SENSITIVITY_COLORS: Record<DlpSensitivity, string> = {
  L1: '#94a3b8',
  L2: '#38bdf8',
  L3: '#0ea5e9',
  L4: '#d97706',
  L5: '#dc2626',
};

export const SENSITIVITY_LABELS: Record<DlpSensitivity, string> = {
  L1: '公开',
  L2: '内部',
  L3: '敏感',
  L4: '机密',
  L5: '绝密',
};

export const SENSITIVITY_ORDER: DlpSensitivity[] = ['L1', 'L2', 'L3', 'L4', 'L5'];

export const DEFAULT_SENSITIVITY: DlpSensitivity = 'L2';

/**
 * Numeric rank for clearance gating: L1=1 ... L5=5. `undefined` maps to 0 so a
 * missing level never satisfies a clearance check above L0.
 */
export function sensitivityRank(level: DlpSensitivity | undefined): number {
  if (!level) return 0;
  const idx = SENSITIVITY_ORDER.indexOf(level);
  return idx === -1 ? 0 : idx + 1;
}

export interface LevelBadgeProps {
  level?: DlpSensitivity;
  small?: boolean;
}

/**
 * Coloured sensitivity pill (mono font, white text on the level's accent
 * colour). Renders a muted "-" when `level` is undefined so callers can render
 * badge-shaped UI before the field is on the wire. `small` tightens padding
 * and text for dense rows.
 */
export function LevelBadge({ level, small }: LevelBadgeProps) {
  const pad = small ? 'px-1 py-0.5' : 'px-1.5 py-0.5';
  const size = small ? 'text-[9.5px]' : 'text-[10.5px]';
  if (!level) {
    return (
      <span className={`inline-flex items-center ${pad} rounded-sm font-mono ${size} text-dim`}>
        -
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center ${pad} rounded-sm font-mono ${size} text-white`}
      style={{ background: SENSITIVITY_COLORS[level] }}
    >
      {level} {SENSITIVITY_LABELS[level]}
    </span>
  );
}
