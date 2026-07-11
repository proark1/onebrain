import type {
  ApproveDocumentResult,
  AskPayload,
  BrandTheme,
  BrandThemeInput,
  ChatScope,
  ChatStreamEvent,
  ConversationDetail,
  ConversationSummary,
  CreatePlatformAccountInput,
  CreatePlatformSpaceInput,
  CreateOperatorReleaseInput,
  DocumentSummary,
  InstallPlatformAppInput,
  OperatorBackup,
  OperatorCustomer,
  OperatorDeployment,
  OperatorHealth,
  OperatorModule,
  OperatorObservability,
  OperatorRelease,
  OperatorRollout,
  OperatorRolloutStatusInput,
  OperatorRunInput,
  OperatorUpdatePlan,
  PendingDocument,
  PlatformAccessCheckInput,
  PlatformAccessCheckResult,
  PlatformAccount,
  PlatformAppInstallation,
  PlatformAuditEvent,
  PlatformSpace,
  PrivacyEraseInput,
  PrivacyEraseResult,
  PrivacyExport,
  ProvisionCustomerInput,
  ProvisioningRun,
  ProvisioningBundle,
  ProvisioningResult,
  BootstrapSecretResult,
  ServiceKeyInfo,
  UploadDocumentInput,
  FleetOverview,
  FleetRollout,
  FleetRolloutCreateResult,
  CreateFleetRolloutInput,
  FleetKeyInfo,
  MintedFleetKey,
  DeploymentEnrollment,
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

export function listPlatformAccounts(): Promise<PlatformAccount[]> {
  return requestJson<PlatformAccount[]>("/platform/accounts");
}

export function createPlatformAccount(input: CreatePlatformAccountInput): Promise<PlatformAccount> {
  return requestJson<PlatformAccount>("/platform/accounts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: input.id?.trim() || null,
      kind: input.kind,
      name: input.name.trim(),
    }),
  });
}

export function listPlatformSpaces(accountId: string): Promise<PlatformSpace[]> {
  return requestJson<PlatformSpace[]>(`/platform/accounts/${encodeURIComponent(accountId)}/spaces`);
}

export function createPlatformSpace(accountId: string, input: CreatePlatformSpaceInput): Promise<PlatformSpace> {
  return requestJson<PlatformSpace>(`/platform/accounts/${encodeURIComponent(accountId)}/spaces`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: input.id?.trim() || null,
      kind: input.kind,
      name: input.name.trim(),
    }),
  });
}

export function listPlatformApps(accountId: string): Promise<PlatformAppInstallation[]> {
  return requestJson<PlatformAppInstallation[]>(`/platform/accounts/${encodeURIComponent(accountId)}/apps`);
}

export function installPlatformApp(
  accountId: string,
  input: InstallPlatformAppInput,
): Promise<PlatformAppInstallation> {
  return requestJson<PlatformAppInstallation>(`/platform/accounts/${encodeURIComponent(accountId)}/apps`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: input.id?.trim() || null,
      app_id: input.app_id,
      display_name: input.display_name?.trim() || "",
      enabled_space_ids: input.enabled_space_ids,
      allowed_purposes: input.allowed_purposes,
    }),
  });
}

export function checkPlatformAccess(input: PlatformAccessCheckInput): Promise<PlatformAccessCheckResult> {
  return requestJson<PlatformAccessCheckResult>("/platform/access/check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function listPlatformAudit(accountId: string): Promise<PlatformAuditEvent[]> {
  return requestJson<PlatformAuditEvent[]>(`/platform/accounts/${encodeURIComponent(accountId)}/audit`);
}

export function getPlatformBrandTheme(accountId: string, appId = ""): Promise<BrandTheme> {
  const query = appId ? `?${new URLSearchParams({ app_id: appId }).toString()}` : "";
  return requestJson<BrandTheme>(`/platform/accounts/${encodeURIComponent(accountId)}/brand-theme${query}`);
}

export function listPlatformBrandThemes(accountId: string): Promise<BrandTheme[]> {
  return requestJson<BrandTheme[]>(`/platform/accounts/${encodeURIComponent(accountId)}/brand-themes`);
}

export function upsertPlatformBrandTheme(
  accountId: string,
  input: BrandThemeInput & { app_id?: string },
): Promise<BrandTheme> {
  return requestJson<BrandTheme>(`/platform/accounts/${encodeURIComponent(accountId)}/brand-theme`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ...input,
      app_id: input.app_id?.trim() || "",
      logo_url: input.logo_url?.trim() || "",
      name: input.name?.trim() || "",
    }),
  });
}

export function listProvisioningBundles(): Promise<ProvisioningBundle[]> {
  return requestJson<ProvisioningBundle[]>("/provisioning/bundles");
}

export function provisionCustomer(input: ProvisionCustomerInput): Promise<ProvisioningResult> {
  return requestJson<ProvisioningResult>("/provisioning/customers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: input.account_id?.trim() || null,
      account_kind: input.account_kind || "organization",
      bundle_id: input.bundle_id,
      current_migration: input.current_migration?.trim() || "",
      customer_name: input.customer_name.trim(),
      deployment_id: input.deployment_id?.trim() || null,
      deployment_type: input.deployment_type,
      initial_version: input.initial_version.trim(),
      mint_integration_keys: input.mint_integration_keys ?? true,
      module_versions: input.module_versions || {},
      region: input.region?.trim() || "",
      release_ring: input.release_ring,
      brand_theme: input.brand_theme,
      app_brand_themes: input.app_brand_themes || {},
      external_provisioning: input.external_provisioning ?? false,
      dry_run: input.dry_run ?? true,
      callback_url: input.callback_url?.trim() || "",
    }),
  });
}

export function listProvisioningRuns(): Promise<ProvisioningRun[]> {
  return requestJson<ProvisioningRun[]>("/provisioning/runs");
}

export function retryProvisioningRun(runId: string): Promise<ProvisioningRun> {
  return requestJson<ProvisioningRun>(`/provisioning/runs/${encodeURIComponent(runId)}/retry`, {
    method: "POST",
  });
}

export function readBootstrapSecret(runId: string): Promise<BootstrapSecretResult> {
  return requestJson<BootstrapSecretResult>(
    `/provisioning/runs/${encodeURIComponent(runId)}/bootstrap-secret/read`,
    { method: "POST" },
  );
}

export function listOperatorCustomers(): Promise<OperatorCustomer[]> {
  return requestJson<OperatorCustomer[]>("/operator/customers");
}

export function getOperatorObservability(): Promise<OperatorObservability> {
  return requestJson<OperatorObservability>("/operator/observability");
}

export function listOperatorDeployments(): Promise<OperatorDeployment[]> {
  return requestJson<OperatorDeployment[]>("/operator/deployments");
}

export function listOperatorDeploymentModules(deploymentId: string): Promise<OperatorModule[]> {
  return requestJson<OperatorModule[]>(`/operator/deployments/${encodeURIComponent(deploymentId)}/modules`);
}

export function listOperatorReleases(): Promise<OperatorRelease[]> {
  return requestJson<OperatorRelease[]>("/operator/releases");
}

export function createOperatorRelease(input: CreateOperatorReleaseInput): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>("/operator/releases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      git_sha: input.git_sha.trim(),
      migration_from: input.migration_from?.trim() || "",
      migration_to: input.migration_to?.trim() || "",
      modules: input.modules,
      rollback_plan: input.rollback_plan?.trim() || "",
      security_notes: input.security_notes?.trim() || "",
      status: input.status || "draft",
      version: input.version.trim(),
    }),
  });
}

export function getOperatorUpdatePlan(deploymentId: string, targetVersion: string): Promise<OperatorUpdatePlan> {
  return requestJson<OperatorUpdatePlan>(
    `/operator/deployments/${encodeURIComponent(deploymentId)}/update-plan/${encodeURIComponent(targetVersion)}`,
  );
}

export function listOperatorRollouts(deploymentId: string): Promise<OperatorRollout[]> {
  return requestJson<OperatorRollout[]>(`/operator/deployments/${encodeURIComponent(deploymentId)}/rollouts`);
}

export function startOperatorRollout(deploymentId: string, targetVersion: string): Promise<OperatorRollout> {
  return requestJson<OperatorRollout>(`/operator/deployments/${encodeURIComponent(deploymentId)}/rollouts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_version: targetVersion }),
  });
}

export function updateOperatorRollout(
  rolloutId: string,
  input: OperatorRolloutStatusInput,
): Promise<OperatorRollout> {
  return requestJson<OperatorRollout>(`/operator/rollouts/${encodeURIComponent(rolloutId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: input.notes || "", status: input.status }),
  });
}

export function latestOperatorBackup(deploymentId: string): Promise<OperatorBackup | null> {
  return requestJson<OperatorBackup | null>(`/operator/deployments/${encodeURIComponent(deploymentId)}/backups/latest`);
}

export function recordOperatorBackup(deploymentId: string, input: OperatorRunInput): Promise<OperatorBackup> {
  return requestJson<OperatorBackup>(`/operator/deployments/${encodeURIComponent(deploymentId)}/backups`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ detail: input.detail || "", id: input.id || null, status: input.status }),
  });
}

export function latestOperatorHealth(deploymentId: string): Promise<OperatorHealth | null> {
  return requestJson<OperatorHealth | null>(`/operator/deployments/${encodeURIComponent(deploymentId)}/health/latest`);
}

export function recordOperatorHealth(deploymentId: string, input: OperatorRunInput): Promise<OperatorHealth> {
  return requestJson<OperatorHealth>(`/operator/deployments/${encodeURIComponent(deploymentId)}/health`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ detail: input.detail || "", id: input.id || null, status: input.status }),
  });
}

export function listAccountServiceKeys(accountId: string): Promise<ServiceKeyInfo[]> {
  return requestJson<ServiceKeyInfo[]>(`/operator/accounts/${encodeURIComponent(accountId)}/service-keys`);
}

export async function revokeAccountServiceKey(accountId: string, keyId: string): Promise<void> {
  await requestJson<{ revoked: string }>(
    `/operator/accounts/${encodeURIComponent(accountId)}/service-keys/${encodeURIComponent(keyId)}`,
    { method: "DELETE" },
  );
}

export function exportPrivacyData(accountId: string, spaceId = ""): Promise<PrivacyExport> {
  const params = spaceId ? `?${new URLSearchParams({ space_id: spaceId }).toString()}` : "";
  return requestJson<PrivacyExport>(`/privacy/accounts/${encodeURIComponent(accountId)}/export${params}`);
}

export function erasePrivacyData(accountId: string, input: PrivacyEraseInput): Promise<PrivacyEraseResult> {
  return requestJson<PrivacyEraseResult>(`/privacy/accounts/${encodeURIComponent(accountId)}/erase`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      confirm_account_id: input.confirm_account_id,
      space_id: input.space_id || "",
      reason: input.reason || "",
    }),
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

// --- fleet (Mission Control) ---

export function getFleetOverview(): Promise<FleetOverview> {
  return requestJson<FleetOverview>("/fleet/overview");
}

export function listFleetRollouts(): Promise<FleetRollout[]> {
  return requestJson<FleetRollout[]>("/operator/fleet-rollouts");
}

export function createFleetRollout(input: CreateFleetRolloutInput): Promise<FleetRolloutCreateResult> {
  return requestJson<FleetRolloutCreateResult>("/operator/fleet-rollouts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

function fleetRolloutAction(id: string, action: "pause" | "resume" | "abort"): Promise<FleetRollout> {
  return requestJson<FleetRollout>(`/operator/fleet-rollouts/${encodeURIComponent(id)}/${action}`, { method: "POST" });
}

export const pauseFleetRollout = (id: string) => fleetRolloutAction(id, "pause");
export const resumeFleetRollout = (id: string) => fleetRolloutAction(id, "resume");
export const abortFleetRollout = (id: string) => fleetRolloutAction(id, "abort");

export function listFleetKeys(): Promise<FleetKeyInfo[]> {
  return requestJson<FleetKeyInfo[]>("/fleet/keys");
}

export function mintFleetKey(deploymentId: string, label = ""): Promise<MintedFleetKey> {
  return requestJson<MintedFleetKey>("/fleet/keys", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ deployment_id: deploymentId, label }),
  });
}

export function revokeFleetKey(id: string): Promise<{ revoked: string }> {
  return requestJson<{ revoked: string }>(`/fleet/keys/${encodeURIComponent(id)}/revoke`, { method: "POST" });
}

export function enrollDeployment(deploymentId: string): Promise<DeploymentEnrollment> {
  return requestJson<DeploymentEnrollment>(`/fleet/deployments/${encodeURIComponent(deploymentId)}/enroll`, {
    method: "POST",
  });
}
