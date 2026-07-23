"use client";

import { useTranslations } from "@/components/locale-provider";
import { LOCALE_NAMES, SUPPORTED_LOCALES, isLocale } from "@/lib/i18n";

// The per-user language override. Persists the choice (localStorage) via the
// provider; each language is shown in its own name.
export function LocaleSwitcher() {
  const { locale, setLocale, t } = useTranslations();
  return (
    <select
      aria-label={t("locale.label")}
      className="localeSwitcher"
      onChange={(event) => {
        if (isLocale(event.target.value)) {
          setLocale(event.target.value);
        }
      }}
      value={locale}
    >
      {SUPPORTED_LOCALES.map((code) => (
        <option key={code} value={code}>
          {LOCALE_NAMES[code]}
        </option>
      ))}
    </select>
  );
}
