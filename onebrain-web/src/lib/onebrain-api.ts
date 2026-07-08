import { headers } from "next/headers";
import type { ConversationSummary, DocumentSummary, PendingDocument, SessionInfo } from "@/lib/onebrain-types";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

export function onebrainApiBaseUrl(): string {
  return (process.env.ONEBRAIN_API_BASE_URL || DEFAULT_API_BASE_URL).replace(/\/+$/, "");
}

async function forwardedCookie(): Promise<string> {
  const incoming = await headers();
  return incoming.get("cookie") || "";
}

export async function getSession(): Promise<SessionInfo | null> {
  return serverRequestJson<SessionInfo>("/api/session/me", true);
}

async function serverRequestJson<T>(path: string, nullableOnUnauthorized = false): Promise<T | null> {
  const cookie = await forwardedCookie();
  const response = await fetch(`${onebrainApiBaseUrl()}${path}`, {
    headers: cookie ? { cookie } : {},
    cache: "no-store",
  });

  if (nullableOnUnauthorized && response.status === 401) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`OneBrain API returned ${response.status} for ${path}`);
  }
  return response.json() as Promise<T>;
}

export async function listServerConversations(): Promise<ConversationSummary[]> {
  const conversations = await serverRequestJson<ConversationSummary[]>("/api/conversations");
  return conversations ?? [];
}

export async function listServerDocuments(): Promise<DocumentSummary[]> {
  const documents = await serverRequestJson<DocumentSummary[]>("/api/documents");
  return documents ?? [];
}

export async function listServerPendingDocuments(): Promise<PendingDocument[]> {
  const documents = await serverRequestJson<PendingDocument[]>("/api/documents/pending");
  return documents ?? [];
}
