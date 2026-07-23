import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { Composer } from './Composer';
import type { ModelCatalogProvider } from '../../api/types';

vi.mock('../../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({
    checkFile: vi.fn().mockResolvedValue({ allowed: true }),
  }),
}));

// Hoisted mock so the factory can reference it. Default resolves to [] which
// makes useModelOptions fall back to MODEL_OPTIONS - preserving the behaviour
// the existing structural tests were written against. Per-test overrides below.
const apiMocks = vi.hoisted(() => ({
  listModels: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  api: { modelPreferences: { listModels: apiMocks.listModels } },
}));

// A DeepSeek provider advertising the two real model ids the backend catalogue
// returns (matches what the admin model-management page configures).
const DEEPSEEK_PROVIDER: ModelCatalogProvider = {
  id: 'provider-deepseek',
  name: 'DeepSeek',
  kind: 'deepseek',
  enabled: true,
  models: ['deepseek-chat', 'deepseek-reasoner'],
  capabilities: { model_type_map: { llm: ['deepseek-chat', 'deepseek-reasoner'] } },
};

beforeEach(() => {
  apiMocks.listModels.mockReset();
  apiMocks.listModels.mockResolvedValue([]); // default: no providers -> MODEL_OPTIONS
});

describe('Composer - session variant', () => {
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

    // Only uploads are wired in this render, so the trigger names just that -
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

  it('hides the workspace picker - an existing session cannot be rebound', async () => {
    render(<Composer onSend={vi.fn()} onUpload={vi.fn()} />);
    expect(screen.queryByRole('button', { name: '选择工作区' })).not.toBeInTheDocument();
    // Flush the useModelOptions catalogue fetch so its state update settles
    // within act() instead of leaking out of this (otherwise sync) test.
    await waitFor(() => expect(apiMocks.listModels).toHaveBeenCalled());
  });
});

describe('Composer - home variant', () => {
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
    // Effort handler omitted -> the control stays hidden.
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

describe('Composer - model catalogue', () => {
  it('shows real model names from the backend catalogue', async () => {
    const user = userEvent.setup();
    apiMocks.listModels.mockResolvedValue([DEEPSEEK_PROVIDER]);

    render(
      <Composer onSend={vi.fn()} model="deepseek-chat" onModelChange={vi.fn()} />,
    );

    // The trigger reflects the selected real model once the catalogue resolves.
    const trigger = screen.getByRole('button', { name: '选择模型' });
    await waitFor(() => expect(trigger).toHaveTextContent('deepseek-chat (DeepSeek)'));

    // Open the menu and confirm both real models are offered (not the
    // fictional V4 Pro / V4 Flash).
    await user.click(trigger);
    expect(
      await screen.findByRole('menuitemradio', { name: 'deepseek-chat (DeepSeek)' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitemradio', { name: 'deepseek-reasoner (DeepSeek)' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('menuitemradio', { name: 'V4 Pro' })).not.toBeInTheDocument();
  });

  it('falls back to the built-in MODEL_OPTIONS when the catalogue request fails', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    apiMocks.listModels.mockRejectedValue(new Error('boom'));

    render(
      <Composer onSend={vi.fn()} model="deepseek-v4-flash" onModelChange={vi.fn()} />,
    );

    // 'deepseek-v4-flash' resolves to the 'V4 Flash' label in MODEL_OPTIONS.
    const trigger = screen.getByRole('button', { name: '选择模型' });
    await waitFor(() => expect(trigger).toHaveTextContent('V4 Flash'));
    // The hook logs the failure (and still falls back) rather than throwing.
    expect(warnSpy).toHaveBeenCalledWith('加载模型列表失败', expect.any(Error));
    warnSpy.mockRestore();
  });

  it('auto-corrects a fictional model id to the first real catalogue model', async () => {
    apiMocks.listModels.mockResolvedValue([DEEPSEEK_PROVIDER]);

    // Stateful harness so onModelChange actually updates the prop - mirroring
    // how HomePage/ChatWindow wire `model`/`onModelChange` to real state.
    function Harness() {
      const [model, setModel] = useState('deepseek-v4-flash');
      return <Composer onSend={vi.fn()} model={model} onModelChange={setModel} />;
    }
    render(<Harness />);

    const trigger = screen.getByRole('button', { name: '选择模型' });
    // Once the catalogue resolves, the fictional 'deepseek-v4-flash' is no
    // longer a valid option, so the composer snaps to the first real model.
    await waitFor(() => expect(trigger).toHaveTextContent('deepseek-chat (DeepSeek)'));
  });
});
