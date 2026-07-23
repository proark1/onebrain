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
  DevelopmentRetryInput,
  DocumentSummary,
  InstallPlatformAppInput,
  AccountingConfirmInput,
  AccountingDocument,
  AccountingOverview,
  AccountingWorkspace,
  CreateKpiDefinitionInput,
  CreateKpiSnapshotInput,
  KpiDashboard,
  KpiDefinition,
  KpiIngestResult,
  KpiWorkspace,
  OperatorBackup,
  OperatorCustomer,
  OperatorDeployment,
  OperatorHealth,
  OperatorModule,
  OperatorProductModulesResult,
  OperatorObservability,
  OperatorRelease,
  CustomerTeardownExecuted,
  CustomerTeardownRequest,
  CustomerTeardownRequestCreated,
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
  ProvisioningModuleCatalog,
  ProvisioningResult,
  BootstrapSecretResult,
  ServiceKeyInfo,
  UploadDocumentInput,
  UpdateKpiDefinitionInput,
  FleetOverview,
  FleetRollout,
  FleetRolloutCreateResult,
  CreateFleetRolloutInput,
  FleetKeyInfo,
  MintedFleetKey,
  DeploymentEnrollment,
  DevelopmentGate,
  DevelopmentGatePreparation,
  AiActionProposal,
  AiCharacterVersion,
  AiConnectorBinding,
  AiConnectorHealth,
  AiEmployeeConversation,
  AiEmployeeMessage,
  AiEmployeeStreamEvent,
  AiEmployeeTeam,
  AiEmployeeWorkspace,
  AiMission,
  AiModels,
  AiWorkProduct,
  CreateManagedUserInput,
  ManagedUserDirectory,
  ManagedUserMutationResult,
  UserManagementJob,
  UserManagementResult,
} from "@/lib/onebrain-types";
import { describeFailure } from "@/lib/describe-failure";
import { cleanScope, scopeQuery } from "@/lib/onebrain-types";

// Caddy proxies this same-origin namespace directly to the API and overwrites
// X-Forwarded-For from the socket peer.  Do not route browser calls through
// the Next.js server: doing so would make every user share the web container's
// address for login and abuse limits.
const PROXY_BASE = "/api";

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${PROXY_BASE}${path}`, init);
  if (!response.ok) {
    throw new Error(await describeFailure(path, response));
  }
  return response.json() as Promise<T>;
}

function aiScopeQuery(accountId: string, spaceId: string, extra: Record<string, string> = {}): string {
  return new URLSearchParams({ account_id: accountId, space_id: spaceId, ...extra }).toString();
}

async function streamSse(
  path: string,
  body: Record<string, unknown>,
  onEvent: (event: AiEmployeeStreamEvent) => void,
): Promise<void> {
  const response = await fetch(`${PROXY_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok || !response.body) {
    throw new Error(await describeFailure(path, response));
  }
  const reader = response.body.getReader();
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
      if (!line.startsWith("data:")) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as AiEmployeeStreamEvent);
      } catch {
        // Ignore a malformed provider event while preserving the live stream.
      }
    }
  }
}

export function listAiEmployeeWorkspaces(): Promise<AiEmployeeWorkspace[]> {
  return requestJson<AiEmployeeWorkspace[]>("/ai-employees/workspaces");
}

export function getAiEmployeeTeam(accountId: string, spaceId: string): Promise<AiEmployeeTeam> {
  return requestJson<AiEmployeeTeam>(`/ai-employees/team?${aiScopeQuery(accountId, spaceId)}`);
}

export function setAiEmployeeStatus(
  employeeId: string,
  accountId: string,
  spaceId: string,
  status: "active" | "paused",
) {
  return requestJson(`/ai-employees/agents/${encodeURIComponent(employeeId)}/status`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId, space_id: spaceId, status }),
  });
}

export function listAiConversations(accountId: string, spaceId: string): Promise<AiEmployeeConversation[]> {
  return requestJson<AiEmployeeConversation[]>(
    `/ai-employees/conversations?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function createAiConversation(
  accountId: string,
  spaceId: string,
  employeeId: string,
  title: string,
): Promise<AiEmployeeConversation> {
  return requestJson<AiEmployeeConversation>("/ai-employees/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId, space_id: spaceId, employee_id: employeeId, title }),
  });
}

export function listAiMessages(
  conversationId: string,
  accountId: string,
  spaceId: string,
): Promise<AiEmployeeMessage[]> {
  return requestJson<AiEmployeeMessage[]>(
    `/ai-employees/conversations/${encodeURIComponent(conversationId)}/messages?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function streamAiTurn(
  conversationId: string,
  accountId: string,
  spaceId: string,
  question: string,
  onEvent: (event: AiEmployeeStreamEvent) => void,
) {
  return streamSse(
    `/ai-employees/conversations/${encodeURIComponent(conversationId)}/turns`,
    { account_id: accountId, space_id: spaceId, question, idempotency_key: crypto.randomUUID() },
    onEvent,
  );
}

export function listAiMissions(accountId: string, spaceId: string): Promise<AiMission[]> {
  return requestJson<AiMission[]>(`/ai-employees/missions?${aiScopeQuery(accountId, spaceId)}`);
}

export function getAiMission(missionId: string, accountId: string, spaceId: string): Promise<AiMission> {
  return requestJson<AiMission>(
    `/ai-employees/missions/${encodeURIComponent(missionId)}?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function createAiMission(input: {
  account_id: string;
  space_id: string;
  goal: string;
  accountable_employee_id: string;
  participant_ids: string[];
  token_budget?: number;
  time_budget_seconds?: number;
  cost_budget_usd?: number;
}): Promise<AiMission> {
  return requestJson<AiMission>("/ai-employees/missions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function streamAiMission(
  missionId: string,
  accountId: string,
  spaceId: string,
  onEvent: (event: AiEmployeeStreamEvent) => void,
) {
  return streamSse(
    `/ai-employees/missions/${encodeURIComponent(missionId)}/run`,
    { account_id: accountId, space_id: spaceId },
    onEvent,
  );
}

export function cancelAiMission(missionId: string, accountId: string, spaceId: string): Promise<AiMission> {
  return requestJson<AiMission>(`/ai-employees/missions/${encodeURIComponent(missionId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId, space_id: spaceId }),
  });
}

export function listAiWorkProducts(accountId: string, spaceId: string): Promise<AiWorkProduct[]> {
  return requestJson<AiWorkProduct[]>(`/ai-employees/work-products?${aiScopeQuery(accountId, spaceId)}`);
}

export function listAiActions(accountId: string, spaceId: string): Promise<AiActionProposal[]> {
  return requestJson<AiActionProposal[]>(`/ai-employees/actions?${aiScopeQuery(accountId, spaceId)}`);
}

export function decideAiAction(
  proposalId: string,
  accountId: string,
  spaceId: string,
  decision: "approved" | "rejected" | "changes_requested" | "duplicate",
  note = "",
): Promise<AiActionProposal> {
  return requestJson<AiActionProposal>(`/ai-employees/actions/${encodeURIComponent(proposalId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId, space_id: spaceId, decision, note }),
  });
}

export function executeAiAction(
  proposalId: string,
  accountId: string,
  spaceId: string,
): Promise<AiActionProposal> {
  return requestJson<AiActionProposal>(`/ai-employees/actions/${encodeURIComponent(proposalId)}/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ account_id: accountId, space_id: spaceId }),
  });
}

export function listAiConnectors(accountId: string, spaceId: string): Promise<AiConnectorBinding[]> {
  return requestJson<AiConnectorBinding[]>(`/ai-employees/connectors?${aiScopeQuery(accountId, spaceId)}`);
}

export function getAiConnectorHealth(accountId: string, spaceId: string): Promise<AiConnectorHealth[]> {
  return requestJson<AiConnectorHealth[]>(
    `/ai-employees/connectors/health?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function startGoogleCalendarOAuth(input: {
  account_id: string;
  space_id: string;
  employee_ids: string[];
  capabilities: string[];
  resource_ids: string[];
}): Promise<{ authorization_url: string; state_expires_at: number }> {
  return requestJson("/ai-employees/connectors/google-calendar/oauth/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function completeGoogleCalendarOAuth(input: {
  account_id: string;
  space_id: string;
  state: string;
  code: string;
}): Promise<AiConnectorBinding> {
  return requestJson<AiConnectorBinding>("/ai-employees/connectors/google-calendar/oauth/callback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function revokeGoogleCalendar(
  bindingId: string,
  accountId: string,
  spaceId: string,
): Promise<AiConnectorBinding> {
  return requestJson<AiConnectorBinding>(
    `/ai-employees/connectors/google-calendar/${encodeURIComponent(bindingId)}?${aiScopeQuery(accountId, spaceId)}`,
    { method: "DELETE" },
  );
}

export function listGoogleCalendars(
  bindingId: string,
  accountId: string,
  spaceId: string,
): Promise<{ id: string; summary: string; primary: boolean; access_role: string }[]> {
  return requestJson(
    `/ai-employees/connectors/google-calendar/${encodeURIComponent(bindingId)}/calendars?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function configureGoogleCalendar(
  bindingId: string,
  input: {
    account_id: string;
    space_id: string;
    employee_ids: string[];
    capabilities: string[];
    resource_ids: string[];
  },
): Promise<AiConnectorBinding> {
  return requestJson<AiConnectorBinding>(
    `/ai-employees/connectors/google-calendar/${encodeURIComponent(bindingId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export function getAiModels(accountId: string, spaceId: string): Promise<AiModels> {
  return requestJson<AiModels>(`/ai-employees/models?${aiScopeQuery(accountId, spaceId)}`);
}

export function listAiCharacterVersions(
  employeeId: string,
  accountId: string,
  spaceId: string,
): Promise<AiCharacterVersion[]> {
  return requestJson<AiCharacterVersion[]>(
    `/ai-employees/agents/${encodeURIComponent(employeeId)}/character/versions?${aiScopeQuery(accountId, spaceId)}`,
  );
}

export function createAiCharacterDraft(
  employeeId: string,
  input: Record<string, unknown> & { account_id: string; space_id: string },
): Promise<AiCharacterVersion> {
  return requestJson<AiCharacterVersion>(
    `/ai-employees/agents/${encodeURIComponent(employeeId)}/character/drafts`,
    { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) },
  );
}

export function publishAiCharacter(
  employeeId: string,
  versionId: string,
  accountId: string,
  spaceId: string,
  expectedProfileVersionId: string,
): Promise<AiCharacterVersion> {
  return requestJson<AiCharacterVersion>(
    `/ai-employees/agents/${encodeURIComponent(employeeId)}/character/versions/${encodeURIComponent(versionId)}/publish`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: accountId,
        space_id: spaceId,
        expected_profile_version_id: expectedProfileVersionId,
      }),
    },
  );
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

export function getProvisioningModuleCatalog(): Promise<ProvisioningModuleCatalog> {
  return requestJson<ProvisioningModuleCatalog>("/provisioning/modules");
}

export function provisionCustomer(input: ProvisionCustomerInput): Promise<ProvisioningResult> {
  return requestJson<ProvisioningResult>("/provisioning/customers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      account_id: input.account_id?.trim() || null,
      account_kind: input.account_kind || "organization",
      module_ids: input.module_ids,
      default_locale: input.default_locale || "de",
      current_migration: input.current_migration?.trim() || "",
      customer_name: input.customer_name.trim(),
      deployment_id: input.deployment_id?.trim() || null,
      deployment_type: input.deployment_type,
      initial_version: input.initial_version.trim(),
      mint_integration_keys: input.mint_integration_keys ?? true,
      module_versions: input.module_versions || {},
      owner_email: input.owner_email.trim(),
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

export function activateOperatorProductModules(
  deploymentId: string,
  addModuleIds: string[],
): Promise<OperatorProductModulesResult> {
  return requestJson<OperatorProductModulesResult>(
    `/operator/deployments/${encodeURIComponent(deploymentId)}/product-modules`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ add_module_ids: addModuleIds }),
    },
  );
}

export function listOperatorReleases(): Promise<OperatorRelease[]> {
  return requestJson<OperatorRelease[]>("/operator/releases");
}

export function getDevelopmentGate(): Promise<DevelopmentGate> {
  return requestJson<DevelopmentGate>("/operator/development-gate");
}

export function designateDevelopmentGate(deploymentId: string): Promise<DevelopmentGate> {
  return requestJson<DevelopmentGate>(`/operator/development-gate/${encodeURIComponent(deploymentId)}`, {
    method: "PUT",
  });
}

export function prepareExistingDevelopmentGate(): Promise<DevelopmentGatePreparation> {
  return requestJson<DevelopmentGatePreparation>("/operator/development-gate/prepare-existing", {
    method: "POST",
  });
}

export function provisionDevelopmentGate(ownerEmail: string, dryRun = true): Promise<{ deployment: { id: string }; dry_run?: boolean }> {
  return requestJson<{ deployment: { id: string }; dry_run?: boolean }>("/operator/development-gate/provision", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ owner_email: ownerEmail, region: "nbg1", dry_run: dryRun }),
  });
}

export function retryDevelopmentRelease(
  version: string,
  input: DevelopmentRetryInput,
): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/retry-dev`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function uploadProductionSignature(
  version: string,
  signature: string,
  signingKeyId: string,
): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/production-signature`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ signature, signing_key_id: signingKeyId }),
  });
}

export function approveOperatorRelease(version: string, note = ""): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
}

export function pauseOperatorRelease(version: string, note: string): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
}

export function resumeOperatorRelease(version: string, note: string): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
}

export function yankOperatorRelease(version: string, note: string): Promise<OperatorRelease> {
  return requestJson<OperatorRelease>(`/operator/releases/${encodeURIComponent(version)}/yank`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ note }),
  });
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

export function createTeardownRequest(
  deploymentId: string,
  input: { legal_hold_evidence_ref: string; backup_retention_evidence_ref: string },
): Promise<CustomerTeardownRequestCreated> {
  return requestJson<CustomerTeardownRequestCreated>(
    `/operator/deployments/${encodeURIComponent(deploymentId)}/teardown-requests`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export function approveTeardownRequest(
  deploymentId: string,
  requestId: string,
  nonce: string,
): Promise<CustomerTeardownRequest> {
  return requestJson<CustomerTeardownRequest>(
    `/operator/deployments/${encodeURIComponent(deploymentId)}/teardown-requests/${encodeURIComponent(requestId)}/approvals`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nonce }),
    },
  );
}

export function executeTeardownRequest(
  deploymentId: string,
  requestId: string,
  confirmationPhrase: string,
): Promise<CustomerTeardownExecuted> {
  return requestJson<CustomerTeardownExecuted>(
    `/operator/deployments/${encodeURIComponent(deploymentId)}/teardown-requests/${encodeURIComponent(requestId)}/execute`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation_phrase: confirmationPhrase }),
    },
  );
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

export function listKpiWorkspaces(): Promise<KpiWorkspace[]> {
  return requestJson<KpiWorkspace[]>("/kpis/workspaces");
}

export function getKpiDashboard(
  accountId: string,
  spaceId: string,
  historyLimit = 30,
  includeArchived = false,
): Promise<KpiDashboard> {
  const params = new URLSearchParams({
    account_id: accountId,
    space_id: spaceId,
    history_limit: String(historyLimit),
    include_archived: String(includeArchived),
  });
  return requestJson<KpiDashboard>(`/kpis?${params.toString()}`);
}

export function createKpiDefinition(input: CreateKpiDefinitionInput): Promise<KpiDefinition> {
  return requestJson<KpiDefinition>("/kpis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function updateKpiDefinition(id: string, input: UpdateKpiDefinitionInput): Promise<KpiDefinition> {
  return requestJson<KpiDefinition>(`/kpis/${encodeURIComponent(id)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function createManualKpiSnapshot(id: string, input: CreateKpiSnapshotInput): Promise<KpiIngestResult> {
  return requestJson<KpiIngestResult>(`/kpis/${encodeURIComponent(id)}/snapshots`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

export function listAccountingWorkspaces(): Promise<AccountingWorkspace[]> {
  return requestJson<AccountingWorkspace[]>("/accounting/workspaces");
}

export function getAccountingOverview(accountId: string, spaceId: string): Promise<AccountingOverview> {
  const params = new URLSearchParams({ account_id: accountId, space_id: spaceId });
  return requestJson<AccountingOverview>(`/accounting?${params.toString()}`);
}

export function listAccountingDocuments(
  accountId: string,
  spaceId: string,
  status = "",
): Promise<AccountingDocument[]> {
  const params = new URLSearchParams({ account_id: accountId, space_id: spaceId });
  if (status) params.set("status", status);
  return requestJson<AccountingDocument[]>(`/accounting/documents?${params.toString()}`);
}

export function getAccountingDocument(
  accountId: string,
  spaceId: string,
  documentId: string,
): Promise<AccountingDocument> {
  const params = new URLSearchParams({ account_id: accountId, space_id: spaceId });
  return requestJson<AccountingDocument>(
    `/accounting/documents/${encodeURIComponent(documentId)}?${params.toString()}`,
  );
}

export function confirmAccountingDocuments(input: AccountingConfirmInput): Promise<AccountingDocument[]> {
  return requestJson<AccountingDocument[]>("/accounting/documents/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
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
    throw new Error(await describeFailure("/ask", response));
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

export function refreshManagedUserDirectory(
  deploymentId: string,
  includeDeleted = false,
): Promise<UserManagementJob<ManagedUserDirectory>> {
  return requestJson(`/operator/user-management/deployments/${encodeURIComponent(deploymentId)}/directory`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ include_deleted: includeDeleted }),
  });
}

export function getUserManagementJob<T = Record<string, unknown>>(jobId: string): Promise<UserManagementJob<T>> {
  return requestJson(`/operator/user-management/jobs/${encodeURIComponent(jobId)}`);
}

export function createManagedUser(
  deploymentId: string,
  input: CreateManagedUserInput,
): Promise<UserManagementJob<ManagedUserMutationResult>> {
  return requestJson(`/operator/user-management/deployments/${encodeURIComponent(deploymentId)}/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
}

function managedUserAction(
  deploymentId: string,
  userId: string,
  action: "reset-password" | "disable" | "enable",
): Promise<UserManagementJob<ManagedUserMutationResult>> {
  return requestJson(
    `/operator/user-management/deployments/${encodeURIComponent(deploymentId)}/users/${encodeURIComponent(userId)}/${action}`,
    { method: "POST" },
  );
}

export const resetManagedUserPassword = (deploymentId: string, userId: string) =>
  managedUserAction(deploymentId, userId, "reset-password");
export const disableManagedUser = (deploymentId: string, userId: string) =>
  managedUserAction(deploymentId, userId, "disable");
export const enableManagedUser = (deploymentId: string, userId: string) =>
  managedUserAction(deploymentId, userId, "enable");

export function deleteManagedUser(
  deploymentId: string,
  userId: string,
): Promise<UserManagementJob<ManagedUserMutationResult>> {
  return requestJson(
    `/operator/user-management/deployments/${encodeURIComponent(deploymentId)}/users/${encodeURIComponent(userId)}`,
    { method: "DELETE" },
  );
}

export function revealManagedUserSecret(
  jobId: string,
): Promise<UserManagementResult<ManagedUserMutationResult>> {
  return requestJson(`/operator/user-management/jobs/${encodeURIComponent(jobId)}/reveal`, { method: "POST" });
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
