const DEFAULT_LOGIN_REDIRECT = "/chat";

export function safeLoginRedirect(value: string | string[] | undefined, fallback = DEFAULT_LOGIN_REDIRECT): string {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (!candidate || !candidate.startsWith("/") || candidate.startsWith("//") || candidate.startsWith("/login")) {
    return fallback;
  }
  return candidate;
}

export function loginHref(nextPath: string): string {
  return `/login?next=${encodeURIComponent(safeLoginRedirect(nextPath))}`;
}
