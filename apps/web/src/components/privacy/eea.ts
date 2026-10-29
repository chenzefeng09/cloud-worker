/**
 * EU/EEA helpers for the admin data-protection screen (GDPR Chapter V).
 * Deliberately classification-only: whether a destination has an adequacy
 * decision changes over time, so we link to the Commission's list instead of
 * hard-coding it.
 */

/** EU member states plus Iceland, Liechtenstein and Norway (ISO 3166-1 alpha-2; EL = EU code for Greece). */
const EEA_COUNTRIES = new Set([
  'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'EL', 'HU', 'IE',
  'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE', 'IS', 'LI', 'NO',
]);

export type RegionStatus = 'eea' | 'outside' | 'unknown';

/** Classify a reported hosting region. Cloud region names ("eu-west-2" is London) stay unknown. */
export function regionStatus(region: string | null | undefined): RegionStatus {
  const r = (region ?? '').trim().toUpperCase();
  if (!r) return 'unknown';
  if (r === 'EU' || r === 'EEA' || EEA_COUNTRIES.has(r)) return 'eea';
  if (/^[A-Z]{2}$/.test(r)) return 'outside';
  return 'unknown';
}

/** True when a residency policy refers to China's PIPL rather than the GDPR. */
export function isPiplPolicy(policy: string | null | undefined): boolean {
  return /\bPIPL\b|personal information protection law/i.test(policy ?? '');
}

export const ADEQUACY_DECISIONS_URL =
  'https://commission.europa.eu/law/law-topic/data-protection/international-dimension-data-protection/adequacy-decisions_en';
