/**
 * Home composer "+" menu — the home page has the same three sections as the
 * chat page (upload / skills / MCP), even though no session exists yet.
 *
 * Uses the REAL chat store: the whole point of these tests is the draft-slot
 * handover (home toggles → DRAFT_SESSION_ID → the session id created by
 * startChat), which a mocked store would not exercise.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { HomePage } from './HomePage';
import { DRAFT_SESSION_ID, useChatStore } from '../stores/chatStore';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  createSession: vi.fn(),
}));

vi.mock('react-router-dom', () => ({ useNavigate: () => mocks.navigate }));
vi.mock('../components/shell/PageHead', () => ({ PageHead: () => null }));
vi.mock('../components/app/CreateAppModal', () => ({ CreateAppModal: () => null }));
vi.mock('../i18n', () => ({ useTranslation: () => ({ lang: 'en' }) }));
vi.mock('../i18n/dates', () => ({ formatDate: () => 'Saturday, July 18' }));
vi.mock('../lib/workspaceContext', () => ({
  useWorkspace: () => ({
    activeWorkspace: { id: 'ws_1', name: 'Workspace', sensitivity_level: 'L2' },
    workspaces: [{ id: 'ws_1', name: 'Workspace', sensitivity_level: 'L2' }],
    setActiveWorkspaceId: vi.fn(),
  }),
}));
vi.mock('../hooks/useSessions', () => ({
  useSessions: () => ({ sessions: [], createSession: mocks.createSession }),
}));
vi.mock('../hooks/useTasks', () => ({ useTasks: () => ({ items: [] }) }));
vi.mock('../hooks/useApps', () => ({ useApps: () => ({ apps: [], loading: false }) }));
vi.mock('../hooks/useDefaultModel', () => ({
  useDefaultModel: () => ({ modelId: 'deepseek-v4-flash', isLoading: false }),
}));
vi.mock('../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({ checkFile: vi.fn().mockResolvedValue({ allowed: true }) }),
}));
vi.mock('../hooks/useFeatureFlags', () => ({ useFeatureFlag: () => true }));
vi.mock('../hooks/useInstalledSkills', () => ({
  useInstalledSkills: () =>
    Object.assign(
      [
        { id: 'skl_1', name: 'pdf-report', enabled_by_default: true },
        { id: 'skl_2', name: 'excel-clean', enabled_by_default: false },
      ],
      { refresh: vi.fn() },
    ),
}));
vi.mock('../hooks/useMcpServers', () => ({
  useMcpServers: () => ({
    servers: [
      {
        id: 'mcp_a',
        name: 'files-mcp',
        enabled: true,
        discovered_tools: [{ name: 'read_file', description: null }],
      },
      { id: 'mcp_b', name: 'jira-mcp', enabled: false, discovered_tools: [] },
    ],
    refresh: vi.fn(),
  }),
}));
vi.mock('../api/client', () => ({
  api: {
    attachments: { upload: vi.fn(), remove: vi.fn() },
    // Composer fetches the model catalogue via useModelOptions; resolve it
    // empty so the menu tests are unaffected by the fetch.
    modelPreferences: { listModels: vi.fn().mockResolvedValue([]) },
  },
}));

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Add files, skills, or tools' }));
}

// The "+" menu is a click-based flyout now: the skills chip cloud lives in a
// secondary panel that opens to the right of the primary menu when the Skills
// row is clicked (it no longer starts expanded).
async function openSkills(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('menuitem', { name: /^Skills/ }));
}

beforeEach(() => {
  mocks.navigate.mockReset();
  mocks.createSession.mockReset();
  useChatStore.setState({ selectedSkills: {}, selectedMcpServerIds: {} });
});

describe('HomePage "+" menu', () => {
  it('opens a menu with all three sections instead of jumping straight to the file picker', async () => {
    const user = userEvent.setup();
    render(<HomePage />);

    const trigger = screen.getByRole('button', { name: 'Add files, skills, or tools' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    await user.click(trigger);

    expect(screen.getByRole('menuitem', { name: /Upload files \/ images/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /^Skills/ })).toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /MCP tools/ })).toBeInTheDocument();

    // Open the skills flyout - chips reflect the install defaults.
    await openSkills(user);
    expect(screen.getByRole('button', { name: 'pdf-report' })).toHaveAttribute('data-accent', '1');
    expect(screen.getByRole('button', { name: 'excel-clean' })).toHaveAttribute('data-dashed', '1');

    await user.click(screen.getByRole('menuitem', { name: /MCP tools/ }));
    expect(screen.getByRole('menuitemcheckbox', { name: /files-mcp/ })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemcheckbox', { name: /jira-mcp/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('parks toggles in the draft slot — toggling never creates a session', async () => {
    const user = userEvent.setup();
    render(<HomePage />);
    await openMenu(user);
    await openSkills(user);

    await user.click(screen.getByRole('button', { name: 'pdf-report' }));
    await user.click(screen.getByRole('menuitem', { name: /MCP tools/ }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: /jira-mcp/ }));

    expect(mocks.createSession).not.toHaveBeenCalled();
    const s = useChatStore.getState();
    // Seeded from the defaults (['pdf-report']) then that one switched off →
    // an explicit "no skills", which is not the same as never having chosen.
    expect(s.selectedSkills[DRAFT_SESSION_ID]).toEqual([]);
    expect(s.selectedMcpServerIds[DRAFT_SESSION_ID]).toEqual(['mcp_a', 'mcp_b']);
  });

  it('hands the draft to the session created by startChat, then clears it', async () => {
    const user = userEvent.setup();
    mocks.createSession.mockResolvedValue({ id: 'ses_new' });
    render(<HomePage />);

    await openMenu(user);
    await openSkills(user);
    await user.click(screen.getByRole('button', { name: 'excel-clean' }));
    await user.click(screen.getByRole('menuitem', { name: /MCP tools/ }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: /files-mcp/ }));

    await user.type(screen.getByRole('textbox'), 'Plan my day tomorrow');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(mocks.navigate).toHaveBeenCalled());
    const s = useChatStore.getState();
    // Exactly what was ticked on the home page, now under the real session id.
    expect(s.selectedSkills.ses_new).toEqual(['pdf-report', 'excel-clean']);
    expect(s.selectedMcpServerIds.ses_new).toEqual([]);
    // Draft handed over and cleared.
    expect(s.selectedSkills[DRAFT_SESSION_ID]).toBeUndefined();
    expect(s.selectedMcpServerIds[DRAFT_SESSION_ID]).toBeUndefined();
  });

  it('does not leak one new task’s selection into the next one', async () => {
    const user = userEvent.setup();
    mocks.createSession.mockResolvedValue({ id: 'ses_first' });
    const first = render(<HomePage />);

    await openMenu(user);
    await openSkills(user);
    await user.click(screen.getByRole('button', { name: 'excel-clean' }));
    await user.type(screen.getByRole('textbox'), 'First task');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(mocks.navigate).toHaveBeenCalled());
    first.unmount();

    // Back on the home page for a second task, menu untouched this time.
    mocks.createSession.mockResolvedValue({ id: 'ses_second' });
    render(<HomePage />);
    await user.type(screen.getByRole('textbox'), 'Second task');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() => expect(mocks.navigate).toHaveBeenCalledTimes(2));

    const s = useChatStore.getState();
    expect(s.selectedSkills.ses_first).toEqual(['pdf-report', 'excel-clean']);
    // No selection at all → the backend applies the user's own defaults.
    expect(s.selectedSkills.ses_second).toBeUndefined();
  });
});
