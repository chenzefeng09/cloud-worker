/**
 * Task board UI — card actions, the board page (filters, retry, delete), the
 * New task dialog's request body, and the lifecycle bar on the task view.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { Task } from '../../api/types';
import { useChatStore } from '../../stores/chatStore';
import { TaskCard } from './TaskCard';
import { CreateTaskDialog } from './CreateTaskDialog';
import { TaskBar } from './TaskBar';
import { TasksPage } from '../../pages/TasksPage';

const mocks = vi.hoisted(() => {
  class ApiError extends Error {
    status: number;
    code: string;
    reason?: string;
    constructor(status: number, code: string, detail: string, reason?: string) {
      super(detail);
      this.status = status;
      this.code = code;
      this.reason = reason;
    }
  }
  return {
    ApiError,
    list: vi.fn(),
    get: vi.fn(),
    create: vi.fn(),
    retry: vi.fn(),
    cancel: vi.fn(),
    deleteSession: vi.fn(),
    createSession: vi.fn(),
    largeCheck: vi.fn(),
    upload: vi.fn(),
    deleteFile: vi.fn(),
  };
});

vi.mock('../../api/client', () => ({
  ApiError: mocks.ApiError,
  api: {
    tasks: { list: mocks.list, get: mocks.get, create: mocks.create, retry: mocks.retry },
    sessions: { cancel: mocks.cancel, delete: mocks.deleteSession, create: mocks.createSession },
    attachments: { largeCheck: mocks.largeCheck, upload: mocks.upload },
    artifacts: { deleteFile: mocks.deleteFile },
  },
}));
vi.mock('../shell/PageHead', () => ({
  PageHead: ({ actions }: { actions?: React.ReactNode }) => <div>{actions}</div>,
}));
vi.mock('../../lib/workspaceContext', () => ({
  useWorkspace: () => ({ activeWorkspace: null, workspaces: [{ id: 'ws_1', name: 'Legal' }] }),
}));
vi.mock('../../hooks/useInstalledSkills', () => ({
  useInstalledSkills: () => [
    { name: 'pdf', enabled_by_default: true },
    { name: 'xlsx', enabled_by_default: false },
  ],
}));
vi.mock('../../hooks/useMcpServers', () => ({
  useMcpServers: () => ({ servers: [{ id: 'mcp_a', name: 'files', enabled: true }] }),
}));
vi.mock('../../hooks/useModelOptions', () => ({
  useModelOptions: () => ({ options: [{ id: 'm-big', label: 'Big model' }] }),
}));
vi.mock('../../hooks/useDefaultModel', () => ({
  useDefaultModel: () => ({ modelId: 'm-default' }),
}));

const task = (over: Partial<Task> = {}): Task => ({
  id: 'ses_1',
  title: 'Summarise contracts',
  kind: 'task',
  status: 'failed',
  workspace_id: null,
  instructions: 'Read the contracts and list renewal dates',
  options: {},
  run_count: 1,
  latest_run: {
    id: 'run_1',
    status: 'FAILED',
    source: 'user',
    app_id: null,
    created_at: '2026-10-30T10:00:00Z',
    started_at: '2026-10-30T10:00:01Z',
    finished_at: '2026-10-30T10:01:00Z',
    result_summary: null,
    error_message: 'Sandbox crashed',
    total_tokens: 1234,
  },
  created_at: '2026-10-30T10:00:00Z',
  updated_at: '2026-10-30T10:01:00Z',
  ...over,
});

const counts = { queued: 0, running: 1, needs_input: 0, completed: 2, failed: 1, cancelled: 0, idle: 0 };

beforeEach(() => {
  Object.values(mocks).forEach((m) => {
    if (typeof m === 'function' && 'mockReset' in m) (m as ReturnType<typeof vi.fn>).mockReset();
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ─── TaskCard ────────────────────────────────────────────────────────────────

describe('TaskCard', () => {
  const handlers = () => ({
    onOpen: vi.fn(), onRetry: vi.fn(), onCancel: vi.fn(), onDelete: vi.fn(),
  });

  it('shows the failure and offers Retry and Delete, not Cancel', () => {
    render(<TaskCard task={task()} {...handlers()} />);
    expect(screen.getByText('Failed')).toBeInTheDocument();
    expect(screen.getByText('Sandbox crashed')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
  });

  it('offers only Cancel while running', async () => {
    const h = handlers();
    render(<TaskCard task={task({ status: 'running', latest_run: { ...task().latest_run!, status: 'RUNNING', finished_at: null } })} {...h} />);
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(h.onCancel).toHaveBeenCalled();
  });

  it('offers Start for a task whose first run never launched', () => {
    render(<TaskCard task={task({ status: 'idle', run_count: 0, latest_run: null })} {...handlers()} />);
    expect(screen.getByRole('button', { name: 'Start' })).toBeInTheDocument();
    expect(screen.getByText('No runs yet')).toBeInTheDocument();
  });

  it('does not offer a retry for a chat that never ran', () => {
    render(<TaskCard task={task({ kind: 'chat', status: 'idle', run_count: 0, latest_run: null })} {...handlers()} />);
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull();
  });

  it('flags unattended and needs-input tasks', () => {
    render(<TaskCard task={task({ status: 'needs_input', options: { hitl: false } })} {...handlers()} />);
    expect(screen.getByText('Unattended')).toBeInTheDocument();
    expect(screen.getByText(/Waiting for your answer/)).toBeInTheDocument();
  });
});

// ─── TasksPage ───────────────────────────────────────────────────────────────

function renderBoard(path = '/tasks') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <TasksPage />
    </MemoryRouter>,
  );
}

describe('TasksPage', () => {
  it('lists tasks with per-tab counts', async () => {
    mocks.list.mockResolvedValue({ items: [task()], next_cursor: null, counts });
    renderBoard();
    expect(await screen.findByText('Summarise contracts')).toBeInTheDocument();
    const tabs = screen.getByRole('tablist', { name: 'Task status' });
    expect(within(tabs).getByRole('tab', { name: /All/ })).toHaveTextContent('4');
    expect(within(tabs).getByRole('tab', { name: /Active/ })).toHaveTextContent('1');
  });

  it('filters by the selected status tab', async () => {
    mocks.list.mockResolvedValue({ items: [task()], next_cursor: null, counts });
    renderBoard();
    await screen.findByText('Summarise contracts');
    await userEvent.click(screen.getByRole('tab', { name: /Failed/ }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.objectContaining({ status: ['failed'] })),
    );
    // A task stopped on purpose is not a failure: it has its own tab.
    await userEvent.click(screen.getByRole('tab', { name: /Cancelled/ }));
    await waitFor(() =>
      expect(mocks.list).toHaveBeenLastCalledWith(expect.objectContaining({ status: ['cancelled'] })),
    );
  });

  it('retries a failed task and updates its card in place', async () => {
    mocks.list.mockResolvedValue({ items: [task()], next_cursor: null, counts });
    mocks.retry.mockResolvedValue({
      task: task({ status: 'queued', run_count: 2, latest_run: { ...task().latest_run!, status: 'PENDING', error_message: null } }),
      run_id: 'run_2',
      rewound_from: 2,
    });
    renderBoard();
    await screen.findByText('Summarise contracts');
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(mocks.retry).toHaveBeenCalledWith('ses_1');
    expect(await screen.findByText('Queued')).toBeInTheDocument();
    expect(screen.getByText(/is running again/)).toBeInTheDocument();
  });

  it('deletes a task only after confirmation', async () => {
    mocks.list.mockResolvedValue({ items: [task()], next_cursor: null, counts });
    mocks.deleteSession.mockResolvedValue(undefined);
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true);
    renderBoard();
    await screen.findByText('Summarise contracts');
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(mocks.deleteSession).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(mocks.deleteSession).toHaveBeenCalledWith('ses_1');
    await waitFor(() => expect(screen.queryByText('Summarise contracts')).toBeNull());
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it('shows the empty state with a New task call to action', async () => {
    mocks.list.mockResolvedValue({ items: [], next_cursor: null, counts: {} });
    renderBoard();
    expect(await screen.findByText('No tasks yet')).toBeInTheDocument();
  });
});

// ─── CreateTaskDialog ────────────────────────────────────────────────────────

describe('CreateTaskDialog', () => {
  it('sends the instructions with defaults left to the server', async () => {
    const onCreated = vi.fn();
    const onClose = vi.fn();
    mocks.create.mockResolvedValue({ task: task({ status: 'queued' }), run_id: 'run_1' });
    render(<CreateTaskDialog open onClose={onClose} onCreated={onCreated} />);

    const start = screen.getByRole('button', { name: 'Start task' });
    expect(start).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/What should the agent do/), 'Summarise the attached contracts');
    await userEvent.click(start);

    expect(mocks.create).toHaveBeenCalledWith({
      instructions: 'Summarise the attached contracts',
      model: 'm-default',
      hitl: true,
    });
    expect(onCreated).toHaveBeenCalledWith(expect.objectContaining({ id: 'ses_1' }));
    expect(onClose).toHaveBeenCalled();
  });

  it('sends an explicit tool selection and unattended mode once touched', async () => {
    mocks.create.mockResolvedValue({ task: task(), run_id: 'run_1' });
    render(<CreateTaskDialog open onClose={vi.fn()} onCreated={vi.fn()} />);
    await userEvent.type(screen.getByLabelText(/What should the agent do/), 'Go');
    await userEvent.click(screen.getByRole('button', { name: /Tools/ }));
    await userEvent.click(screen.getByRole('checkbox', { name: 'pdf' })); // default on → off
    await userEvent.click(screen.getByRole('checkbox', { name: /Ask me before sensitive actions/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Start task' }));
    expect(mocks.create).toHaveBeenCalledWith(expect.objectContaining({
      skills: [],
      hitl: false,
    }));
    expect(mocks.create.mock.calls[0][0]).not.toHaveProperty('mcp_server_ids');
  });

  it('reports a saved-but-not-started task instead of losing it', async () => {
    const onCreated = vi.fn();
    mocks.create.mockRejectedValue(
      new mocks.ApiError(503, 'UNKNOWN', 'Failed to start run', 'task_not_started'),
    );
    render(<CreateTaskDialog open onClose={vi.fn()} onCreated={onCreated} />);
    await userEvent.type(screen.getByLabelText(/What should the agent do/), 'Go');
    await userEvent.click(screen.getByRole('button', { name: 'Start task' }));
    expect(onCreated).toHaveBeenCalledWith(null, 'Failed to start run');
  });

  it('keeps the dialog open with the error when nothing was created', async () => {
    const onCreated = vi.fn();
    mocks.create.mockRejectedValue(new mocks.ApiError(502, 'UNKNOWN', 'Failed to create task'));
    render(<CreateTaskDialog open onClose={vi.fn()} onCreated={onCreated} />);
    await userEvent.type(screen.getByLabelText(/What should the agent do/), 'Go');
    await userEvent.click(screen.getByRole('button', { name: 'Start task' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Failed to create task');
    expect(onCreated).not.toHaveBeenCalled();
  });

  it('deletes the draft session of an abandoned upload', async () => {
    mocks.createSession.mockResolvedValue({ id: 'ses_draft' });
    mocks.largeCheck.mockResolvedValue({ allowed: true, is_large: false, reason: null });
    mocks.upload.mockResolvedValue({ id: 'att_1', filename: 'a.pdf', size_bytes: 2048 });
    mocks.deleteSession.mockResolvedValue(undefined);
    const onClose = vi.fn();
    render(<CreateTaskDialog open onClose={onClose} onCreated={vi.fn()} />);
    await userEvent.upload(screen.getByLabelText('Attach files'), new File(['x'], 'a.pdf'));
    expect(await screen.findByText('a.pdf')).toBeInTheDocument();
    expect(mocks.createSession).toHaveBeenCalledWith('New task', { kind: 'task' }, undefined);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(mocks.deleteSession).toHaveBeenCalledWith('ses_draft');
    expect(onClose).toHaveBeenCalled();
  });
});

// ─── TaskBar ─────────────────────────────────────────────────────────────────

describe('TaskBar', () => {
  function renderBar() {
    return render(
      <MemoryRouter>
        <TaskBar sessionId="ses_1" />
      </MemoryRouter>,
    );
  }

  it('renders nothing for an ordinary chat', async () => {
    mocks.get.mockResolvedValue(task({ kind: 'chat' }));
    renderBar();
    await waitFor(() => expect(mocks.get).toHaveBeenCalled());
    expect(screen.queryByTestId('task-bar')).toBeNull();
  });

  it('retries a failed task, clearing the old attempt from the transcript', async () => {
    useChatStore.setState({
      messages: {
        ses_1: [
          { id: 'u', session_id: 'ses_1', role: 'USER', content: 'go', tool_calls: null, run_id: null, sequence: 1, created_at: '' },
          { id: 'a', session_id: 'ses_1', role: 'SYSTEM', content: 'Task failed', tool_calls: null, run_id: 'run_1', sequence: 2, created_at: '' },
        ],
      },
    });
    const queued = task({ status: 'queued', run_count: 2 });
    // First load shows the failure; later refreshes see the new attempt.
    mocks.get.mockResolvedValueOnce(task()).mockResolvedValue(queued);
    mocks.retry.mockResolvedValue({ task: queued, run_id: 'run_2', rewound_from: 2 });
    renderBar();
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(mocks.retry).toHaveBeenCalledWith('ses_1');
    expect(useChatStore.getState().messages['ses_1'].map((m) => m.id)).toEqual(['u']);
    expect(useChatStore.getState().streamingSessions['ses_1']).toBe(true);
    expect(await screen.findByText(/Attempt 2/)).toBeInTheDocument();
  });

  it('asks before re-running a completed task', async () => {
    mocks.get.mockResolvedValue(task({ status: 'completed', latest_run: { ...task().latest_run!, status: 'COMPLETED', error_message: null } }));
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderBar();
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(mocks.retry).not.toHaveBeenCalled();
  });
});
