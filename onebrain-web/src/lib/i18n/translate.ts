// The pure translator. Deliberately self-contained (no runtime relative imports)
// and catalog-shape-agnostic, so it is directly unit-testable under the Node test
// runner (which does not resolve extensionless relative .ts imports).

export type TranslateParams = Record<string, string | number>;

// Resolve `key` against `catalog` and fill {token} placeholders from `params`.
// Falls back to the key itself if it is somehow absent, so a gap degrades to a
// visible label rather than a crash or an empty string.
export function translate<Catalog extends Record<string, string>>(
  catalog: Catalog,
  key: keyof Catalog,
  params?: TranslateParams,
): string {
  const template = catalog[key] ?? String(key);
  if (!params) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (match: string, name: string) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match,
  );
}
