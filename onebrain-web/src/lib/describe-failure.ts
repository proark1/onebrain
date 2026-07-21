/**
 * Describe a failed response for the UI.
 *
 * The API reports its own failures as a JSON `detail`, which is always the best
 * message. But a failure can also come from the edge rather than the API -- a
 * Caddy deny, a 502, an HTML error page -- and those bodies are not JSON. Fall
 * back to the status and path so the message still names what failed instead of
 * collapsing every such case to an undiagnosable "Request failed".
 *
 * The body itself is deliberately never echoed: it can carry arbitrary content,
 * and it must not be rendered into the browser.
 *
 * This lives on its own so the auth surfaces -- login, password change, logout,
 * the pages where an unnamed failure hurts most -- can share it without pulling
 * the whole API client into their bundle.
 */
export async function describeFailure(path: string, response: Response): Promise<string> {
  const body = await response.json().catch(() => null);
  const detail = body && typeof body.detail === "string" ? body.detail : "";
  if (detail) {
    return detail;
  }
  const status = `${response.status} ${response.statusText}`.trim();
  return `${status || "Request failed"} (${path.split("?")[0]})`;
}
