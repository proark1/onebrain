import { headers } from "next/headers";

export type SessionInfo = {
  role_id: string;
  role_label: string;
  clearance: string;
  location_label: string;
  tenant_id: string;
  display_name: string;
  email: string;
};

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

export function onebrainApiBaseUrl(): string {
  return (process.env.ONEBRAIN_API_BASE_URL || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}

async function forwardedCookie(): Promise<string> {
  const incoming = await headers();
  return incoming.get("cookie") || "";
}

export async function getSession(): Promise<SessionInfo | null> {
  const cookie = await forwardedCookie();
  const response = await fetch(`${onebrainApiBaseUrl()}/api/session/me`, {
    headers: cookie ? { cookie } : {},
    cache: "no-store",
  });

  if (response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`OneBrain API returned ${response.status} for /api/session/me`);
  }
  return response.json() as Promise<SessionInfo>;
}
