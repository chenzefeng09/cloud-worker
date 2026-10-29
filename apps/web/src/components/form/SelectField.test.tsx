import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { SelectField } from './SelectField';

const OPTIONS = [
  { value: 'a', label: 'Poem generator' },
  { value: 'b', label: 'Contract review assistant' },
];

describe('SelectField', () => {
  it('opens the popover from anywhere in the bordered box, not just the ▾ glyph', async () => {
    const user = userEvent.setup();
    render(<SelectField label="App" value="" onChange={vi.fn()} options={OPTIONS} placeholder="Select an app" />);

    // The trigger is the whole box: its accessible name is the field label and
    // the placeholder text lives *inside* it, so clicking the label text (i.e.
    // the middle of the box, far from the arrow) must open the menu.
    const trigger = screen.getByRole('button', { name: /App/ });
    expect(trigger).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger).toHaveAttribute('aria-expanded', 'false');

    await user.click(screen.getByText('Select an app'));
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('menu')).toBeInTheDocument();
  });

  it('opens with the keyboard and reports the chosen option', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<SelectField label="App" value="" onChange={onChange} options={OPTIONS} />);

    await user.tab();
    expect(screen.getByRole('button', { name: /App/ })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('menu')).toBeInTheDocument();

    await user.click(screen.getByRole('menuitemradio', { name: 'Contract review assistant' }));
    expect(onChange).toHaveBeenCalledWith('b');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('is not clickable while disabled', async () => {
    const user = userEvent.setup();
    render(<SelectField label="App" value="" onChange={vi.fn()} options={OPTIONS} disabled />);

    const trigger = screen.getByRole('button', { name: /App/ });
    expect(trigger).toBeDisabled();
    await user.click(trigger);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
