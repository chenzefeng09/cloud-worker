/**
 * ScheduleCreateDialog — unit tests.
 *
 * Two layers:
 *  1. CRON_PRESETS mapping that drives the schedule preset chip UI.
 *  2. The trigger type-switch + webhook create flow (mocked client):
 *     - a required-file App disables the 定时 tab and defaults to Webhook (收口);
 *     - creating a webhook POSTs field_mapping and flips to the one-time success
 *       view revealing the webhook URL + plaintext secret.
 *
 * The file stays `.ts` (renamed from SchedulesPage.test.ts by the in-flight
 * refactor), so the component is rendered via React.createElement rather than
 * JSX. i18n is mocked to an identity `t` so assertions match on the raw keys.
 */
import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CRON_PRESETS, ScheduleCreateDialog } from './ScheduleCreateDialog';
import type { App } from '../../api/types';

const mocks = vi.hoisted(() => ({
  create: vi.fn(),
  rotateSecret: vi.fn(),
}));

vi.mock('../../api/client', () => ({
  api: { schedules: { create: mocks.create, rotateSecret: mocks.rotateSecret } },
  ApiError: class ApiError extends Error {},
}));

vi.mock('../../i18n', () => ({
  useTranslation: () => ({ t: (k: string) => k, lang: 'zh', setLang: () => {} }),
}));

// ─── Fixtures ─────────────────────────────────────────────────────────────────

/** App with a single text field — schedule-capable (no file input). */
function appWithTextField(): App {
  return {
    id: 'app_1',
    name: 'Test App',
    current_version_id: 'v1',
    versions: [
      {
        id: 'v1',
        status: 'published',
        input_schema: [{ key: 'topic', label: 'Topic', type: 'text', required: true }],
      },
    ],
  } as unknown as App;
}

/** App whose published schema has a REQUIRED file field — schedule-incapable. */
function appWithRequiredFile(): App {
  return {
    id: 'app_2',
    name: 'File App',
    current_version_id: 'v2',
    versions: [
      {
        id: 'v2',
        status: 'published',
        input_schema: [{ key: 'doc', label: 'Doc', type: 'file', required: true }],
      },
    ],
  } as unknown as App;
}

/** App with a from-event-able text field plus a file field (always from-event). */
function appWithTextAndFile(): App {
  return {
    id: 'app_3',
    name: 'Mixed App',
    current_version_id: 'v3',
    versions: [
      {
        id: 'v3',
        status: 'published',
        input_schema: [
          { key: 'topic', label: 'Topic', type: 'text', required: false },
          { key: 'doc', label: 'Doc', type: 'file', required: false },
        ],
      },
    ],
  } as unknown as App;
}

function renderDialog(app: App, onCreated = vi.fn(), onClose = vi.fn()) {
  return render(createElement(ScheduleCreateDialog, { app, onCreated, onClose }));
}

beforeEach(() => {
  mocks.create.mockReset();
  mocks.rotateSecret.mockReset();
});

// ─── CRON_PRESETS mapping ─────────────────────────────────────────────────────

describe('CRON_PRESETS', () => {
  it('has exactly 4 presets: daily, weekly, hourly, custom', () => {
    const keys = CRON_PRESETS.map((p) => p.key);
    expect(keys).toEqual(['daily', 'weekly', 'hourly', 'custom']);
  });

  it('non-custom presets have a 5-field cron expression', () => {
    for (const p of CRON_PRESETS.filter((p) => p.key !== 'custom')) {
      const fields = p.expr.trim().split(/\s+/);
      expect(fields).toHaveLength(5);
    }
  });

  it('custom preset has an empty expression', () => {
    const custom = CRON_PRESETS.find((p) => p.key === 'custom');
    expect(custom?.expr).toBe('');
  });

  it('daily preset maps to 09:00 every day', () => {
    const daily = CRON_PRESETS.find((p) => p.key === 'daily');
    expect(daily?.expr).toBe('0 9 * * *');
  });

  it('weekly preset maps to Monday 09:00', () => {
    const weekly = CRON_PRESETS.find((p) => p.key === 'weekly');
    expect(weekly?.expr).toBe('0 9 * * 1');
  });

  it('hourly preset maps to every full hour', () => {
    const hourly = CRON_PRESETS.find((p) => p.key === 'hourly');
    expect(hourly?.expr).toBe('0 * * * *');
  });

  it('each preset has a non-empty labelKey starting with schedules.preset.', () => {
    for (const p of CRON_PRESETS) {
      expect(p.labelKey).toMatch(/^schedules\.preset\./);
    }
  });
});

// ─── Trigger type switch ──────────────────────────────────────────────────────

describe('ScheduleCreateDialog trigger type switch', () => {
  it('offers both 定时 and Webhook tabs, defaulting to 定时 for a file-free App', () => {
    renderDialog(appWithTextField());
    const scheduleTab = screen.getByRole('tab', { name: 'schedules.type.schedule' });
    const webhookTab = screen.getByRole('tab', { name: 'schedules.type.webhook' });
    expect(scheduleTab).not.toBeDisabled();
    expect(scheduleTab).toHaveAttribute('aria-selected', 'true');
    expect(webhookTab).toHaveAttribute('aria-selected', 'false');
  });

  it('收口: a required-file App disables the 定时 tab and defaults to Webhook', () => {
    renderDialog(appWithRequiredFile());
    const scheduleTab = screen.getByRole('tab', { name: 'schedules.type.schedule' });
    const webhookTab = screen.getByRole('tab', { name: 'schedules.type.webhook' });
    expect(scheduleTab).toBeDisabled();
    expect(webhookTab).toHaveAttribute('aria-selected', 'true');
    // The 收口 hint is surfaced to the user.
    expect(screen.getAllByText('schedules.dialog.scheduleDisabledHint').length).toBeGreaterThan(0);
  });
});

// ─── Webhook create flow ──────────────────────────────────────────────────────

describe('ScheduleCreateDialog webhook create flow', () => {
  it('POSTs field_mapping and reveals the URL + secret on success', async () => {
    const user = userEvent.setup();
    mocks.create.mockResolvedValue({
      id: 'cron_x',
      webhook_secret: 'sekret-123',
      webhook_path: '/api/bff/v1/triggers/cron_x/webhook',
    });
    const onCreated = vi.fn();
    renderDialog(appWithTextField(), onCreated);

    // Switch to the Webhook tab.
    await user.click(screen.getByRole('tab', { name: 'schedules.type.webhook' }));

    // Name is required.
    await user.type(
      screen.getByPlaceholderText('schedules.dialog.field.name.placeholder'),
      'My Hook',
    );

    // Submit — incoming keys default to the App field keys (identity), and every
    // field defaults to from-event, so input_values buckets stay empty.
    await user.click(screen.getByRole('button', { name: 'schedules.dialog.submit' }));

    expect(mocks.create).toHaveBeenCalledWith({
      app_id: 'app_1',
      name: 'My Hook',
      trigger_type: 'webhook',
      field_mapping: { topic: 'topic' },
      input_values: { text: {}, multi: {} },
    });

    // Success view: title, secret, and the full webhook URL are shown once.
    await screen.findByText('schedules.webhook.success.title');
    expect(screen.getByText('sekret-123')).toBeInTheDocument();
    const fullUrl = window.location.origin + '/api/bff/v1/triggers/cron_x/webhook';
    expect(screen.getByText(fullUrl)).toBeInTheDocument();
    // onCreated does not fire until the user clicks 完成.
    expect(onCreated).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'schedules.webhook.done' }));
    expect(onCreated).toHaveBeenCalledTimes(1);
  });
});

// ─── Mixed input mode (fixed | from-event) ────────────────────────────────────

describe('ScheduleCreateDialog mixed input mode', () => {
  it('a field set to 固定值 lands in input_values and NOT in field_mapping', async () => {
    const user = userEvent.setup();
    mocks.create.mockResolvedValue({
      id: 'cron_x',
      webhook_secret: 's',
      webhook_path: '/api/bff/v1/triggers/cron_x/webhook',
    });
    renderDialog(appWithTextField());

    await user.click(screen.getByRole('tab', { name: 'schedules.type.webhook' }));
    await user.type(
      screen.getByPlaceholderText('schedules.dialog.field.name.placeholder'),
      'Fixed Hook',
    );

    // Flip the single 'topic' field to 固定值, then enter a fixed value.
    await user.click(screen.getByRole('button', { name: 'schedules.webhook.mode.fixed' }));
    await user.type(
      screen.getByPlaceholderText('schedules.dialog.webhook.fixedValue'),
      'climate',
    );

    await user.click(screen.getByRole('button', { name: 'schedules.dialog.submit' }));

    expect(mocks.create).toHaveBeenCalledWith({
      app_id: 'app_1',
      name: 'Fixed Hook',
      trigger_type: 'webhook',
      field_mapping: {}, // no from-event fields
      input_values: { text: { topic: 'climate' }, multi: {} },
    });
  });

  it('a from-event field lands in field_mapping and a file field stays from-event', async () => {
    const user = userEvent.setup();
    mocks.create.mockResolvedValue({
      id: 'cron_x',
      webhook_secret: 's',
      webhook_path: '/api/bff/v1/triggers/cron_x/webhook',
    });
    renderDialog(appWithTextAndFile());

    // The file field is optional here, so switch to the Webhook tab explicitly.
    await user.click(screen.getByRole('tab', { name: 'schedules.type.webhook' }));
    await user.type(
      screen.getByPlaceholderText('schedules.dialog.field.name.placeholder'),
      'Event Hook',
    );

    // The file field's 固定值 toggle is disabled (can't fix a file).
    const fixedButtons = screen.getAllByRole('button', { name: 'schedules.webhook.mode.fixed' });
    // topic (fixable) + doc (file, disabled).
    expect(fixedButtons.some((b) => (b as HTMLButtonElement).disabled)).toBe(true);

    // Leave everything at the default (from-event). Both keys map by identity.
    await user.click(screen.getByRole('button', { name: 'schedules.dialog.submit' }));

    expect(mocks.create).toHaveBeenCalledWith({
      app_id: 'app_3',
      name: 'Event Hook',
      trigger_type: 'webhook',
      field_mapping: { topic: 'topic', doc: 'doc' },
      input_values: { text: {}, multi: {} },
    });
  });
});
