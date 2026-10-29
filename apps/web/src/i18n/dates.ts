/**
 * Date formatting helpers that respect the active display language.
 *
 * Primary API (for React components with useTranslation()):
 *   formatDateTime(iso, lang)  — full date + time
 *   formatDate(iso, lang)      — date only
 *   formatTime(iso, lang)      — time only
 *
 * Auto-detect variant (for non-hook call sites, e.g. plain functions):
 *   formatDateTimeAuto(iso)    — reads lang from localStorage directly
 *   formatDateAuto(iso)        — same, date only
 *
 * Tradeoff: the *Auto() variants read localStorage synchronously, so they
 * won't re-render when lang changes unless the component itself re-renders
 * (e.g. due to other state). Prefer the lang-param versions inside components.
 */

import { readStoredLang, type Lang } from './index';

const LOCALE_MAP: Record<Lang, string> = {
  zh: 'zh-CN',
  en: 'en-US',
};

function toLocale(lang: Lang): string {
  return LOCALE_MAP[lang];
}

function asDate(iso: string | Date): Date {
  return iso instanceof Date ? iso : new Date(iso);
}

// ─── Primary (explicit lang param) ───────────────────────────────────────────

export function formatDateTime(
  iso: string | Date,
  lang: Lang,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = asDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString(toLocale(lang), options);
}

export function formatDate(
  iso: string | Date,
  lang: Lang,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = asDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString(toLocale(lang), options);
}

export function formatTime(
  iso: string | Date,
  lang: Lang,
  options?: Intl.DateTimeFormatOptions,
): string {
  const d = asDate(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleTimeString(toLocale(lang), options);
}

// ─── Auto-detect (localStorage read, for non-hook call sites) ────────────────

function detectLang(): Lang {
  return readStoredLang();
}

export function formatDateTimeAuto(
  iso: string | Date,
  options?: Intl.DateTimeFormatOptions,
): string {
  return formatDateTime(iso, detectLang(), options);
}

export function formatDateAuto(
  iso: string | Date,
  options?: Intl.DateTimeFormatOptions,
): string {
  return formatDate(iso, detectLang(), options);
}

export function formatTimeAuto(
  iso: string | Date,
  options?: Intl.DateTimeFormatOptions,
): string {
  return formatTime(iso, detectLang(), options);
}
