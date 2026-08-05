/**
 * POST /chat body — the three-state contract for `skills` and
 * `mcp_server_ids`.
 *
 *   key ABSENT   → the session never chose; the backend applies its defaults
 *   explicit []  → everything off for this session
 *   non-empty    → exactly these
 *
 * `undefined` and `[]` are DIFFERENT wire payloads. A truthiness check
 * (`ids?.length ? {ids} : {}`) would collapse them and silently turn "all off"
 * into "use the defaults", so these cases are locked down here.
 */
import { beforeEach, describe, expect, it } from 'vitest';

import { buildChatRequestBody } from './useChat';
import { useChatStore } from '../stores/chatStore';

const sessionId = 'ses_body';

beforeEach(() => {
  useChatStore.setState({
    selectedSkills: {},
    selectedMcpServerIds: {},
    selectedModel: {},
    selectedEffort: {},
    executionMode: {},
    platformDefaultsEnabled: {},
  });
});

describe('buildChatRequestBody — mcp_server_ids', () => {
  it('omits the key entirely when the session has no selection', () => {
    const body = buildChatRequestBody(sessionId, { text: '你好' });

    expect('mcp_server_ids' in body).toBe(false);
    expect(body).toEqual({
      message: '你好',
      attachment_ids: [],
      execution_mode: 'sandbox',
    });
  });

  it('sends an explicit [] when the user turned every server off', () => {
    useChatStore.setState({ selectedMcpServerIds: { [sessionId]: [] } });

    const body = buildChatRequestBody(sessionId, { text: '你好' });

    expect('mcp_server_ids' in body).toBe(true);
    expect(body.mcp_server_ids).toEqual([]);
  });

  it('sends the exact list when servers are selected', () => {
    useChatStore.setState({ selectedMcpServerIds: { [sessionId]: ['mcp_a', 'mcp_b'] } });

    expect(buildChatRequestBody(sessionId, { text: '你好' }).mcp_server_ids).toEqual([
      'mcp_a',
      'mcp_b',
    ]);
  });

  it('produces different JSON for undefined vs []', () => {
    const absent = JSON.stringify(buildChatRequestBody(sessionId, { text: '你好' }));
    useChatStore.setState({ selectedMcpServerIds: { [sessionId]: [] } });
    const empty = JSON.stringify(buildChatRequestBody(sessionId, { text: '你好' }));

    expect(absent).not.toContain('mcp_server_ids');
    expect(empty).toContain('"mcp_server_ids":[]');
    expect(absent).not.toEqual(empty);
  });

  it('is independent of the skills key', () => {
    useChatStore.setState({
      selectedSkills: { [sessionId]: [] },
      selectedMcpServerIds: { [sessionId]: ['mcp_a'] },
    });

    const body = buildChatRequestBody(sessionId, { text: '你好' });
    expect(body.skills).toEqual([]);
    expect(body.mcp_server_ids).toEqual(['mcp_a']);
  });
});

describe('buildChatRequestBody — skills keeps its existing behaviour', () => {
  it('omits skills when unset and sends [] when explicitly emptied', () => {
    expect('skills' in buildChatRequestBody(sessionId, { text: 'x' })).toBe(false);

    useChatStore.setState({ selectedSkills: { [sessionId]: [] } });
    const body = buildChatRequestBody(sessionId, { text: 'x' });
    expect('skills' in body).toBe(true);
    expect(body.skills).toEqual([]);
  });
});

describe('buildChatRequestBody — include_platform_defaults (per-session opt-out)', () => {
  it('omits the key by default (platform defaults stay on)', () => {
    expect('include_platform_defaults' in buildChatRequestBody(sessionId, { text: 'x' })).toBe(
      false,
    );
  });

  it('sends false only when the session opted out', () => {
    useChatStore.setState({ platformDefaultsEnabled: { [sessionId]: false } });
    const body = buildChatRequestBody(sessionId, { text: 'x' });
    expect(body.include_platform_defaults).toBe(false);
  });

  it('omits the key for another session that did not opt out', () => {
    useChatStore.setState({ platformDefaultsEnabled: { [sessionId]: false } });
    expect(
      'include_platform_defaults' in buildChatRequestBody('ses_other', { text: 'x' }),
    ).toBe(false);
  });
});

describe('buildChatRequestBody — remaining fields', () => {
  it('forwards model, effort, execution mode and workspace files', () => {
    useChatStore.setState({
      selectedModel: { [sessionId]: 'deepseek-v4' },
      selectedEffort: { [sessionId]: 'high' },
      executionMode: { [sessionId]: 'inline' },
    });

    const body = buildChatRequestBody(sessionId, {
      text: '看看 @a.docx',
      attachmentIds: ['att_1'],
      workspaceFileIds: ['wsf_1'],
    });

    expect(body).toEqual({
      message: '看看 @a.docx',
      attachment_ids: ['att_1'],
      model: 'deepseek-v4',
      reasoning_effort: 'high',
      execution_mode: 'inline',
      workspace_file_ids: ['wsf_1'],
    });
  });

  it('drops empty effort and empty workspace file lists', () => {
    const body = buildChatRequestBody(sessionId, { text: 'x', workspaceFileIds: [] });
    expect('reasoning_effort' in body).toBe(false);
    expect('workspace_file_ids' in body).toBe(false);
  });
});
