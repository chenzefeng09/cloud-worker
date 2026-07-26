import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { HomePage } from './HomePage';

const mocks = vi.hoisted(() => ({
  navigate: vi.fn(),
  createSession: vi.fn(),
  setActiveWorkspaceId: vi.fn(),
  setSessionModel: vi.fn(),
  setSessionEffort: vi.fn(),
  setSessionExecutionMode: vi.fn(),
}));

vi.mock('react-router-dom', () => ({ useNavigate: () => mocks.navigate }));
vi.mock('../components/shell/PageHead', () => ({ PageHead: () => null }));
vi.mock('../components/app/CreateAppModal', () => ({ CreateAppModal: () => null }));
vi.mock('../i18n', () => ({ useTranslation: () => ({ lang: 'zh' }) }));
vi.mock('../i18n/dates', () => ({ formatDate: () => '7 月 18 日' }));
vi.mock('../lib/workspaceContext', () => ({
  useWorkspace: () => ({
    activeWorkspace: { id: 'ws_1', name: '工作区', sensitivity_level: 'L2' },
    workspaces: [{ id: 'ws_1', name: '工作区', sensitivity_level: 'L2' }],
    setActiveWorkspaceId: mocks.setActiveWorkspaceId,
  }),
}));
vi.mock('../hooks/useSessions', () => ({
  useSessions: () => ({ sessions: [], createSession: mocks.createSession }),
}));
vi.mock('../hooks/useApps', () => ({
  useApps: () => ({ apps: [], loading: false }),
}));
vi.mock('../hooks/useDefaultModel', () => ({
  useDefaultModel: () => ({ modelId: 'deepseek-v4-flash', isLoading: false }),
}));
vi.mock('../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({ checkFile: vi.fn().mockResolvedValue({ allowed: true }) }),
}));
vi.mock('../hooks/useFeatureFlags', () => ({ useFeatureFlag: () => true }));
vi.mock('../hooks/useInstalledSkills', () => ({
  useInstalledSkills: () => Object.assign([], { refresh: vi.fn() }),
}));
vi.mock('../hooks/useMcpServers', () => ({
  useMcpServers: () => ({ servers: [], refresh: vi.fn() }),
}));
vi.mock('../stores/chatStore', () => ({
  DRAFT_SESSION_ID: '__draft__',
  useChatStore: (selector: (state: Record<string, unknown>) => unknown) =>
    selector({
      selectedSkills: {},
      selectedMcpServerIds: {},
      toggleSkill: vi.fn(),
      seedSkills: vi.fn(),
      toggleMcpServer: vi.fn(),
      seedMcpServerIds: vi.fn(),
      commitDraftSelections: vi.fn(),
      setSessionModel: mocks.setSessionModel,
      setSessionEffort: mocks.setSessionEffort,
      setSessionExecutionMode: mocks.setSessionExecutionMode,
    }),
}));
vi.mock('../api/client', () => ({
  api: {
    attachments: { upload: vi.fn(), remove: vi.fn() },
    // Composer fetches the model catalogue via useModelOptions; resolve empty
    // so it falls back to MODEL_OPTIONS (the useDefaultModel mock returns
    // 'deepseek-v4-flash', which is a MODEL_OPTIONS entry) - no auto-correct.
    modelPreferences: { listModels: vi.fn().mockResolvedValue([]) },
  },
}));

beforeEach(() => {
  vi.clearAllMocks();
  mocks.createSession.mockReset();
});

describe('HomePage composer toolbar', () => {
  it('uses a single-surface Codex-style control row', async () => {
    const user = userEvent.setup();
    render(<HomePage />);

    const toolbar = screen.getByTestId('home-composer-toolbar');
    expect(toolbar).not.toHaveClass('border-t', 'bg-paper');

    const addButton = screen.getByRole('button', { name: '添加文件、技能或工具' });
    expect(addButton).toHaveClass('rounded-full', 'w-9', 'h-9');

    expect(screen.queryByRole('radiogroup', { name: '运行模式' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '选择工作区' })).toHaveTextContent('工作区（内部）');
    expect(screen.getByRole('button', { name: '选择运行模式' })).toHaveTextContent('智能体');
    expect(screen.getByRole('tooltip', { name: '选择智能体或对话模式' })).toBeInTheDocument();

    const sendButton = screen.getByRole('button', { name: '发送' });
    expect(sendButton).toHaveClass('rounded-full', 'w-10', 'h-10');
    expect(sendButton).toBeDisabled();

    await user.type(screen.getByRole('textbox'), '帮我规划明天');
    expect(sendButton).toBeEnabled();
  });

  it('uses an auto-title placeholder while forwarding the complete first message', async () => {
    const user = userEvent.setup();
    const longPrompt = `MATLAB FOC ${'validation '.repeat(40)}`;
    mocks.createSession.mockResolvedValue({ id: 'ses_long' });
    render(<HomePage />);

    fireEvent.change(screen.getByRole('textbox'), { target: { value: longPrompt } });
    await user.click(screen.getByRole('button', { name: '发送' }));

    await waitFor(() => expect(mocks.navigate).toHaveBeenCalled());
    expect(mocks.createSession.mock.calls[0][0]).toBe('新任务');
    expect(mocks.navigate).toHaveBeenCalledWith(
      '/tasks/ses_long?mode=friendly',
      expect.objectContaining({
        state: expect.objectContaining({ initialMessage: longPrompt.trim() }),
      }),
    );
  });

  it('keeps the draft and shows an error when session creation fails', async () => {
    const user = userEvent.setup();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    mocks.createSession.mockResolvedValue(undefined);
    render(<HomePage />);

    const textbox = screen.getByRole('textbox');
    await user.type(textbox, '保留这段任务');
    await user.click(screen.getByRole('button', { name: '发送' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('创建任务失败');
    expect(textbox).toHaveValue('保留这段任务');
    expect(mocks.navigate).not.toHaveBeenCalled();
    warnSpy.mockRestore();
  });
});
