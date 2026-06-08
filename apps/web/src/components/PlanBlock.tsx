import type { PlanStep } from '../lib/designFixtures';

interface Props {
  steps: PlanStep[];   // from chatStore planSteps[sessionId]
}

/**
 * Inline plan-steps block for the chat transcript. Ported from the AgentPage
 * context-rail Plan section, but a touch more readable now that it lives in the
 * wide chat column. Pure presentational — data arrives via props.
 */
export function PlanBlock({ steps }: Props) {
  if (!steps.length) return null;

  return (
    <div
      style={{
        marginTop: 8,
        padding: '9px 12px',
        border: '1px solid var(--line)',
        borderRadius: 'var(--r-md)',
        background: 'var(--paper)',
      }}
    >
      <div
        className="cw-label"
        style={{ marginBottom: 6 }}
      >
        Plan · {steps.length} steps
      </div>
      <div className="flex flex-col gap-0.5">
        {steps.map((s) => (
          <div
            key={s.n}
            className="flex items-center gap-2 px-1.5 py-1"
            style={{
              borderRadius: 4,
              background: s.live ? 'var(--accent-soft)' : 'transparent',
            }}
          >
            <span className="font-mono text-[11px] text-dim shrink-0">{s.n}</span>
            <span className="font-mono text-[12px] text-ink-2">{s.t}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
