// Backend calls. Auth is via the httpOnly session cookie, sent automatically on
// same-origin requests — no identity headers.

const json = (res) => res.json();

async function requestJson(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Request failed");
  }
  return res.json();
}

export async function getMe() {
  const res = await fetch("/api/session/me");
  return res.ok ? res.json() : null;   // 401 -> null (not logged in)
}

export async function login(email, password) {
  const res = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Login failed");
  }
  return res.json();
}

export const logout = () => fetch("/api/auth/logout", { method: "POST" });

export const listDocuments = () => fetch("/api/documents").then(json);
export const listPending = () => fetch("/api/documents/pending").then(json);

export async function approveDocument(id) {
  const res = await fetch(`/api/documents/${id}/approve`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Approval failed");
  }
  return res.json();
}
export const getConversations = () => fetch("/api/conversations").then(json);
export const getConversation = (id) => fetch(`/api/conversations/${id}`).then(json);
export const deleteConversation = (id) => fetch(`/api/conversations/${id}`, { method: "DELETE" });

export async function uploadDocument(formData) {
  const res = await fetch("/api/upload", { method: "POST", body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Upload failed");
  }
  return res.json();
}

export const listProvisioningBundles = () => requestJson("/api/provisioning/bundles");

export async function provisionCustomer(payload) {
  return requestJson("/api/provisioning/customers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const listPlatformAccounts = () => requestJson("/api/platform/accounts");
export const listPlatformSpaces = (accountId) => requestJson(`/api/platform/accounts/${encodeURIComponent(accountId)}/spaces`);
export const listPlatformApps = (accountId) => requestJson(`/api/platform/accounts/${encodeURIComponent(accountId)}/apps`);

export const listDeployments = () => requestJson("/api/operator/deployments");
export const listDeploymentModules = (deploymentId) =>
  requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/modules`);
export const listReleases = () => requestJson("/api/operator/releases");

export async function createRelease(payload) {
  return requestJson("/api/operator/releases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const getUpdatePlan = (deploymentId, targetVersion) =>
  requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/update-plan/${encodeURIComponent(targetVersion)}`);
export const listRollouts = (deploymentId) =>
  requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/rollouts`);
export const latestBackup = (deploymentId) =>
  requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/backups/latest`);
export const latestHealth = (deploymentId) =>
  requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/health/latest`);

export async function recordBackup(deploymentId, payload) {
  return requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/backups`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function recordHealth(deploymentId, payload) {
  return requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/health`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function startRollout(deploymentId, targetVersion) {
  return requestJson(`/api/operator/deployments/${encodeURIComponent(deploymentId)}/rollouts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_version: targetVersion }),
  });
}

// Streams the answer via SSE. Sends conversation_id (null starts a new one).
export async function askStream(question, conversationId, onEvent) {
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
