import { describe, expect, it } from 'vitest';

import { personLabel } from './format';

describe('personLabel', () => {
  it('shows the resolved display name, never the id', () => {
    expect(personLabel('usr_bob', 'Bob Li', 'usr_alice')).toBe('Bob Li');
  });

  it('marks the caller', () => {
    expect(personLabel('usr_alice', 'Alice Chen', 'usr_alice')).toBe('Alice Chen (you)');
    expect(personLabel('usr_alice', null, 'usr_alice')).toBe('You');
  });

  it('falls back to a neutral label when no name came back', () => {
    expect(personLabel('usr_bob', null)).toBe('Unknown user');
    // An erased author's placeholder is not a user id.
    expect(personLabel('erased-user', null)).toBe('Deleted user');
    expect(personLabel(null, null)).toBe('—');
  });
});
