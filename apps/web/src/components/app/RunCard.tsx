import { Chip } from '../shell/Chip';
import { StatusDot, type DesignStatus } from '../shell/StatusDot';
import { formatAbsoluteTimeZh, formatRelativeTimeZh } from '../../lib/format';
import { modelLabel } from '../../lib/modelOptions';
import type { AppRunHistoryItem, RunStatus } from '../../api/types';

// ─── Shared App-run row ───────────────────────────────────────────────────────
//
// One row of App execution history. Lives here rather than inside
// AppHistoryPage because the App detail page shows the same rows for its
// "recent runs" preview — the two surfaces must stay pixel-identical.

export const RUN_TERMINAL = new Set<RunStatus>(['COMPLETED', 'FAILED', 'CANCELLED']);

const INPUT_VALUE_MAX = 80;
const RESULT_SUMMARY_MAX = 150;

export function runToDesignStatus(status: RunStatus): DesignStatus {
  switch (status) {
    case 'PENDING':
      return 'queued';
    case 'RUNNING':
      return 'thinking';
    case 'PAUSED':
      return 'paused';
    case 'COMPLETED':
      return 'done';
    case 'FAILED':
      return 'awaiting';
    case 'CANCELLED':
      return 'idle';
    default:
      return 'idle';
  }
}

export function runStatusZh(status: RunStatus): string {
  switch (status) {
    case 'PENDING':
      return '排队中';
    case 'RUNNING':
      return '运行中';
    case 'PAUSED':
      return '待确认';
    case 'COMPLETED':
      return '已完成';
    case 'FAILED':
      return '已失败';
    case 'CANCELLED':
      return '已取消';
    default:
      return status;
  }
}

/** Render one snapshot input value as a readable string (mirrors AppResultPage). */
export function renderInputValue(value: unknown): string {
  if (value == null) return '-';
  if (Array.isArray(value)) {
    // multiselect -> strings; file -> [{filename,...}]
    return value
      .map((v) => {
        if (v && typeof v === 'object' && 'filename' in (v as Record<string, unknown>)) {
          return String((v as { filename: unknown }).filename);
        }
        return String(v);
      })
      .join('、');
  }
  return String(value);
}

export function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
}

/** One run card. Click navigates to the AppResultPage for that run. */
export function RunCard({
  run,
  onOpen,
}: {
  run: AppRunHistoryItem;
  onOpen: (run: AppRunHistoryItem) => void;
}) {
  const status = run.status;
  const isSchedule = run.source === 'schedule';
  const otherSource =
    run.source && run.source !== 'user' && run.source !== 'schedule' ? run.source : null;
  const hasInputs = run.inputs.length > 0;

  return (
    <div
      onClick={() => onOpen(run)}
      className="border border-line bg-surface rounded-md p-3.5 cursor-pointer flex flex-col gap-2 transition-all hover:border-ink-2 hover:-translate-y-px"
    >
      {/* Row 1: status + source + time */}
      <div className="flex items-center gap-2 flex-wrap">
        <StatusDot status={runToDesignStatus(status)} label={runStatusZh(status)} />
        {isSchedule && <Chip dashed>定时</Chip>}
        {otherSource && <Chip dashed>{otherSource}</Chip>}
        {run.execution_mode === 'inline' && <Chip>对话模式</Chip>}
        <span className="flex-1" />
        <span
          className="font-mono text-[10.5px] text-dim"
          title={formatAbsoluteTimeZh(run.created_at)}
        >
          {formatRelativeTimeZh(run.created_at)}
        </span>
      </div>

      {/* Row 2: app context chips */}
      {run.app && (
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="font-mono text-[11.5px] text-ink-2 truncate max-w-[220px]" title={run.app.app_name}>
            {run.app.app_name || '-'}
          </span>
          <Chip>v{run.app.app_version}</Chip>
          <Chip>模型 · {modelLabel(run.app.model)}</Chip>
        </div>
      )}

      {/* Row 3: inputs summary */}
      {hasInputs && (
        <div className="flex flex-col gap-0.5 border-t border-line-2 pt-2">
          {run.inputs.map((inp) => {
            const raw = renderInputValue(inp.value);
            if (!raw || raw === '-') return null;
            return (
              <div key={inp.key} className="grid grid-cols-[110px_1fr] gap-2 items-start">
                <span className="text-[11.5px] text-dim truncate" title={inp.label}>
                  {inp.label}
                </span>
                <span className="text-[12px] text-ink-2 leading-[1.45] break-words whitespace-pre-wrap">
                  {truncate(raw, INPUT_VALUE_MAX)}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Row 4: result summary preview (finished runs only) */}
      {run.result_summary && RUN_TERMINAL.has(status) && (
        <div className="border-t border-line-2 pt-2 text-[12.5px] text-ink leading-[1.5] whitespace-pre-wrap break-words">
          {truncate(run.result_summary, RESULT_SUMMARY_MAX)}
        </div>
      )}

      {/* Row 5: error message (failed runs only) */}
      {status === 'FAILED' && run.error_message && (
        <div className="border-t border-line-2 pt-2 px-2.5 py-1.5 bg-accent-soft border border-accent-line rounded-sm text-[12px] text-accent leading-[1.45] whitespace-pre-wrap break-words">
          {truncate(run.error_message, RESULT_SUMMARY_MAX)}
        </div>
      )}
    </div>
  );
}
