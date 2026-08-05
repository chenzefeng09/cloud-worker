import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { Composer } from './Composer';
import { ComposerAddMenu } from './ComposerAddMenu';
import { useChatStore } from '../../stores/chatStore';
import type { InstalledSkill, McpServer } from '../../api/types';

vi.mock('../../hooks/useCanUploadAttachment', () => ({
  useCanUploadAttachment: () => ({
    checkFile: vi.fn().mockResolvedValue({ allowed: true }),
  }),
}));

// Composer fetches the model catalogue via useModelOptions; resolve empty so it
// falls back to MODEL_OPTIONS without making a real (jsdom-unresolvable) fetch.
vi.mock('../../api/client', () => ({
  api: { modelPreferences: { listModels: vi.fn().mockResolvedValue([]) } },
}));

function skill(name: string, over: Partial<InstalledSkill> = {}): InstalledSkill {
  return {
    id: `skl_${name}`,
    tenant_id: 'ten_1',
    name,
    description: null,
    owner: null,
    visibility: 'tenant',
    created_at: '2026-01-01',
    enabled_by_default: true,
    ...over,
  } as InstalledSkill;
}

function server(id: string, over: Partial<McpServer> = {}): McpServer {
  return {
    id,
    tenant_id: 'ten_1',
    name: `server-${id}`,
    transport: 'http',
    endpoint: 'https://example.invalid/mcp',
    command: null,
    env: {},
    enabled: true,
    discovered_tools: [{ name: 'echo', description: '回显' }],
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
    ...over,
  } as McpServer;
}

describe('composer "+" menu', () => {
  it('opens a compact primary menu and flies sections out to the right', async () => {
    const user = userEvent.setup();
    render(
      <ComposerAddMenu
        onPickFiles={vi.fn()}
        skills={{ skills: [skill('pdf-report')], selected: ['pdf-report'], onToggle: vi.fn() }}
        mcp={{ servers: [server('mcp_1')], selected: undefined, onToggle: vi.fn() }}
      />,
    );

    // The trigger names exactly the sections that are wired.
    const trigger = screen.getByRole('button', { name: '添加文件、技能或工具' });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    await user.click(trigger);

    // Primary menu: 3 rows, no flyout open yet.
    expect(screen.getByRole('menuitem', { name: /上传文件 \/ 图片/ })).toBeInTheDocument();
    const skillsHeader = screen.getByRole('menuitem', { name: /技能/ });
    const mcpHeader = screen.getByRole('menuitem', { name: /MCP 工具/ });
    expect(skillsHeader).toHaveAttribute('aria-expanded', 'false');
    expect(mcpHeader).toHaveAttribute('aria-expanded', 'false');

    // Click skills -> its flyout opens to the right; mcp stays closed.
    await user.click(skillsHeader);
    expect(skillsHeader).toHaveAttribute('aria-expanded', 'true');
    expect(mcpHeader).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('button', { name: 'pdf-report' })).toBeInTheDocument();

    // Switch to MCP -> skills flyout closes, MCP flyout opens (one at a time).
    await user.click(mcpHeader);
    expect(mcpHeader).toHaveAttribute('aria-expanded', 'true');
    expect(skillsHeader).toHaveAttribute('aria-expanded', 'false');
    const row = screen.getByRole('menuitemcheckbox', { name: /server-mcp_1/ });
    // Tools come from the persisted discovery result - never a live probe.
    expect(row).toHaveAttribute('title', '工具：echo');
    expect(row).toHaveTextContent('工具：echo');

    // Click MCP again -> collapses its own flyout (toggle).
    await user.click(mcpHeader);
    expect(mcpHeader).toHaveAttribute('aria-expanded', 'false');
  });

  it('file row fires onPickFiles and closes the whole menu (no flyout)', async () => {
    const user = userEvent.setup();
    const onPickFiles = vi.fn();
    render(
      <ComposerAddMenu
        onPickFiles={onPickFiles}
        skills={{ skills: [skill('a')], onToggle: vi.fn() }}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /上传文件 \/ 图片/ }));

    expect(onPickFiles).toHaveBeenCalledOnce();
    // Menu closed: primary rows gone.
    expect(screen.queryByRole('menuitem', { name: /技能/ })).not.toBeInTheDocument();
  });

  it('closes the whole menu on outside click (single outside-click owner)', async () => {
    const user = userEvent.setup();
    render(
      <div>
        <ComposerAddMenu
          onPickFiles={vi.fn()}
          skills={{ skills: [skill('a')], onToggle: vi.fn() }}
          mcp={{ servers: [server('mcp_1')], onToggle: vi.fn() }}
        />
        <button>outside</button>
      </div>,
    );
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    // Open the MCP flyout so we also prove it does not keep its own listener.
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));
    expect(screen.getByRole('menuitemcheckbox', { name: /server-mcp_1/ })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'outside' }));
    expect(screen.queryByRole('menuitem', { name: /技能/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitemcheckbox', { name: /server-mcp_1/ })).not.toBeInTheDocument();
  });

  it('Escape closes the menu', async () => {
    const user = userEvent.setup();
    render(
      <ComposerAddMenu
        onPickFiles={vi.fn()}
        skills={{ skills: [skill('a')], onToggle: vi.fn() }}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /技能/ }));
    expect(screen.getByRole('button', { name: 'a' })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menuitem', { name: /技能/ })).not.toBeInTheDocument();
  });

  it('falls back to the tenant `enabled` flag until the session picks its own set', async () => {
    const user = userEvent.setup();
    render(
      <ComposerAddMenu
        onPickFiles={vi.fn()}
        mcp={{
          servers: [server('mcp_on'), server('mcp_off', { enabled: false })],
          selected: undefined,
          onToggle: vi.fn(),
        }}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));

    expect(screen.getByRole('menuitemcheckbox', { name: /server-mcp_on/ })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemcheckbox', { name: /server-mcp_off/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });

  it('shows an explicit empty selection as all-off, not as "use the defaults"', async () => {
    const user = userEvent.setup();
    render(
      <ComposerAddMenu
        onPickFiles={vi.fn()}
        mcp={{ servers: [server('mcp_on')], selected: [], onToggle: vi.fn() }}
      />,
    );
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));

    expect(screen.getByRole('menuitemcheckbox', { name: /server-mcp_on/ })).toHaveAttribute(
      'aria-checked',
      'false',
    );
  });
});

describe('MCP selection -> chat store', () => {
  const sessionId = 'ses_mcp';
  const servers = [server('mcp_a'), server('mcp_b'), server('mcp_c', { enabled: false })];

  /** Mirrors ChatWindow's seed-then-toggle wiring. */
  function renderComposer() {
    return render(
      <Composer
        onSend={vi.fn()}
        onUpload={vi.fn()}
        skills={{ skills: [], onToggle: vi.fn() }}
        mcp={{
          servers,
          selected: useChatStore.getState().selectedMcpServerIds[sessionId],
          onToggle: (id) => {
            useChatStore
              .getState()
              .seedMcpServerIds(sessionId, servers.filter((s) => s.enabled).map((s) => s.id));
            useChatStore.getState().toggleMcpServer(sessionId, id);
          },
        }}
      />,
    );
  }

  beforeEach(() => {
    useChatStore.setState({ selectedMcpServerIds: {} });
  });

  it('leaves the session untouched until the user actually toggles', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));

    expect(useChatStore.getState().selectedMcpServerIds[sessionId]).toBeUndefined();
  });

  it('seeds the tenant defaults then removes the toggled server', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: /server-mcp_a/ }));

    // Seeded from `enabled` (a + b), then a flipped off.
    expect(useChatStore.getState().selectedMcpServerIds[sessionId]).toEqual(['mcp_b']);
  });

  it('turns an off-by-default server on without dropping the defaults', async () => {
    const user = userEvent.setup();
    renderComposer();
    await user.click(screen.getByRole('button', { name: /^添加/ }));
    await user.click(screen.getByRole('menuitem', { name: /MCP 工具/ }));
    await user.click(screen.getByRole('menuitemcheckbox', { name: /server-mcp_c/ }));

    expect(useChatStore.getState().selectedMcpServerIds[sessionId]).toEqual([
      'mcp_a',
      'mcp_b',
      'mcp_c',
    ]);
  });

  it('reaches an explicit [] - every server off - and never re-seeds after that', () => {
    const store = useChatStore.getState();
    store.seedMcpServerIds(sessionId, ['mcp_a']);
    store.toggleMcpServer(sessionId, 'mcp_a');
    expect(useChatStore.getState().selectedMcpServerIds[sessionId]).toEqual([]);

    // A later seed must not resurrect the defaults over the user's "all off".
    useChatStore.getState().seedMcpServerIds(sessionId, ['mcp_a', 'mcp_b']);
    expect(useChatStore.getState().selectedMcpServerIds[sessionId]).toEqual([]);
  });
});
