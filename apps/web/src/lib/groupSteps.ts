import type { Message } from '../api/types';

export interface StepGroupUnit {
  kind: 'stepGroup';
  runId: string;
  steps: Message[];
  isActive: boolean;
}

export interface StandaloneUnit {
  kind: 'message';
  message: Message;
}

export type RenderUnit = StepGroupUnit | StandaloneUnit;

/**
 * groupSteps — pure function that collapses consecutive tool/thinking messages
 * that share the same run-id into a single StepGroupUnit, while leaving all
 * other messages as individual StandaloneUnits.
 *
 * A message is classified as a STEP iff:
 *   1. It has a non-null key  (streamRunId ?? run_id), AND
 *   2. Its role (uppercased) is 'TOOL', OR its blockKind is 'tool' or 'thinking'.
 *
 * Consecutive steps with the same key are merged into one StepGroupUnit.
 * Any non-step message (user, system, assistant text, key==null) flushes the
 * current group before being emitted as a StandaloneUnit.
 *
 * When isStreaming is true the last unit in the output, if it is a StepGroupUnit,
 * has its isActive flag set to true — all other StepGroupUnits remain false.
 *
 * Pure: input messages are never mutated.
 */
export function groupSteps(messages: Message[], isStreaming: boolean): RenderUnit[] {
  const units: RenderUnit[] = [];
  let currentGroup: StepGroupUnit | null = null;

  const flush = () => {
    if (currentGroup !== null) {
      units.push(currentGroup);
      currentGroup = null;
    }
  };

  for (const message of messages) {
    const role = (message.role || '').toUpperCase();
    const key = message.streamRunId ?? message.run_id; // string | null | undefined

    const isStep =
      key != null &&
      (role === 'TOOL' || message.blockKind === 'tool' || message.blockKind === 'thinking');

    if (isStep) {
      // key is narrowed to string here (key != null)
      const runId = key as string;

      if (currentGroup === null || currentGroup.runId !== runId) {
        flush();
        currentGroup = { kind: 'stepGroup', runId, steps: [], isActive: false };
      }
      currentGroup.steps.push(message);
    } else {
      flush();
      units.push({ kind: 'message', message });
    }
  }

  // Flush any trailing group
  flush();

  // Mark the last StepGroupUnit as active when streaming
  if (isStreaming && units.length > 0) {
    const last = units[units.length - 1];
    if (last.kind === 'stepGroup') {
      last.isActive = true;
    }
  }

  return units;
}
