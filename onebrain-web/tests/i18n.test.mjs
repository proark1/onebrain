import assert from "node:assert/strict";
import test from "node:test";

// Import only self-contained leaf modules: the Node test runner does not resolve
// extensionless relative .ts imports, so index.ts/format.ts (which have them) are
// covered by typecheck + the running app, not here.
import { de } from "../src/lib/i18n/de.ts";
import { en } from "../src/lib/i18n/en.ts";
import { DEFAULT_LOCALE, SUPPORTED_LOCALES, isLocale, normalizeLocale } from "../src/lib/i18n/locales.ts";
import { translate } from "../src/lib/i18n/translate.ts";
import { ALL_NAV, consoleNavigationGroups } from "../src/lib/console-navigation.ts";

test("the English catalog carries exactly the German catalog's keys", () => {
  // The core i18n invariant: no missing and no orphaned translations.
  assert.deepEqual(Object.keys(en).sort(), Object.keys(de).sort());
});

test("both catalogs provide a non-empty string for every key", () => {
  for (const catalog of [de, en]) {
    for (const [key, value] of Object.entries(catalog)) {
      assert.equal(typeof value, "string", `value for ${key} must be a string`);
      assert.ok(value.length > 0, `value for ${key} must not be empty`);
    }
  }
});

test("every navigation label key resolves in both catalogs", () => {
  const groupKeys = [false, true].flatMap((operatorMode) =>
    consoleNavigationGroups(operatorMode).map((group) => group.labelKey),
  );
  const itemKeys = ALL_NAV.map((item) => item.labelKey);
  for (const key of new Set([...groupKeys, ...itemKeys])) {
    assert.ok(key in de, `German catalog is missing ${key}`);
    assert.ok(key in en, `English catalog is missing ${key}`);
  }
});

test("translate fills named placeholders and leaves unknown ones intact", () => {
  assert.equal(translate(en, "shell.accountSettingsFor", { name: "Ada" }), "Account settings for Ada");
  assert.equal(translate(de, "shell.accountSettingsFor", { name: "Ada" }), "Kontoeinstellungen für Ada");
  assert.equal(translate(en, "nav.groupSections", { group: "Work" }), "Work sections");
  // A placeholder with no matching param is left verbatim rather than blanked.
  assert.equal(translate(en, "shell.accountSettingsFor"), "Account settings for {name}");
});

test("locale helpers accept supported locales and default to German otherwise", () => {
  assert.deepEqual([...SUPPORTED_LOCALES], ["de", "en"]);
  assert.equal(DEFAULT_LOCALE, "de");
  assert.equal(isLocale("de"), true);
  assert.equal(isLocale("en"), true);
  assert.equal(isLocale("fr"), false);
  assert.equal(normalizeLocale("en"), "en");
  assert.equal(normalizeLocale("fr"), "de");
  assert.equal(normalizeLocale(null), "de");
  assert.equal(normalizeLocale(undefined), "de");
});
