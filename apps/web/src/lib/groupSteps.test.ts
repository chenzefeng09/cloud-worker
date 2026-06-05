/**
 * groupSteps unit tests — pure function, no DOM, no store.
 */
import { describe, it, expect } from 'vitest';
import type { Message, ToolCallEntry } from '../api/types';
import { groupSteps } from './groupSteps';
import type { StepGroupUnit, StandaloneUnit } from './groupSteps';

// ─── Factory ─────────────────────────────────────────────────────────────────

let _autoId = 0;

function mkMsg(partial: Partial<Message>): Message {
  return {
    id: partial.id ?? `auto_${++_autoId}`,
    session_id: 's',
    role: 'ASSISTANT',
    content: '',
    tool_calls: null,
    run_id: null,
    sequence: 0,
    created_at: '',
    ...partial,
  };
}

// Helpers to keep test data readable
const TOOL_CALLS: ToolCallEntry[] = [
  { id: 'tc1', name: 'Read', input: { file_path: '/foo.ts' } },
];

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('groupSteps', () => {
  // 1. Simple user + assistant text — no step group
  it('case 1: user + assistant text (run_id set but no blockKind) → 2 standalone units', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      mkMsg({ id: 't', role: 'ASSISTANT', content: 'hi', run_id: 'r1' }),
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(2);
    expect(units[0].kind).toBe('message');
    expect(units[1].kind).toBe('message');
  });

  // 2. thinking + two tool messages with the same run_id → single stepGroup
  it('case 2: thinking + tool messages consecutive → [user standalone, stepGroup(3)]', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      mkMsg({ id: 'th', blockKind: 'thinking', run_id: 'r1', content: '...' }),
      mkMsg({ id: 't1', role: 'TOOL', blockKind: 'tool', run_id: 'r1', tool_calls: TOOL_CALLS }),
      mkMsg({ id: 't2', role: 'TOOL', blockKind: 'tool', run_id: 'r1' }),
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(2);

    const first = units[0] as StandaloneUnit;
    expect(first.kind).toBe('message');
    expect(first.message.id).toBe('u');

    const group = units[1] as StepGroupUnit;
    expect(group.kind).toBe('stepGroup');
    expect(group.runId).toBe('r1');
    expect(group.steps).toHaveLength(3);
    expect(group.isActive).toBe(false);
  });

  // 3. Text message in the middle of tools with same run_id breaks the group
  it('case 3: tool → assistant text (same run_id) → tool → 4 units', () => {
    const toolMsg1 = mkMsg({ id: 'tool1', role: 'TOOL', blockKind: 'tool', run_id: 'r1' });
    const midText  = mkMsg({ id: 'mid',   role: 'ASSISTANT', content: '中间', run_id: 'r1' });
    const toolMsg2 = mkMsg({ id: 'tool2', role: 'TOOL', blockKind: 'tool', run_id: 'r1' });

    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      toolMsg1,
      midText,
      toolMsg2,
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(4);

    expect(units[0].kind).toBe('message');
    expect((units[0] as StandaloneUnit).message.id).toBe('u');

    expect(units[1].kind).toBe('stepGroup');
    expect((units[1] as StepGroupUnit).steps).toHaveLength(1);
    expect((units[1] as StepGroupUnit).steps[0].id).toBe('tool1');

    expect(units[2].kind).toBe('message');
    expect((units[2] as StandaloneUnit).message.id).toBe('mid');

    expect(units[3].kind).toBe('stepGroup');
    expect((units[3] as StepGroupUnit).steps).toHaveLength(1);
    expect((units[3] as StepGroupUnit).steps[0].id).toBe('tool2');
  });

  // 4. Multiple turns interleaved — order preserved, runIds correct
  it('case 4: user→tool(r1)→user2→thinking(r2) → [msg, stepGroup(r1), msg, stepGroup(r2)]', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u1', role: 'USER' }),
      mkMsg({ id: 'tool_r1', role: 'TOOL', blockKind: 'tool', run_id: 'r1' }),
      mkMsg({ id: 'u2', role: 'USER' }),
      mkMsg({ id: 'think_r2', blockKind: 'thinking', run_id: 'r2' }),
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(4);

    expect(units[0].kind).toBe('message');
    expect((units[0] as StandaloneUnit).message.id).toBe('u1');

    expect(units[1].kind).toBe('stepGroup');
    expect((units[1] as StepGroupUnit).runId).toBe('r1');
    expect((units[1] as StepGroupUnit).steps[0].id).toBe('tool_r1');

    expect(units[2].kind).toBe('message');
    expect((units[2] as StandaloneUnit).message.id).toBe('u2');

    expect(units[3].kind).toBe('stepGroup');
    expect((units[3] as StepGroupUnit).runId).toBe('r2');
    expect((units[3] as StepGroupUnit).steps[0].id).toBe('think_r2');
  });

  // 5. SYSTEM with null run_id never groups
  it('case 5: SYSTEM message with run_id null → single standalone unit', () => {
    const messages: Message[] = [
      mkMsg({ id: 'e', role: 'SYSTEM', content: 'err', run_id: null }),
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(1);
    expect(units[0].kind).toBe('message');
  });

  // 6. Streaming active flag — last stepGroup gets isActive=true
  it('case 6a: isStreaming=true → last stepGroup.isActive===true', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      mkMsg({ id: 'th', blockKind: 'thinking', run_id: 'r1', content: '...' }),
      mkMsg({ id: 't1', role: 'TOOL', blockKind: 'tool', run_id: 'r1', tool_calls: TOOL_CALLS }),
      mkMsg({ id: 't2', role: 'TOOL', blockKind: 'tool', run_id: 'r1' }),
    ];
    const units = groupSteps(messages, true);
    expect(units).toHaveLength(2);
    expect((units[1] as StepGroupUnit).isActive).toBe(true);
  });

  it('case 6b: two stepGroups separated by user msg + isStreaming=true → only last is active', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u1', role: 'USER' }),
      mkMsg({ id: 'tool_r1', role: 'TOOL', blockKind: 'tool', run_id: 'r1' }),
      mkMsg({ id: 'u2', role: 'USER' }),
      mkMsg({ id: 'think_r2', blockKind: 'thinking', run_id: 'r2' }),
    ];
    const units = groupSteps(messages, true);
    expect(units).toHaveLength(4);

    const group1 = units[1] as StepGroupUnit;
    const group2 = units[3] as StepGroupUnit;

    expect(group1.kind).toBe('stepGroup');
    expect(group1.isActive).toBe(false); // earlier group NOT active

    expect(group2.kind).toBe('stepGroup');
    expect(group2.isActive).toBe(true);  // only the last is active
  });

  it('case 6c: isStreaming=false → stepGroup.isActive stays false', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      mkMsg({ id: 'th', blockKind: 'thinking', run_id: 'r1' }),
    ];
    const units = groupSteps(messages, false);
    const group = units[1] as StepGroupUnit;
    expect(group.isActive).toBe(false);
  });

  // 7. Hydrated DB tool row: role='TOOL', no blockKind → groups by role
  it('case 7: hydrated DB tool row (role TOOL, no blockKind) → groups into stepGroup', () => {
    const messages: Message[] = [
      mkMsg({ id: 'u', role: 'USER' }),
      mkMsg({ id: 'h', role: 'TOOL', run_id: 'r1', tool_calls: TOOL_CALLS }),
      // blockKind intentionally omitted — simulates a DB-hydrated row
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(2);

    const group = units[1] as StepGroupUnit;
    expect(group.kind).toBe('stepGroup');
    expect(group.runId).toBe('r1');
    expect(group.steps).toHaveLength(1);
    expect(group.steps[0].id).toBe('h');
  });

  // Bonus: empty input → empty output
  it('empty messages → empty units', () => {
    expect(groupSteps([], false)).toEqual([]);
    expect(groupSteps([], true)).toEqual([]);
  });

  // Bonus: streamRunId takes precedence over run_id for grouping key
  it('streamRunId takes precedence over run_id as the grouping key', () => {
    const messages: Message[] = [
      mkMsg({ id: 'a', blockKind: 'thinking', streamRunId: 'sr1', run_id: 'db_run' }),
      mkMsg({ id: 'b', role: 'TOOL', blockKind: 'tool', streamRunId: 'sr1', run_id: 'db_run' }),
    ];
    const units = groupSteps(messages, false);
    expect(units).toHaveLength(1);
    const group = units[0] as StepGroupUnit;
    expect(group.runId).toBe('sr1');
    expect(group.steps).toHaveLength(2);
  });
});
