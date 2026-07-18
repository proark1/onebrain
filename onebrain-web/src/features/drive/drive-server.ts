import { headers } from "next/headers";
import { onebrainApiBaseUrl } from "@/lib/onebrain-api";
import type { DriveBootstrap } from "./types";

export async function getDriveBootstrap(): Promise<DriveBootstrap> {
  const incoming = await headers();
  const cookie = incoming.get("cookie") || "";
  const response = await fetch(`${onebrainApiBaseUrl()}/api/drive/bootstrap`, {
    headers: cookie ? { cookie } : {},
    cache: "no-store",
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(apiErrorMessage(payload, `Drive returned ${response.status}.`));
  }
  return response.json() as Promise<DriveBootstrap>;
}

function apiErrorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}
