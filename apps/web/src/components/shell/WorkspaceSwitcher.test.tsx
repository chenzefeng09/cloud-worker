/**
 * WorkspaceSwitcher — workspace data + dropdown rendering tests.
 *
 * Verifies that:
 * 1. WORKSPACES no longer carries taskCount / fileCount mock fields
 * 2. WorkspaceSwitcher dropdown shows workspace description (not fake counts)
 * 3. Fake count strings ("个文件", "个进行中") are absent from the rendered UI
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WORKSPACES } from '../../lib/workspaceData';
import { WorkspaceSwitcher } from './WorkspaceSwitcher';

// ── Mocks ────────────────────────────────────────────────────────────────────

vi.mock('react-router-dom', () => ({
  useNavigate: () => vi.fn(),
}));

vi.mock('../../lib/workspaceContext', () => ({
  useWorkspace: () => ({
    wsId: 'eng',
    setWsId: vi.fn(),
    ws: WORKSPACES[0],  // eng workspace
    allWs: WORKSPACES,
    files: [],
  }),
}));

// ── Tests ────────────────────────────────────────────────────────────────────

describe('WORKSPACES data shape', () => {
  it('does not contain taskCount on any workspace', () => {
    for (const ws of WORKSPACES) {
      expect((ws as unknown as Record<string, unknown>).taskCount).toBeUndefined();
    }
  });

  it('does not contain fileCount on any workspace', () => {
    for (const ws of WORKSPACES) {
      expect((ws as unknown as Record<string, unknown>).fileCount).toBeUndefined();
    }
  });

  it('still has id, name, glyph, desc, accent on all workspaces', () => {
    for (const ws of WORKSPACES) {
      expect(ws.id).toBeTruthy();
      expect(ws.name).toBeTruthy();
      expect(ws.glyph).toBeTruthy();
      expect(ws.desc).toBeTruthy();
      expect(ws.accent).toMatch(/^#/);
    }
  });
});

describe('WorkspaceSwitcher dropdown', () => {
  it('does not show "个文件" mock count text in dropdown', () => {
    render(<WorkspaceSwitcher />);
    // Open the dropdown
    const trigger = screen.getByText('工程');
    fireEvent.click(trigger.closest('[class]')!);

    // Mock count strings must be absent
    expect(screen.queryByText(/个文件/)).toBeNull();
    expect(screen.queryByText(/个进行中/)).toBeNull();
  });

  it('shows workspace description text in dropdown items', () => {
    render(<WorkspaceSwitcher />);
    // Open the dropdown
    const trigger = screen.getByText('工程');
    fireEvent.click(trigger.closest('[class]')!);

    // Desc for eng workspace
    expect(screen.getAllByText('代码 · 仓库 · 基础设施').length).toBeGreaterThan(0);
  });
});
