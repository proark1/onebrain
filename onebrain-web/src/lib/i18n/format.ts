// Locale-aware Intl helpers — the formatting half of the i18n foundation. Panels
// currently pass `undefined`/hardcoded "en" to Intl (dates, numbers) and fake a "€"
// glyph for currency; these give every surface one locale-keyed source instead.
// Consumed as panels migrate and by the upcoming bilingual accounting UI (EUR).

import { LOCALE_TAGS, type Locale } from "./locales";

type DateInput = Date | string | number;

function toDate(value: DateInput): Date | null {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatNumber(value: number, locale: Locale, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale], options).format(value);
}

export function formatCurrency(value: number, locale: Locale, currency = "EUR"): string {
  return new Intl.NumberFormat(LOCALE_TAGS[locale], { style: "currency", currency }).format(value);
}

export function formatDate(
  value: DateInput,
  locale: Locale,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium" },
): string {
  const date = toDate(value);
  return date ? new Intl.DateTimeFormat(LOCALE_TAGS[locale], options).format(date) : "";
}

export function formatDateTime(
  value: DateInput,
  locale: Locale,
  options: Intl.DateTimeFormatOptions = { dateStyle: "medium", timeStyle: "short" },
): string {
  return formatDate(value, locale, options);
}
