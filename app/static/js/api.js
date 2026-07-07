// Backend calls. Every request carries the current role, location, and the
// device session id (which scopes saved chats).

import { getSession, getState } from "./state.js";

function headers(extra = {}) {
  const { role, location } = getState();
  return {
    "X-Onebrain-Role": role,
    "X-Onebrain-Location": location,
    "X-Onebrain-Session": getSession(),
    ...extra,
  };
}

const json = (res) => res.json();

export const getRoles = () => fetch("/api/session/roles").then(json);
export const getLocations = () => fetch("/api/session/locations").then(json);
export const getMe = () => fetch("/api/session/me", { headers: headers() }).then(json);
export const listDocuments = () => fetch("/api/documents", { headers: headers() }).then(json);

export const getConversations = () => fetch("/api/conversations", { headers: headers() }).then(json);
export const getConversation = (id) => fetch(`/api/conversations/${id}`, { headers: headers() }).then(json);
export const deleteConversation = (id) =>
  fetch(`/api/conversations/${id}`, { method: "DELETE", headers: headers() });

export async function uploadDocument(formData) {
  const res = await fetch("/api/upload", { method: "POST", headers: headers(), body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Upload failed");
  }
  return res.json();
}

// Streams the answer via SSE. Sends conversation_id (null starts a new one) and
// calls onEvent for each parsed event, including the initial `conversation` event.
export async function askStream(question, conversationId, onEvent) {
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ question, conversation_id: conversationId }),
  });
  if (!res.ok || !res.body) throw new Error("Request failed");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}
