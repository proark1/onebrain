// Platform UI languages. German is primary; English is available. Mirrors the
// backend closed set (app/platform/base.py SUPPORTED_LOCALES / DEFAULT_LOCALE):
// the account's provisioned default flows here via /api/session/me.

export const SUPPORTED_LOCALES = ["de", "en"] as const;

export type Locale = (typeof SUPPORTED_LOCALES)[number];

export const DEFAULT_LOCALE: Locale = "de";

// Each language shown in its own name — a language menu never translates these.
export const LOCALE_NAMES: Record<Locale, string> = {
  de: "Deutsch",
  en: "English",
};

// BCP-47 tags for Intl.* formatting (dates, numbers, currency).
export const LOCALE_TAGS: Record<Locale, string> = {
  de: "de-DE",
  en: "en-US",
};

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && (SUPPORTED_LOCALES as readonly string[]).includes(value);
}

// Coerce anything (a missing/unknown session value, a stale localStorage entry)
// to a supported locale, defaulting to German — never surfaces an unsupported UI.
export function normalizeLocale(value: unknown): Locale {
  return isLocale(value) ? value : DEFAULT_LOCALE;
}
