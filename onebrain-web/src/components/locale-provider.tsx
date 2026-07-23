"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import {
  DEFAULT_LOCALE,
  formatCurrency,
  formatDate,
  formatDateTime,
  formatNumber,
  getCatalog,
  isLocale,
  normalizeLocale,
  translate,
  type Locale,
  type MessageKey,
  type TranslateParams,
} from "@/lib/i18n";

type DateInput = Date | string | number;

const STORAGE_KEY = "onebrain.locale";

// A tiny external store over localStorage: the per-user language override lives in
// localStorage (shared across tabs), and useSyncExternalStore reads it without a
// hydration mismatch — the server snapshot is empty (→ account default), and the
// client re-reads after hydration. No setState-in-effect.
const listeners = new Set<() => void>();

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (event: StorageEvent) => {
    if (event.key === STORAGE_KEY) {
      listener();
    }
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

function getStoredSnapshot(): string {
  try {
    return window.localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function getServerSnapshot(): string {
  return "";
}

function writeStoredLocale(locale: Locale): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, locale);
  } catch {
    // Non-fatal (privacy mode): the choice still applies until listeners re-read,
    // but only after an in-memory notify below.
  }
  listeners.forEach((listener) => listener());
}

type LocaleContextValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: MessageKey, params?: TranslateParams) => string;
  // Formatters pre-bound to the active locale — the Intl half of the foundation,
  // consumed as panels migrate and by the bilingual accounting UI (EUR).
  formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string;
  formatCurrency: (value: number, currency?: string) => string;
  formatDate: (value: DateInput, options?: Intl.DateTimeFormatOptions) => string;
  formatDateTime: (value: DateInput, options?: Intl.DateTimeFormatOptions) => string;
};

const LocaleContext = createContext<LocaleContextValue | null>(null);

/**
 * Seeds the UI language from the account's provisioned default (`defaultLocale`,
 * from /api/session/me) and lets the user override it for this browser.
 *
 * With no stored override the active locale is the account default, so the server
 * render and first client paint agree — no hydration mismatch and no flash for the
 * common case. `<html lang>` tracks the active locale. This is the deliberately
 * light client-side approach; a cookie/SSR handoff could replace it later without
 * changing the t()/useTranslations contract.
 */
export function LocaleProvider({
  children,
  defaultLocale,
}: {
  children: ReactNode;
  defaultLocale?: string;
}) {
  const stored = useSyncExternalStore(subscribe, getStoredSnapshot, getServerSnapshot);
  const locale = isLocale(stored) ? stored : normalizeLocale(defaultLocale);

  useEffect(() => {
    document.documentElement.lang = locale;
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    writeStoredLocale(next);
  }, []);

  const value = useMemo<LocaleContextValue>(() => {
    const catalog = getCatalog(locale);
    return {
      locale,
      setLocale,
      t: (key, params) => translate(catalog, key, params),
      formatNumber: (val, options) => formatNumber(val, locale, options),
      formatCurrency: (val, currency) => formatCurrency(val, locale, currency),
      formatDate: (val, options) => formatDate(val, locale, options),
      formatDateTime: (val, options) => formatDateTime(val, locale, options),
    };
  }, [locale, setLocale]);

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

/**
 * Access the active locale and translator. Falls back to the default catalog when
 * used outside a provider (mirrors useWorkspace) so a component never crashes for
 * lack of a provider — it simply renders German with no switching.
 */
export function useTranslations(): LocaleContextValue {
  const value = useContext(LocaleContext);
  if (value) {
    return value;
  }
  const catalog = getCatalog(DEFAULT_LOCALE);
  return {
    locale: DEFAULT_LOCALE,
    setLocale: () => {},
    t: (key, params) => translate(catalog, key, params),
    formatNumber: (val, options) => formatNumber(val, DEFAULT_LOCALE, options),
    formatCurrency: (val, currency) => formatCurrency(val, DEFAULT_LOCALE, currency),
    formatDate: (val, options) => formatDate(val, DEFAULT_LOCALE, options),
    formatDateTime: (val, options) => formatDateTime(val, DEFAULT_LOCALE, options),
  };
}
