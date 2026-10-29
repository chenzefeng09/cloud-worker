/**
 * Hand-rolled i18n provider.
 *
 * Shape:
 *   <I18nProvider>         — wraps the app, reads/persists lang to localStorage
 *   useTranslation()       — { t, lang, setLang }
 *   t(key, vars?)          — lookup chain: currentLang → en → key
 *                            English is the default and the fallback locale;
 *                            zh is optional. Missing zh keys fall back silently
 *                            to en; missing en keys fall back to the raw key +
 *                            dev console.warn.
 *   {{var}} interpolation  — replaced with vars[var] values
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import enMessages from './locales/en.json';
import zhMessages from './locales/zh.json';

export type Lang = 'en' | 'zh';

/** Default + fallback display language. */
export const DEFAULT_LANG: Lang = 'en';

export const LANG_STORAGE_KEY = 'app.lang';
const STORAGE_KEY = LANG_STORAGE_KEY;

// Flatten nested JSON into dot-delimited keys: { "a": { "b": "v" } } → { "a.b": "v" }
type FlatMessages = Record<string, string>;

function flatten(obj: Record<string, unknown>, prefix = ''): FlatMessages {
  const out: FlatMessages = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'object' && v !== null) {
      Object.assign(out, flatten(v as Record<string, unknown>, key));
    } else {
      out[key] = String(v);
    }
  }
  return out;
}

const MESSAGES: Record<Lang, FlatMessages> = {
  en: flatten(enMessages as Record<string, unknown>),
  zh: flatten(zhMessages as Record<string, unknown>),
};

function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{\{(\w+)\}\}/g, (_, name) =>
    name in vars ? String(vars[name]) : `{{${name}}}`,
  );
}

export function readStoredLang(): Lang {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === 'zh' || v === 'en') return v;
  } catch {
    // localStorage unavailable (SSR / private mode)
  }
  return DEFAULT_LANG;
}

// ─── Context ──────────────────────────────────────────────────────────────────

interface I18nContextValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: string, vars?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

// ─── Provider ─────────────────────────────────────────────────────────────────

export function I18nProvider({ children }: { children: ReactNode }) {
  const [lang, setLangState] = useState<Lang>(readStoredLang);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // ignore
    }
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>): string => {
      const inCurrent = MESSAGES[lang][key];
      if (inCurrent !== undefined) return interpolate(inCurrent, vars);

      // Fallback 1: English string (covers keys missing from optional locales)
      const inEn = MESSAGES[DEFAULT_LANG][key];
      if (inEn !== undefined) return interpolate(inEn, vars);

      // Fallback 2: raw key — dev warning
      if (import.meta.env.DEV) {
        console.warn(`[i18n] missing key "${key}" in both "${lang}" and "${DEFAULT_LANG}"`);
      }
      return key;
    },
    [lang],
  );

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

// ─── Hook ─────────────────────────────────────────────────────────────────────

export function useTranslation(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) throw new Error('useTranslation must be used inside <I18nProvider>');
  return ctx;
}
