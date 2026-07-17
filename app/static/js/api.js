// Backend calls. Auth is via the httpOnly session cookie, sent automatically on
// same-origin requests — no identity headers.

const json = (res) => res.json();

function cleanScope(scope = {}) {
  const accountId = (scope.account_id || "").trim();
  const spaceId = (scope.space_id || "").trim();
  return accountId && spaceId ? { account_id: accountId, space_id: spaceId } : {};
}

function scopeQuery(scope = {}) {
  const clean = cleanScope(scope);
  if (!clean.account_id) return "";
  const params = new URLSearchParams(clean);
  return `?${params.toString()}`;
}

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

export const listDocuments = (scope = {}) => fetch(`/api/documents${scopeQuery(scope)}`).then(json);
export const listPending = (scope = {}) => fetch(`/api/documents/pending${scopeQuery(scope)}`).then(json);

export async function approveDocument(id, scope = {}) {
  const res = await fetch(`/api/documents/${id}/approve${scopeQuery(scope)}`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Approval failed");
  }
  return res.json();
}
export const getConversations = (scope = {}) => fetch(`/api/conversations${scopeQuery(scope)}`).then(json);
export const getConversation = (id, scope = {}) => fetch(`/api/conversations/${id}${scopeQuery(scope)}`).then(json);
export const deleteConversation = (id, scope = {}) =>
  fetch(`/api/conversations/${id}${scopeQuery(scope)}`, { method: "DELETE" });

export async function uploadDocument(formData, scope = {}) {
  const clean = cleanScope(scope);
  if (clean.account_id) {
    formData.set("account_id", clean.account_id);
    formData.set("space_id", clean.space_id);
  }
  const res = await fetch("/api/upload", { method: "POST", body: formData });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || "Upload failed");
  }
  return res.json();
}

export const listProvisioningModules = () => requestJson("/api/provisioning/modules");

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

export const listOperatorCustomers = () => requestJson("/api/operator/customers");
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
export const listAccountServiceKeys = (accountId) =>
  requestJson(`/api/operator/accounts/${encodeURIComponent(accountId)}/service-keys`);

export const exportPrivacyData = (accountId, spaceId = "") => {
  const params = spaceId ? `?space_id=${encodeURIComponent(spaceId)}` : "";
  return requestJson(`/api/privacy/accounts/${encodeURIComponent(accountId)}/export${params}`);
};

export async function erasePrivacyData(accountId, payload) {
  return requestJson(`/api/privacy/accounts/${encodeURIComponent(accountId)}/erase`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function revokeAccountServiceKey(accountId, keyId) {
  return requestJson(
    `/api/operator/accounts/${encodeURIComponent(accountId)}/service-keys/${encodeURIComponent(keyId)}`,
    { method: "DELETE" },
  );
}

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

export async function updateRollout(rolloutId, payload) {
  return requestJson(`/api/operator/rollouts/${encodeURIComponent(rolloutId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

// Streams the answer via SSE. Sends conversation_id (null starts a new one).
export async function askStream(question, conversationId, scopeOrCallback, maybeCallback) {
  const scope = typeof scopeOrCallback === "function" ? {} : cleanScope(scopeOrCallback || {});
  const onEvent = typeof scopeOrCallback === "function" ? scopeOrCallback : maybeCallback;
  const payload = { question, conversation_id: conversationId, ...scope };
  const res = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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
