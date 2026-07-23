import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Composer } from './Composer';

vi.mock('../../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({
    checkFile: vi.fn().mockResolvedValue({ allowed: true }),
  }),
}));

describe('Composer — session variant', () => {
  it('matches the simplified composer controls and exposes concise hover hints', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();

    render(
      <Composer
        onSend={onSend}
        onUpload={vi.fn()}
        model="deepseek-v4-flash"
        effort="medium"
        onModelChange={vi.fn()}
        onEffortChange={vi.fn()}
        executionMode="sandbox"
        onExecutionModeChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId('session-composer')).toHaveClass('rounded-[20px]');
    expect(screen.queryByRole('radiogroup', { name: '运行模式' })).not.toBeInTheDocument();

    // Only uploads are wired in this render, so the trigger names just that —
    // but it is still a menu, never a bare file-picker shortcut.
    const addButton = screen.getByRole('button', { name: '添加文件' });
    expect(addButton).toHaveClass('rounded-full', 'w-9', 'h-9');
    expect(addButton).toHaveAttribute('aria-haspopup', 'menu');
    expect(screen.getByRole('button', { name: '选择运行模式' })).toHaveTextContent('智能体');

    expect(screen.getByRole('tooltip', { name: '添加文件' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: '选择智能体或对话模式' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: '选择本次会话使用的模型' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: '调整模型的思考深度' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: '发送消息（Enter）' })).toBeInTheDocument();

    const sendButton = screen.getByRole('button', { name: '发送消息' });
    expect(sendButton).toHaveClass('rounded-full', 'w-10', 'h-10');
    expect(sendButton).toBeDisabled();

    await user.type(screen.getByRole('textbox'), '继续完善方案');
    expect(sendButton).toBeEnabled();
    await user.click(sendButton);
    expect(onSend).toHaveBeenCalledWith('继续完善方案', [], [], undefined);
  });

  it('hides the workspace picker — an existing session cannot be rebound', () => {
    render(<Composer onSend={vi.fn()} onUpload={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '选择工作区' })).not.toBeInTheDocument();
  });
});

describe('Composer — home variant', () => {
  it('renders the workspace picker and the create-task copy', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    const onTextChange = vi.fn();

    const { rerender } = render(
      <Composer
        variant="home"
        text=""
        onTextChange={onTextChange}
        placeholder="帮我安排明天的团队站会…"
        onSend={onSend}
        onUpload={vi.fn()}
        workspace={{
          value: 'ws_1',
          options: [{ id: 'ws_1', label: '工作区（内部）' }],
          onChange: vi.fn(),
        }}
        model="deepseek-v4-flash"
        onModelChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId('home-composer-toolbar')).not.toHaveClass('border-t', 'bg-paper');
    expect(screen.getByRole('button', { name: '选择工作区' })).toHaveTextContent('工作区（内部）');
    // Effort handler omitted → the control stays hidden.
    expect(screen.queryByRole('button', { name: '调整推理强度' })).not.toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: '发送并创建任务（Enter）' })).toBeInTheDocument();

    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();

    // Controlled text: typing goes out through onTextChange, not internal state.
    await user.type(screen.getByRole('textbox'), '帮');
    expect(onTextChange).toHaveBeenCalledWith('帮');

    rerender(
      <Composer
        variant="home"
        text="帮我规划明天"
        onTextChange={onTextChange}
        onSend={onSend}
        onUpload={vi.fn()}
        model="deepseek-v4-flash"
        onModelChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(onSend).toHaveBeenCalledWith('帮我规划明天', [], [], undefined);
  });
});
