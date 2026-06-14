/**
 * SchedulesPage — unit tests for the cron preset mapping.
 *
 * Tests the CRON_PRESETS array that drives the preset chip UI:
 * each non-custom preset must have a non-empty cron expression that
 * follows the standard 5-field format, and the custom preset must have
 * an empty expression (the host TextField captures the user value).
 */
import { describe, it, expect } from 'vitest';
import { CRON_PRESETS } from './SchedulesPage';

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
