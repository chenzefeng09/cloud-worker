import { describe, expect, it } from 'vitest';
import type { ToolCallEntry } from '../api/types';
import { extractTarget, normalizeToolInput } from './chatStepRenderers';

function bash(input: unknown, extra: Record<string, unknown> = {}): ToolCallEntry {
  return {
    id: 'tc-1',
    name: 'Bash',
    input: input as Record<string, unknown>,
    ...extra,
  };
}

describe('Bash tool input rendering', () => {
  it('renders the canonical command field', () => {
    expect(extractTarget(bash({ cmd: 'pwd' }))).toBe('pwd');
  });

  it('renders a raw command stored as value', () => {
    expect(extractTarget(bash({ value: '/opt/matlab/bin/matlab' }))).toBe(
      '/opt/matlab/bin/matlab',
    );
  });

  it('decodes legacy args when canonical input is empty', () => {
    const toolCall = bash({}, { args: '{"command":"ls -la"}' });
    expect(normalizeToolInput(toolCall)).toEqual({ command: 'ls -la' });
    expect(extractTarget(toolCall)).toBe('ls -la');
  });

  it('decodes a raw JSON input string', () => {
    expect(extractTarget(bash('{"cmd":"whoami"}'))).toBe('whoami');
  });
});
