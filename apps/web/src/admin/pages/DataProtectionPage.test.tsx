/**
 * DataProtectionPage — the residency card is a GDPR (Chapter V) view: it
 * classifies the hosting region against the EU/EEA and never repeats the
 * upstream service's PIPL "compliant" verdict or restricted-destination list.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DialogProvider } from '../components/dialogs';
import { useAdminStore } from '../../stores/adminStore';
import { ADEQUACY_DECISIONS_URL } from '../../components/privacy/eea';

const dp = vi.hoisted(() => ({
  maskingRules: vi.fn(),
  dataResidency: vi.fn(),
  crossBorderCheck: vi.fn(),
  consentList: vi.fn(),
}));

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>();
  return {
    ...actual,
    api: {
      admin: {
        tenants: { list: () => Promise.resolve({ items: [] }) },
        dataProtection: {
          maskingRules: dp.maskingRules,
          dataResidency: dp.dataResidency,
          crossBorderCheck: dp.crossBorderCheck,
          destruction: { get: vi.fn(), create: vi.fn(), approve: vi.fn() },
          consent: { list: dp.consentList, create: vi.fn() },
        },
      },
    },
  };
});

import { DataProtectionPage } from './DataProtectionPage';

const PIPL_RESIDENCY = {
  primary_region: 'CN',
  restricted_destinations: ['KP', 'IR'],
  policy: 'PIPL compliant — personal information stays in mainland China',
};

function renderPage() {
  return render(
    <DialogProvider>
      <DataProtectionPage />
    </DialogProvider>,
  );
}

async function transfersCard() {
  const title = await screen.findByText('International transfers (GDPR)');
  return title.parentElement as HTMLElement;
}

beforeEach(() => {
  useAdminStore.setState({ tenants: [{ id: 'tnt_x', name: 'X' } as never], selectedTenantId: null });
  dp.maskingRules.mockResolvedValue({ rules: [] });
  dp.consentList.mockResolvedValue([]);
});

afterEach(() => {
  vi.clearAllMocks();
});

describe('DataProtectionPage — international transfers (GDPR)', () => {
  it('flags a PIPL profile instead of presenting it as compliance', async () => {
    dp.dataResidency.mockResolvedValue(PIPL_RESIDENCY);
    renderPage();
    const card = await transfersCard();

    expect(await within(card).findByText('CN · outside the EU/EEA')).toBeInTheDocument();
    expect(within(card).getByRole('note')).toHaveTextContent(/China \(PIPL\) residency profile/);
    expect(within(card).getByRole('link', { name: /EU adequacy decisions/ })).toHaveAttribute('href', ADEQUACY_DECISIONS_URL);

    // The upstream verdict and its PIPL restricted list aren't shown anywhere.
    expect(screen.queryByText(/PIPL compliant/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\bKP\b/)).not.toBeInTheDocument();
    expect(screen.queryByText(/compliant/i)).not.toBeInTheDocument();
    // The cross-border check says its rules are PIPL's, not a GDPR decision.
    expect(screen.getByText(/built-in PIPL rules/)).toBeInTheDocument();
  });

  it('shows an EU/EEA region without warnings', async () => {
    dp.dataResidency.mockResolvedValue({ primary_region: 'DE', restricted_destinations: [], policy: 'Default' });
    renderPage();
    const card = await transfersCard();

    expect(await within(card).findByText('DE · inside the EU/EEA')).toBeInTheDocument();
    expect(within(card).queryByRole('note')).not.toBeInTheDocument();
    expect(screen.queryByText(/built-in PIPL rules/)).not.toBeInTheDocument();
    expect(within(card).getByText(/Standard Contractual Clauses/)).toBeInTheDocument();
  });

  it('explains that hosting outside the EU/EEA is itself a transfer', async () => {
    dp.dataResidency.mockResolvedValue({ primary_region: 'us', policy: '' });
    renderPage();
    const card = await transfersCard();

    expect(await within(card).findByText('us · outside the EU/EEA')).toBeInTheDocument();
    expect(within(card).getByRole('note')).toHaveTextContent(/hosted outside the EU\/EEA/);
  });

  it("doesn't guess where a cloud region name is", async () => {
    dp.dataResidency.mockResolvedValue({ primary_region: 'eu-west-2' });
    renderPage();
    const card = await transfersCard();
    expect(await within(card).findByText('eu-west-2 · check which country this region is in')).toBeInTheDocument();
    expect(within(card).queryByRole('note')).not.toBeInTheDocument();
  });

  it('labels cross-border check results as the service rules', async () => {
    dp.dataResidency.mockResolvedValue(PIPL_RESIDENCY);
    dp.crossBorderCheck.mockResolvedValue({ allowed: false, decision: 'REVIEW', risks: ['r1'], required_actions: ['a1'] });
    renderPage();
    await transfersCard();

    await userEvent.click(screen.getByRole('button', { name: '+ Register cross-border transfer' }));
    const dialog = screen.getByRole('dialog', { name: 'Register cross-border transfer' });
    await userEvent.type(within(dialog).getByPlaceholderText('US'), 'US');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Check' }));

    await waitFor(() =>
      expect(dp.crossBorderCheck).toHaveBeenCalledWith({ destination_country: 'US', data_categories: [] }, undefined),
    );
    expect((await screen.findByText('Allowed (service rules)')).nextElementSibling).toHaveTextContent('No');
    expect(screen.getByText('Decision (service rules)').nextElementSibling).toHaveTextContent('REVIEW');
  });
});
