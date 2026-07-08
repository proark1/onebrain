import type {
  AskPayload,
  ChatScope,
  ChatStreamEvent,
  ConversationDetail,
  ConversationSummary,
} from "@/lib/onebrain-types";
import { scopeQuery } from "@/lib/onebrain-types";

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
