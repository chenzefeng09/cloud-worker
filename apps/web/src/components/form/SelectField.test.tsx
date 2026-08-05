import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { SelectField } from './SelectField';

const OPTIONS = [
  { value: 'a', label: '诗歌生成器' },
  { value: 'b', label: '合同审查助手' },
];

describe('SelectField', () => {
  it('opens the popover from anywhere in the bordered box, not just the ▾ glyph', async () => {
    const user = userEvent.setup();
    render(<SelectField label="应用" value="" onChange={vi.fn()} options={OPTIONS} placeholder="请选择应用" />);

    // The trigger is the whole box: its accessible name is the field label and
    // the placeholder text lives *inside* it, so clicking the label text (i.e.
    // the middle of the box, far from the arrow) must open the menu.
    const trigger = screen.getByRole('button', { name: /应用/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await user.click(screen.getByText('请选择应用'));
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('opens with the keyboard and reports the chosen option', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectField label="应用" value="" onChange={onChange} options={OPTIONS} />);

    await user.tab();
    expect(screen.getByRole('button', { name: /应用/ })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await user.click(screen.getByRole('menuitemradio', { name: '合同审查助手' }));
    expect(onChange).toHaveBeenCalledWith('b');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('is not clickable while disabled', async () => {
    const user = userEvent.setup();
    render(<SelectField label="应用" value="" onChange={vi.fn()} options={OPTIONS} disabled />);

    const trigger = screen.getByRole('button', { name: /应用/ });
    expect(trigger).toBeDisabled();
    await user.click(trigger);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
