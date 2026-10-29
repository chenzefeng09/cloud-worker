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
vi.mock('../components/FilePreviewPanel', () => ({
  FilePreviewPanel: ({
    file,
    onClose,
  }: {
    file: { filename: string };
    onClose: () => void;
  }) => (
    <aside aria-label={`File preview: ${file.filename}`}>
      <button type="button" onClick={onClose}>Back</button>
    </aside>
  ),
}));
vi.mock('../components/CanvasReadonlyView', () => ({
  CanvasReadonlyView: ({
    canvasId,
    onClose,
  }: {
    canvasId: string;
    onClose: () => void;
  }) => (
    <aside aria-label={`Canvas preview: ${canvasId}`}>
      <button type="button" onClick={onClose}>Back</button>
    </aside>
  ),
}));

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

const file = {
  id: 'att_1',
  session_id: 'ses_1',
  kind: 'file' as const,
  title: '季度数据',
  canvas_kind: null,
  filename: 'quarter.csv',
  mime: 'text/csv',
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
    await user.click(screen.getByTitle('Delete'));

    const dialog = screen.getByRole('dialog', { name: 'Delete artifact' });
    expect(dialog).toHaveTextContent('Delete “季度总结”? This can’t be undone.');
    expect(nativeConfirm).not.toHaveBeenCalled();
    expect(nativeAlert).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog', { name: 'Delete artifact' })).not.toBeInTheDocument();
  });

  it('deletes only after the in-app confirm button is clicked', async () => {
    const user = userEvent.setup();
    render(<ArtifactsPage />);

    await screen.findByText('季度总结');
    await user.click(screen.getByTitle('Delete'));
    await user.click(screen.getByRole('button', { name: 'Delete' }));

    await waitFor(() => expect(mocks.deleteCanvas).toHaveBeenCalledWith('cvs_1'));
  });
});

describe('ArtifactsPage preview layout', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.list.mockResolvedValue({ items: [canvas, file], total: 2 });
  });

  it('keeps the artifact list visible and opens file previews on the right', async () => {
    const user = userEvent.setup();
    render(<ArtifactsPage />);

    await user.click(await screen.findByText('季度数据'));

    expect(screen.getByLabelText('File preview: quarter.csv')).toBeInTheDocument();
    expect(screen.getByRole('separator', { name: 'Drag to resize the preview panel' })).toBeInTheDocument();
    expect(screen.getByText('季度总结')).toBeVisible();

    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.queryByLabelText('File preview: quarter.csv')).not.toBeInTheDocument();
    expect(screen.queryByRole('separator', { name: 'Drag to resize the preview panel' })).not.toBeInTheDocument();
  });

  it('switches the right-side preview from a file to a canvas', async () => {
    const user = userEvent.setup();
    render(<ArtifactsPage />);

    await user.click(await screen.findByText('季度数据'));
    await user.click(screen.getByText('季度总结'));

    expect(screen.queryByLabelText('File preview: quarter.csv')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Canvas preview: cvs_1')).toBeInTheDocument();
  });
});
