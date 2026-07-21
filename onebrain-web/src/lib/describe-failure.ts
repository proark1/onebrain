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
 * and it must not be rendered into the browser. A 422 is the one shape we read
 * field-wise, and even there only FastAPI's own generated `msg` is taken --
 * never `input`, which holds what the user typed (a password, on the surface
 * that needs this most).
 *
 * This lives on its own so the auth surfaces -- login, password change, logout,
 * the pages where an unnamed failure hurts most -- can share it without pulling
 * the whole API client into their bundle.
 */
export async function describeFailure(path: string, response: Response): Promise<string> {
  const body = await response.json().catch(() => null);
  const detail = describeDetail(body?.detail);
  if (detail) {
    return detail;
  }
  const status = `${response.status} ${response.statusText}`.trim();
  return `${status || "Request failed"} (${path.split("?")[0]})`;
}

/**
 * `detail` is a string when the API raised the error itself, and an array of
 * `{loc, msg, type}` when Pydantic rejected the request body (422). Reading
 * only the string form left every validation failure showing "422
 * Unprocessable Entity" -- and a too-short new password is exactly a 422, on
 * the password-change panel, where a message that does not say what is wrong
 * leaves the user with no way to succeed.
 */
function describeDetail(detail: unknown): string {
  if (typeof detail === "string") {
    return detail;
  }
  if (!Array.isArray(detail)) {
    return "";
  }
  return detail
    .map((item) => (item && typeof (item as { msg?: unknown }).msg === "string"
      ? (item as { msg: string }).msg
      : ""))
    .filter(Boolean)
    .join("; ");
}
