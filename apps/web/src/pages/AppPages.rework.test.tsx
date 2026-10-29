/**
 * App pages interaction rework — guards the three regressions the redesign was
 * meant to fix: the Run CTA must live in the body (not the page head), the
 * Run history entry point must appear once, and every confirm must be the in-app
 * dialog rather than window.confirm.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DialogProvider } from '../admin/components/dialogs';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  get: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
  listRuns: vi.fn(),
  listSkills: vi.fn(),
  listMcpServers: vi.fn(),
}));

/** `n` terminal runs, newest-first, shaped like GET /apps/{id}/runs items. */
function makeRuns(n: number) {
  return Array.from({ length: n }, (_, i) => ({
    id: `run_${i}`,
    session_id: `ses_${i}`,
    status: 'COMPLETED',
    source: 'user',
    execution_mode: 'agent',
    created_at: '2026-07-20T00:00:00Z',
    result_summary: `第 ${i} 次结果`,
    error_message: null,
    inputs: [{ key: 'topic', label: '主题', value: `主题${i}` }],
    app: { app_name: '诗歌生成器', app_version: '1', model: 'deepseek-v4-flash' },
  }));
}

const APP = {
  id: 'app_1',
  name: '诗歌生成器',
  icon: '📝',
  description: '写诗',
  visibility: 'tenant',
  author_id: 'usr_alice',
  tags: [],
  current_version_id: 'v1',
  versions: [
    {
      id: 'v1',
      version: '1',
      status: 'published',
      model: 'deepseek-v4-flash',
      reasoning_effort: null,
      sampling_params: {},
      forced_skills: [],
      forced_mcp_server_ids: [],
      input_schema: [{ key: 'topic', label: '主题', type: 'text', required: true }],
      system_prompt: '',
      user_prompt_template: '写一首关于 /topic 的诗',
      created_at: '2026-07-01T00:00:00Z',
    },
  ],
};

vi.mock('react-router-dom', () => ({
  useNavigate: () => mocks.navigate,
  useParams: () => ({ id: 'app_1' }),
}));
vi.mock('../components/shell/PageHead', () => ({
  PageHead: ({ actions }: { actions?: React.ReactNode }) => (
    <div data-testid="page-head">{actions}</div>
  ),
}));
vi.mock('../stores/chatStore', () => ({
  useChatStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ userProfile: { id: 'usr_alice', roles: [] } }),
}));
vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    apps: {
      get: (...a: unknown[]) => mocks.get(...a),
      update: (...a: unknown[]) => mocks.update(...a),
      remove: (...a: unknown[]) => mocks.remove(...a),
      listRuns: (...a: unknown[]) => mocks.listRuns(...a),
      createVersion: vi.fn(),
      updateVersion: vi.fn(),
    },
    library: {
      listSkills: () => mocks.listSkills(),
      listMcpServers: () => mocks.listMcpServers(),
    },
  },
}));

import { AppDetailPage } from './AppDetailPage';
import { AppEditorPage } from './AppEditorPage';

function renderWithShell(ui: React.ReactElement) {
  return render(<DialogProvider>{ui}</DialogProvider>);
}

describe('acceptance', () => {
  it('detail page: hero CTA + gear menu', async () => {
    const user = userEvent.setup();
    mocks.get.mockResolvedValue(APP);
    mocks.listRuns.mockResolvedValue({ items: [] });
    renderWithShell(<AppDetailPage />);

    await screen.findByText('诗歌生成器');

    // Run lives in the body, NOT the page head
    const runBtns = screen.getAllByRole('button', { name: 'Run' });
    expect(runBtns).toHaveLength(1);
    const head = screen.getByTestId('page-head');
    expect(head.contains(runBtns[0])).toBe(false);
    expect(runBtns[0]).not.toBeDisabled();
    // compact height: no oversized vertical padding
    expect(runBtns[0].className).toContain('py-1.5');

    // page head only has the gear trigger
    const headBtns = within(head).getAllByRole('button');
    expect(headBtns).toHaveLength(1);
    expect(headBtns[0]).toHaveAttribute('aria-label', 'More actions');
    expect(headBtns[0]).toHaveAttribute('aria-haspopup', 'menu');
    expect(headBtns[0]).toHaveAttribute('aria-expanded', 'false');
    // it's an icon button (square hit target), not the old bare ⋯ glyph
    expect(headBtns[0].querySelector('svg')).not.toBeNull();
    expect(headBtns[0].textContent).not.toContain('⋯');

    // no duplicate Run history entry point in the head
    expect(screen.queryByRole('button', { name: 'Run history' })).toBeNull();

    // gear opens Edit app / Set up trigger / Delete
    await user.click(headBtns[0]);
    const menu = screen.getByRole('menu');
    const items = within(menu).getAllByRole('menuitem').map((b) => b.textContent);
    expect(items).toEqual(['Edit app', 'Set up trigger', 'Delete']);
    // published app → Set up trigger is actionable
    expect(within(menu).getByRole('menuitem', { name: 'Set up trigger' })).not.toBeDisabled();

    // Delete goes through the in-app confirm dialog (no window.confirm)
    const confirmSpy = vi.spyOn(window, 'confirm');
    await user.click(within(menu).getByRole('menuitem', { name: 'Delete' }));
    await screen.findByText('Delete app');
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('detail page: unpublished app disables Run with visible hint', async () => {
    mocks.get.mockResolvedValue({
      ...APP,
      current_version_id: null,
      versions: [{ ...APP.versions[0], status: 'draft' }],
    });
    mocks.listRuns.mockResolvedValue({ items: [] });
    renderWithShell(<AppDetailPage />);
    await screen.findByText('诗歌生成器');
    expect(screen.getByRole('button', { name: 'Run' })).toBeDisabled();
    expect(screen.getAllByText('This app hasn’t been published yet').length).toBeGreaterThan(0);
  });

  it('detail page: Run history card lists recent runs with a View more link', async () => {
    mocks.get.mockResolvedValue(APP);
    mocks.listRuns.mockResolvedValue({ items: makeRuns(5) });
    renderWithShell(<AppDetailPage />);

    await screen.findByText('第 0 次结果');
    expect(mocks.listRuns).toHaveBeenCalledWith('app_1', { limit: 5, offset: 0 });
    for (let i = 0; i < 5; i++) {
      expect(screen.getByText(`第 ${i} 次结果`)).toBeTruthy();
    }
    expect(screen.getByRole('button', { name: 'View more' })).toBeTruthy();
  });

  it('detail page: fewer than 5 runs hides View more; zero runs shows the empty state', async () => {
    mocks.get.mockResolvedValue(APP);
    mocks.listRuns.mockResolvedValue({ items: makeRuns(2) });
    const { unmount } = renderWithShell(<AppDetailPage />);
    await screen.findByText('第 0 次结果');
    expect(screen.queryByRole('button', { name: 'View more' })).toBeNull();
    unmount();

    mocks.listRuns.mockResolvedValue({ items: [] });
    renderWithShell(<AppDetailPage />);
    await screen.findByText('No runs yet.');
    expect(screen.queryByRole('button', { name: 'View more' })).toBeNull();
  });

  it('detail page: a runs-fetch failure degrades to an in-card message', async () => {
    mocks.get.mockResolvedValue(APP);
    mocks.listRuns.mockRejectedValue(new Error('boom'));
    renderWithShell(<AppDetailPage />);
    // the page itself still renders
    await screen.findByText('诗歌生成器');
    await screen.findByText('Failed to load run history');
    expect(screen.getByRole('button', { name: 'Run' })).toBeTruthy();
  });

  it('detail page: Version history button opens a dialog listing versions', async () => {
    const user = userEvent.setup();
    mocks.get.mockResolvedValue(APP);
    mocks.listRuns.mockResolvedValue({ items: [] });
    renderWithShell(<AppDetailPage />);

    await screen.findByText('诗歌生成器');

    // Version history entry lives in the hero (visible to all users, not just author/admin).
    const historyBtn = screen.getByRole('button', { name: 'Version history' });
    expect(historyBtn).not.toBeDisabled();
    await user.click(historyBtn);

    const dialog = await screen.findByRole('dialog', { name: 'Version history' });
    // the published v1 row renders with its status chip + Current marker
    expect(within(dialog).getByText('v1')).toBeTruthy();
    expect(within(dialog).getByText('Published')).toBeTruthy();
    expect(within(dialog).getByText('Current')).toBeTruthy();
  });

  it('editor page: step 1 shows the app-info block prefilled, saves meta before version', async () => {
    const user = userEvent.setup();
    mocks.get.mockResolvedValue(APP);
    mocks.listSkills.mockResolvedValue({ items: [] });
    mocks.listMcpServers.mockResolvedValue({ items: [] });
    mocks.update.mockResolvedValue(APP);
    renderWithShell(<AppEditorPage />);

    await screen.findByText('App info');
    const nameInput = screen.getByDisplayValue('诗歌生成器');
    expect(nameInput).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByDisplayValue('写诗')).toBeInstanceOf(HTMLTextAreaElement);

    // Cancel opens the in-app dialog, not window.confirm
    const confirmSpy = vi.spyOn(window, 'confirm');
    await user.click(screen.getAllByRole('button', { name: 'Cancel' })[0]);
    await screen.findByText('Discard changes');
    expect(confirmSpy).not.toHaveBeenCalled();
    const cancels = screen.getAllByRole('button', { name: 'Cancel' });
    await user.click(cancels[cancels.length - 1]);
    confirmSpy.mockRestore();

    // renaming then saving pushes api.apps.update first
    await user.clear(nameInput);
    await user.type(nameInput, '新名字');
    await user.click(screen.getAllByRole('button', { name: 'Save' })[0]);
    await waitFor(() =>
      expect(mocks.update).toHaveBeenCalledWith('app_1', {
        name: '新名字',
        icon: '📝',
        description: '写诗',
        visibility: 'tenant',
      }),
    );
  });
});
