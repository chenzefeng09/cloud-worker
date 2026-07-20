import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ArtifactsPage } from './ArtifactsPage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  deleteCanvas: vi.fn(),
}));

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    artifacts: {
      list: (...args: unknown[]) => mocks.list(...args),
      deleteCanvas: (...args: unknown[]) => mocks.deleteCanvas(...args),
      deleteFile: vi.fn(),
    },
  },
}));

vi.mock('../components/shell/PageHead', () => ({ PageHead: () => null }));
vi.mock('../components/FilePreviewPanel', () => ({ FilePreviewPanel: () => null }));
vi.mock('../components/CanvasReadonlyView', () => ({ CanvasReadonlyView: () => null }));

const canvas = {
  id: 'cvs_1',
  session_id: 'ses_1',
  kind: 'canvas' as const,
  title: '季度总结',
  canvas_kind: 'article',
  filename: null,
  mime: null,
  created_at: '2026-07-18T00:00:00Z',
  updated_at: '2026-07-18T00:00:00Z',
};

describe('ArtifactsPage delete confirmation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.list.mockResolvedValue({ items: [canvas], total: 1 });
    mocks.deleteCanvas.mockResolvedValue(undefined);
  });

  it('uses the in-app dialog and never opens a native browser dialog', async () => {
    const user = userEvent.setup();
    const nativeConfirm = vi.spyOn(window, 'confirm');
    const nativeAlert = vi.spyOn(window, 'alert');
    render(<ArtifactsPage />);

    await screen.findByText('季度总结');
    await user.click(screen.getByTitle('删除'));

    const dialog = screen.getByRole('dialog', { name: '删除产物' });
    expect(dialog).toHaveTextContent('确定删除「季度总结」？此操作不可撤销。');
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(nativeAlert).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByRole('dialog', { name: '删除产物' })).not.toBeInTheDocument();
  });

  it('deletes only after the in-app confirm button is clicked', async () => {
    const user = userEvent.setup();
    render(<ArtifactsPage />);

    await screen.findByText('季度总结');
    await user.click(screen.getByTitle('删除'));
    await user.click(screen.getByRole('button', { name: '删除' }));

    await waitFor(() => expect(mocks.deleteCanvas).toHaveBeenCalledWith('cvs_1'));
  });
});
