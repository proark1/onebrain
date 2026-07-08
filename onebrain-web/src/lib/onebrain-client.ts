import type {
  ApproveDocumentResult,
  AskPayload,
  ChatScope,
  ChatStreamEvent,
  ConversationDetail,
  ConversationSummary,
  DocumentSummary,
  PendingDocument,
  UploadDocumentInput,
} from "@/lib/onebrain-types";
import { cleanScope, scopeQuery } from "@/lib/onebrain-types";

const PROXY_BASE = "/api/onebrain";

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${PROXY_BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  }
  return response.json() as Promise<T>;
}

export function listConversations(scope: ChatScope = {}): Promise<ConversationSummary[]> {
  return requestJson<ConversationSummary[]>(`/conversations${scopeQuery(scope)}`);
}

export function getConversation(id: string, scope: ChatScope = {}): Promise<ConversationDetail> {
  return requestJson<ConversationDetail>(`/conversations/${encodeURIComponent(id)}${scopeQuery(scope)}`);
}

export async function deleteConversation(id: string, scope: ChatScope = {}): Promise<void> {
  await requestJson<{ deleted: string }>(`/conversations/${encodeURIComponent(id)}${scopeQuery(scope)}`, {
    method: "DELETE",
  });
}

export function listDocuments(scope: ChatScope = {}): Promise<DocumentSummary[]> {
  return requestJson<DocumentSummary[]>(`/documents${scopeQuery(scope)}`);
}

export function listPendingDocuments(scope: ChatScope = {}): Promise<PendingDocument[]> {
  return requestJson<PendingDocument[]>(`/documents/pending${scopeQuery(scope)}`);
}

export function approveDocument(id: string, scope: ChatScope = {}): Promise<ApproveDocumentResult> {
  return requestJson<ApproveDocumentResult>(`/documents/${encodeURIComponent(id)}/approve${scopeQuery(scope)}`, {
    method: "POST",
  });
}

export function uploadDocument(input: UploadDocumentInput, scope: ChatScope = {}): Promise<DocumentSummary> {
  const clean = cleanScope(scope);
  const body = new FormData();
  body.set("file", input.file);
  body.set("classification", input.classification);
  body.set("location", input.location);
  body.set("category", input.category);
  if (clean.account_id && clean.space_id) {
    body.set("account_id", clean.account_id);
    body.set("space_id", clean.space_id);
  }
  return requestJson<DocumentSummary>("/upload", {
    method: "POST",
    body,
  });
}

export async function askStream(
  payload: AskPayload,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  const response = await fetch(`${PROXY_BASE}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";

    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith("data:")) {
        continue;
      }
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatStreamEvent);
      } catch {
        // Keep the stream alive if one event is malformed.
      }
    }
  }
}
