const DEFAULT_LOGIN_REDIRECT = "/chat";
const APP_ORIGIN = "https://onebrain.invalid";

export function safeLoginRedirect(value: string | string[] | undefined, fallback = DEFAULT_LOGIN_REDIRECT): string {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (!candidate) {
    return fallback;
  }
  try {
    let decodedCandidate = candidate;
    // Decode nested URL escaping before validating boundaries. This catches
    // values such as `/login%252F...` or `%255C` that a router may decode later.
    for (let index = 0; index < 3; index += 1) {
      const decoded = decodeURIComponent(decodedCandidate);
      if (decoded === decodedCandidate) break;
      decodedCandidate = decoded;
    }
    if (decodedCandidate.includes("\\") || decodedCandidate.startsWith("//")) {
      return fallback;
    }
    const appUrl = new URL(APP_ORIGIN);
    const target = new URL(candidate, appUrl);
    const decodedTarget = new URL(decodedCandidate, appUrl);
    if (
      target.origin !== appUrl.origin
      || !target.pathname.startsWith("/")
      || target.pathname.startsWith("//")
      || target.pathname === "/login"
      || target.pathname.startsWith("/login/")
      || decodedTarget.origin !== appUrl.origin
      || decodedTarget.pathname === "/login"
      || decodedTarget.pathname.startsWith("/login/")
    ) {
      return fallback;
    }
    return `${target.pathname}${target.search}${target.hash}`;
  } catch {
    return fallback;
  }
}

export function loginHref(nextPath: string): string {
  return `/login?next=${encodeURIComponent(safeLoginRedirect(nextPath))}`;
}
