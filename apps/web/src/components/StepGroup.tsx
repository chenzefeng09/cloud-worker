import { useState, useRef, useEffect } from 'react';
import type { StepGroupUnit } from '../lib/groupSteps';
import { ToolRow, ThinkingBlock } from './chatStepRenderers';

interface StepGroupProps {
  group: StepGroupUnit;
  friendly?: boolean;
  onPreviewFile?: (att: { id: string; filename: string; mime?: string }) => void; // reserved
}

export function StepGroup({ group, friendly }: StepGroupProps) {
  const [stepsOpen, setStepsOpen] = useState<boolean>(group.isActive);
  const [toolExpanded, setToolExpanded] = useState<Record<string, boolean>>({});

  // Auto-collapse when run finishes; auto-open when (re)activated.
  // Only fires on active↔inactive transitions, not on ordinary re-renders.
  const prevActive = useRef(group.isActive);
  useEffect(() => {
    if (prevActive.current && !group.isActive) {
      setStepsOpen(false); // just finished → fold
    } else if (!prevActive.current && group.isActive) {
      setStepsOpen(true); // (re)activated → open
    }
    prevActive.current = group.isActive;
  }, [group.isActive]);

  if (group.steps.length === 0) return null;

  // ── Summary counts ────────────────────────────────────────────────────────

  const stepCount = group.steps.length;

  let toolCount = 0;
  const previewNames: string[] = [];

  for (const step of group.steps) {
    const stepRole = (step.role || '').toUpperCase();
    const isTool = stepRole === 'TOOL' || step.blockKind === 'tool';
    const isThinking = step.blockKind === 'thinking';

    if (isTool) {
      toolCount += step.tool_calls?.length || 1;
      const name = step.tool_calls?.[0]?.name;
      if (name) previewNames.push(name);
    } else if (isThinking) {
      previewNames.push('thinking');
    }
  }

  const previewSlice = previewNames.slice(0, 2);
  const previewSuffix = previewNames.length > 2 ? '…' : '';
  const previewStr = previewSlice.join(', ') + previewSuffix;

  // ── Chip header label ─────────────────────────────────────────────────────

  const chipLabel = friendly
    ? `worked through ${stepCount} step${stepCount !== 1 ? 's' : ''}`
    : `${toolCount} tool call${toolCount !== 1 ? 's' : ''}`;

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex gap-3 items-start">
      <span className="cw-avatar accent lg shrink-0 font-mono">◆</span>
      <div className="flex-1 min-w-0">
        {/* Collapse chip header */}
        <div
          onClick={() => setStepsOpen((v) => !v)}
          className="inline-flex items-center gap-2 cursor-pointer text-dim"
          style={
            friendly
              ? {
                  padding: '6px 10px',
                  border: '1px solid var(--line-2)',
                  borderRadius: 'var(--r-sm)',
                  background: 'var(--paper)',
                }
              : { padding: '4px 8px' }
          }
        >
          <span className="font-mono text-[10.5px]">{stepsOpen ? '▾' : '▸'}</span>
          <span className="cw-label">{chipLabel}</span>
          {!stepsOpen && previewStr && (
            <span className="font-mono text-[10.5px] text-dim">— {previewStr}</span>
          )}
        </div>

        {/* Expanded steps with left rail */}
        {stepsOpen && (
          <div
            style={{
              borderLeft: '2px solid var(--accent-line)',
              marginLeft: 6,
              paddingLeft: 12,
            }}
            className="flex flex-col gap-2 mt-2"
          >
            {group.steps.map((step) => {
              if (step.blockKind === 'thinking') {
                return (
                  <ThinkingBlock key={step.id} content={step.content} friendly={friendly} />
                );
              }

              // Tool block: role TOOL or blockKind tool
              const tcs = step.tool_calls ?? [];
              if (tcs.length === 0) return null;

              return tcs.map((tc) => (
                <ToolRow
                  key={tc.id}
                  tc={tc}
                  rowKey={tc.id}
                  expanded={!!toolExpanded[tc.id]}
                  onToggle={() =>
                    setToolExpanded((p) => ({ ...p, [tc.id]: !p[tc.id] }))
                  }
                />
              ));
            })}
          </div>
        )}
      </div>
    </div>
  );
}
