import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Dropdown } from './Dropdown';

describe('Dropdown', () => {
  it('keeps workspace labels on one line and sizes the menu to its content', async () => {
    const user = userEvent.setup();
    render(
      <Dropdown
        value="ws_internal"
        options={[
          { id: 'ws_internal', label: 'Workspace · Internal' },
          { id: 'ws_contract', label: 'Contract review · Internal' },
        ]}
        onChange={vi.fn()}
        title="Select workspace"
        align="left"
      />,
    );

    const trigger = screen.getByTitle('Select workspace');
    expect(trigger).toHaveClass('whitespace-nowrap');
    await user.click(trigger);

    const menu = screen.getByRole('menu');
    expect(menu).toHaveClass('w-max', 'max-w-[320px]');
    for (const option of screen.getAllByRole('menuitemradio')) {
      expect(option).toHaveClass('whitespace-nowrap', 'text-ellipsis');
    }
  });
});
