import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { InputBar } from './InputBar';

vi.mock('../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({
    checkFile: vi.fn().mockResolvedValue({ allowed: true }),
  }),
}));

describe('InputBar action area', () => {
  it('matches the simplified composer controls and exposes concise hover hints', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();

    render(
      <InputBar
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

    const addButton = screen.getByRole('button', { name: '添加文件或启用技能' });
    expect(addButton).toHaveClass('rounded-full', 'w-9', 'h-9');
    expect(screen.getByRole('button', { name: '选择运行模式' })).toHaveTextContent('智能体');

    expect(screen.getByRole('tooltip', { name: '添加文件或启用技能' })).toBeInTheDocument();
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
});
