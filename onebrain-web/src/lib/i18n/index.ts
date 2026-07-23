// Public surface of the light i18n layer: locale constants/helpers, the message
// catalogs, the pure translate() function, and locale-aware formatters. The React
// binding (LocaleProvider / useTranslations) lives in components/locale-provider.tsx
// and consumes these; everything here is framework-free.

import { de, type Messages } from "./de";
import { en } from "./en";
import { DEFAULT_LOCALE, type Locale } from "./locales";

export type { Messages } from "./de";
export type MessageKey = keyof Messages;
export * from "./locales";
export * from "./format";
export * from "./translate";

const CATALOGS: Record<Locale, Messages> = { de, en };

export function getCatalog(locale: Locale): Messages {
  return CATALOGS[locale] ?? CATALOGS[DEFAULT_LOCALE];
}
