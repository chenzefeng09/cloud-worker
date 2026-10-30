/**
 * McpServerFormDialog — stored env / header values arrive masked ("••••••"):
 *  - a masked row is shown read-only (key included) with a Replace action
 *  - saving untouched sends the sentinel back, which the registry keeps as-is
 *  - Replace takes a new value; Keep saved undoes it
 *  - probing with a masked header goes through the saved server's /test, which
 *    resolves the sentinel server-side (the id-less /probe would send it verbatim)
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { McpServer } from '../api/types';

const mcp = vi.hoisted(() => ({ test: vi.fn(), probe: vi.fn() }));

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>();
  return { ...actual, api: { admin: { mcp } } };
});

import { McpServerFormDialog } from './McpServerFormDialog';

const MASKED = '••••••';

const httpServer: McpServer = {
  id: 'mcp_1',
  tenant_id: 'tnt_1',
  name: 'weather',
  transport: 'http',
  endpoint: 'https://weather.example.com/mcp',
  command: null,
  env: {},
  enabled: true,
  headers: { 'X-Api-Key': MASKED, 'X-Trace': '' },
  auth: { type: 'none' },
  timeout_seconds: null,
  // A non-empty allowlist opens the tool filter, so "Fetch tool list" renders.
  tool_filter: { allow: ['search'], block: [] },
  args: [],
  discovered_tools: [],
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
};

function renderEdit(server: McpServer = httpServer) {
  const onSubmit = vi.fn().mockResolvedValue(server);
  render(
    <McpServerFormDialog
      mode="edit"
      server={server}
      onClose={() => {}}
      onSaved={() => {}}
      onSubmit={onSubmit}
    />,
  );
  return onSubmit;
}

describe('McpServerFormDialog masked env / header values', () => {
  beforeEach(() => {
    mcp.test.mockReset();
    mcp.probe.mockReset();
  });

  it('shows a stored value masked and read-only, key included', () => {
    renderEdit();
    const value = screen.getByLabelText('X-Api-Key value (saved, hidden)');
    expect(value).toHaveValue(MASKED);
    expect(value).toHaveAttribute('readonly');
    expect(screen.getByDisplayValue('X-Api-Key')).toHaveAttribute('readonly');
    expect(screen.getByRole('button', { name: 'Replace X-Api-Key value' })).toBeInTheDocument();
    // A blank stored value holds nothing to hide and stays editable.
    expect(screen.getByDisplayValue('X-Trace')).not.toHaveAttribute('readonly');
  });

  it('saving untouched sends the sentinel back so the stored value is kept', async () => {
    const onSubmit = renderEdit();
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0].headers).toEqual({ 'X-Api-Key': MASKED, 'X-Trace': '' });
  });

  it('Replace takes a new value', async () => {
    const onSubmit = renderEdit();
    await userEvent.click(screen.getByRole('button', { name: 'Replace X-Api-Key value' }));
    expect(screen.queryByLabelText('X-Api-Key value (saved, hidden)')).not.toBeInTheDocument();
    await userEvent.type(screen.getByPlaceholderText('new value'), 'rotated-key');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSubmit.mock.calls[0][0].headers).toEqual({ 'X-Api-Key': 'rotated-key', 'X-Trace': '' });
  });

  it('Keep saved undoes a replacement, renamed key included', async () => {
    const onSubmit = renderEdit();
    await userEvent.click(screen.getByRole('button', { name: 'Replace X-Api-Key value' }));
    const key = screen.getByDisplayValue('X-Api-Key');
    await userEvent.clear(key);
    await userEvent.type(key, 'X-Renamed');
    await userEvent.type(screen.getByPlaceholderText('new value'), 'oops');
    await userEvent.click(screen.getByRole('button', { name: 'Keep saved X-Api-Key value' }));
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSubmit.mock.calls[0][0].headers).toEqual({ 'X-Api-Key': MASKED, 'X-Trace': '' });
  });

  it('keeps a masked stdio env value the same way', async () => {
    const onSubmit = renderEdit({
      ...httpServer,
      transport: 'stdio',
      endpoint: null,
      command: 'python',
      env: { API_KEY: MASKED },
      headers: {},
    });
    expect(screen.getByLabelText('API_KEY value (saved, hidden)')).toHaveValue(MASKED);
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(onSubmit.mock.calls[0][0].env).toEqual({ API_KEY: MASKED });
  });

  it('probes through the saved server /test while a header is still masked', async () => {
    mcp.test.mockResolvedValue({ ok: true, tools: [{ name: 'search' }] });
    renderEdit();
    await userEvent.click(screen.getByRole('button', { name: 'Fetch tool list' }));
    expect(mcp.probe).not.toHaveBeenCalled();
    expect(mcp.test).toHaveBeenCalledWith('mcp_1', {
      endpoint: 'https://weather.example.com/mcp',
      headers: { 'X-Api-Key': MASKED, 'X-Trace': '' },
      // Auth is not masked here (type none), so the form's own auth goes along.
      auth: { type: 'none' },
      timeout_seconds: undefined,
    });
  });

  it('uses the id-less /probe once nothing masked is left', async () => {
    mcp.probe.mockResolvedValue({ ok: true, tools: [] });
    renderEdit();
    await userEvent.click(screen.getByRole('button', { name: 'Replace X-Api-Key value' }));
    await userEvent.type(screen.getByPlaceholderText('new value'), 'typed-key');
    await userEvent.click(screen.getByRole('button', { name: 'Fetch tool list' }));
    expect(mcp.test).not.toHaveBeenCalled();
    expect(mcp.probe).toHaveBeenCalledWith(
      expect.objectContaining({ headers: { 'X-Api-Key': 'typed-key', 'X-Trace': '' } }),
    );
  });
});
