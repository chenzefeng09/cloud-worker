import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { Composer } from './Composer';
import type { ModelCatalogProvider } from '../../api/types';
import { NO_MODEL_CONFIGURED } from '../../lib/modelOptions';

vi.mock('../../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({
    checkFile: vi.fn().mockResolvedValue({ allowed: true }),
  }),
}));

// Hoisted mock so the factory can reference it. Default resolves to a tenant
// with one provider (DEEPSEEK_PROVIDER). Per-test overrides below.
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
  apiMocks.listModels.mockResolvedValue([DEEPSEEK_PROVIDER]);
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
      />,
    );

    expect(screen.getByTestId('session-composer')).toHaveClass('rounded-[20px]');
    // The execution-mode picker was removed: every run uses the sandbox agent.
    expect(screen.queryByRole('button', { name: /execution mode|run mode/i })).not.toBeInTheDocument();

    // Only uploads are wired in this render, so the trigger names just that -
    // but it is still a menu, never a bare file-picker shortcut.
    const addButton = screen.getByRole('button', { name: 'Add files' });
    expect(addButton).toHaveClass('rounded-full', 'w-9', 'h-9');
    expect(addButton).toHaveAttribute('aria-haspopup', 'menu');

    expect(screen.getByRole('tooltip', { name: 'Add files' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: 'Choose the model for this session' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: 'Adjust how deeply the model thinks' })).toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: 'Send message (Enter)' })).toBeInTheDocument();

    const sendButton = screen.getByRole('button', { name: 'Send message' });
    expect(sendButton).toHaveClass('rounded-full', 'w-10', 'h-10');
    expect(sendButton).toBeDisabled();

    await user.type(screen.getByRole('textbox'), 'Keep refining the plan');
    expect(sendButton).toBeEnabled();
    await user.click(sendButton);
    expect(onSend).toHaveBeenCalledWith('Keep refining the plan', [], [], undefined);
  });

  it('hides the workspace picker - an existing session cannot be rebound', async () => {
    render(<Composer onSend={vi.fn()} onUpload={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Select workspace' })).not.toBeInTheDocument();
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
        placeholder="Plan tomorrow’s team stand-up…"
        onSend={onSend}
        onUpload={vi.fn()}
        workspace={{
          value: 'ws_1',
          options: [{ id: 'ws_1', label: 'Workspace (Internal)' }],
          onChange: vi.fn(),
        }}
        model="deepseek-v4-flash"
        onModelChange={vi.fn()}
      />,
    );

    expect(screen.getByTestId('home-composer-toolbar')).not.toHaveClass('border-t', 'bg-paper');
    expect(screen.getByRole('button', { name: 'Select workspace' })).toHaveTextContent('Workspace (Internal)');
    // Effort handler omitted -> the control stays hidden.
    expect(screen.queryByRole('button', { name: 'Adjust reasoning effort' })).not.toBeInTheDocument();
    expect(screen.getByRole('tooltip', { name: 'Send and create task (Enter)' })).toBeInTheDocument();

    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();

    // Controlled text: typing goes out through onTextChange, not internal state.
    await user.type(screen.getByRole('textbox'), 'P');
    expect(onTextChange).toHaveBeenCalledWith('P');

    rerender(
      <Composer
        variant="home"
        text="Plan my day tomorrow"
        onTextChange={onTextChange}
        onSend={onSend}
        onUpload={vi.fn()}
        model="deepseek-v4-flash"
        onModelChange={vi.fn()}
      />,
    );
    await user.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledWith('Plan my day tomorrow', [], [], undefined);
  });

  it('shows a clear empty workspace state without a dead dropdown arrow', async () => {
    render(
      <Composer
        variant="home"
        onSend={vi.fn()}
        workspace={{
          value: '',
          options: [],
          onChange: vi.fn(),
          disabled: true,
          emptyLabel: 'No workspaces',
        }}
      />,
    );

    const workspace = screen.getByRole('button', { name: 'Select workspace' });
    expect(workspace).toBeDisabled();
    expect(workspace).toHaveTextContent('No workspaces');
    expect(workspace).not.toHaveTextContent('▾');
    await waitFor(() => expect(apiMocks.listModels).toHaveBeenCalled());
  });

  it('keeps the draft when the home-page send is rejected', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn().mockResolvedValue(false);

    render(<Composer variant="home" onSend={onSend} />);

    const textbox = screen.getByRole('textbox');
    await user.type(textbox, 'do not lose this long task');
    await user.click(screen.getByRole('button', { name: 'Send' }));

    await waitFor(() => expect(onSend).toHaveBeenCalled());
    expect(textbox).toHaveValue('do not lose this long task');
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
    const trigger = screen.getByRole('button', { name: 'Select model' });
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
    const trigger = screen.getByRole('button', { name: 'Select model' });
    await waitFor(() => expect(trigger).toHaveTextContent('V4 Flash'));
    // The hook logs the failure (and still falls back) rather than throwing.
    expect(warnSpy).toHaveBeenCalledWith('Failed to load model list', expect.any(Error));
    warnSpy.mockRestore();
  });

  it('offers no model when the tenant has no provider, and says why', async () => {
    const user = userEvent.setup();
    const onModelChange = vi.fn();
    const onSend = vi.fn();
    apiMocks.listModels.mockResolvedValue([]);

    render(
      <Composer onSend={onSend} model="deepseek-v4-flash" onModelChange={onModelChange} />,
    );

    // Not the built-in 'V4 Flash': nothing the tenant could run is on offer.
    const trigger = screen.getByRole('button', { name: 'Select model' });
    await waitFor(() => expect(trigger).toHaveTextContent('No model available'));
    expect(screen.getByRole('tooltip', { name: NO_MODEL_CONFIGURED })).toBeInTheDocument();
    await user.click(trigger);
    expect(screen.queryByRole('menuitemradio')).not.toBeInTheDocument();
    expect(onModelChange).not.toHaveBeenCalled();

    // Sending stays possible: the server's refusal explains it as well, and an
    // administrator may add a provider while this page is open.
    await user.type(screen.getByRole('textbox'), 'hello');
    await user.click(screen.getByRole('button', { name: 'Send message' }));
    expect(onSend).toHaveBeenCalled();
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

    const trigger = screen.getByRole('button', { name: 'Select model' });
    // Once the catalogue resolves, the fictional 'deepseek-v4-flash' is no
    // longer a valid option, so the composer snaps to the first real model.
    await waitFor(() => expect(trigger).toHaveTextContent('deepseek-chat (DeepSeek)'));
  });
});
