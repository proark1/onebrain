export type SessionInfo = {
  role_id: string;
  role_label: string;
  clearance: string;
  location_label: string;
  tenant_id: string;
  display_name: string;
  email: string;
  must_change_password: boolean;
  // Mission Control: operator_mode hides the customer surface (Ask/Knowledge/Apps/
  // Privacy) so the console reads as an admin-only overview.
  operator_mode: boolean;
  is_operator_surface: boolean;
};

export type AiEmployeeWorkspace = {
  account_id: string;
  account_name: string;
  space_id: string;
  space_name: string;
  space_kind: string;
  installation_status: string;
  can_configure: boolean;
  can_run_missions: boolean;
  can_manage_connectors: boolean;
};

export type AiEmployee = {
  profile_id: string;
  employee_id: string;
  name: string;
  fictional_age: number;
  country: string;
  pronouns: string;
  role: string;
  department: string;
  pod: string;
  reports_to: string;
  status: string;
  leadership_council: boolean;
  personality: string[];
  tone: string;
  strengths: string[];
  watch_outs: string[];
  working_style: string;
  biography: string;
  avatar_url: string;
  character_version_id: string;
  character_version: number;
  model_policy_id: string;
  model_provider: string;
  model: string;
  default_mode: string;
};

export type AiEmployeeTeam = {
  account_id: string;
  space_id: string;
  installation_status: string;
  contract_version: string;
  max_mission_squad_size: number;
  leadership_council_ids: string[];
  pods: Record<string, string[]>;
  can_configure: boolean;
  can_run_missions: boolean;
  can_manage_connectors: boolean;
  agents: AiEmployee[];
};

export type AiEmployeeConversation = {
  id: string;
  account_id: string;
  space_id: string;
  employee_id: string;
  human_owner_id: string;
  title: string;
  status: string;
  character_version_id: string;
  model_policy_id: string;
  mission_id: string;
  created_at: string;
  updated_at: string;
};

export type AiEmployeeMessage = {
  id: string;
  conversation_id: string;
  speaker_type: string;
  speaker_id: string;
  visibility: string;
  content: string;
  citations: string[];
  run_id: string;
  created_at: string;
};

export type AiMissionParticipant = {
  employee_id: string;
  mission_role: string;
  character_version_id: string;
  model_policy_id: string;
  status: string;
};

export type AiMission = {
  id: string;
  account_id: string;
  space_id: string;
  goal: string;
  sponsor_id: string;
  accountable_employee_id: string;
  status: string;
  phase: string;
  token_budget: number;
  time_budget_seconds: number;
  cost_budget_usd: number;
  synthesis_message_id: string;
  error: string;
  created_at: string;
  updated_at: string;
  participants: AiMissionParticipant[];
  usage: { prompt_tokens: number; completion_tokens: number; cost_usd: number };
  conversation_id?: string;
  messages?: AiEmployeeMessage[];
};

export type AiWorkProduct = {
  id: string;
  employee_id: string;
  record_type: string;
  title: string;
  content: string;
  classification: string;
  source_record_ids: string[];
  mission_id: string;
  conversation_id: string;
  created_at: string;
};

export type AiActionProposal = {
  id: string;
  mission_id: string;
  conversation_id: string;
  run_id: string;
  employee_id: string;
  action_type: string;
  target_system: string;
  risk_level: string;
  classification: string;
  actionability: string;
  source_record_ids: string[];
  payload_summary: string;
  payload: Record<string, unknown>;
  payload_hash: string;
  required_approver_role: string;
  expires_at: string;
  idempotency_key: string;
  status: string;
  requires_approval: boolean;
  reason: string;
  approved_by: string;
  approved_at: string;
  execution_ref: string;
  created_at: string;
  updated_at: string;
};

export type AiConnectorBinding = {
  id: string;
  provider: string;
  resource_type: string;
  resource_ids: string[];
  employee_ids: string[];
  capabilities: string[];
  status: string;
  created_at: string;
  updated_at: string;
};

export type AiConnectorHealth = {
  provider: string;
  available: boolean;
  reason: string;
  scopes: string[];
};

export type AiModelPosture = {
  employee_id: string;
  provider: string;
  model: string;
  data_ceiling: string;
  cost_limit_usd: number;
  status: string;
};

export type AiModels = {
  health: { provider: string; available: boolean; reason: string }[];
  policies: AiModelPosture[];
};

export type AiCharacterVersion = {
  id: string;
  employee_id: string;
  version: number;
  state: string;
  payload: Record<string, unknown>;
  checksum: string;
  author_id: string;
  base_version_id: string;
  created_at: string;
  published_at: string;
  preview: string;
};

export type AiEmployeeStreamEvent = {
  type: string;
  phase?: string;
  employee_id?: string;
  content?: string;
  message?: string;
  status?: string;
  mission_id?: string;
  [key: string]: unknown;
};

export type ConversationSummary = {
  id: string;
  title: string;
  updated_at: string;
};

export type MessageOut = {
  role: "user" | "assistant" | string;
  content: string;
  meta: AnswerMeta;
};

export type ConversationDetail = {
  id: string;
  title: string;
  messages: MessageOut[];
};

export type DocumentSummary = {
  doc_id: string;
  title: string;
  classification: string;
  location: string;
  category: string;
  chunks: number;
  status: string;
  pii_findings: number;
  account_id: string;
  space_id: string;
};

export type PendingDocument = {
  doc_id: string;
  title: string;
  classification: string;
  location: string;
  category: string;
  uploaded_by: string;
  has_pii: boolean;
  chunks: number;
  account_id: string;
  space_id: string;
};

export type ApproveDocumentResult = {
  approved: string;
  chunks: number;
  approved_by: string;
};

export type UploadDocumentInput = {
  category: string;
  classification: string;
  file: File;
  location: string;
};

export type PlatformAccount = {
  id: string;
  kind: string;
  name: string;
  owner_user_id: string;
  status: string;
};

export type PlatformSpace = {
  id: string;
  account_id: string;
  kind: string;
  name: string;
  status: string;
};

export type CreatePlatformAccountInput = {
  id?: string;
  kind: string;
  name: string;
};

export type CreatePlatformSpaceInput = {
  id?: string;
  kind: string;
  name: string;
};

export type PlatformAppInstallation = {
  id: string;
  account_id: string;
  app_id: string;
  enabled_space_ids: string[];
  allowed_purposes: string[];
  display_name: string;
  status: string;
};

export type InstallPlatformAppInput = {
  id?: string;
  app_id: string;
  display_name?: string;
  enabled_space_ids: string[];
  allowed_purposes: string[];
};

export type PlatformAccessCheckInput = {
  account_id: string;
  app_id: string;
  space_id: string;
  purpose: string;
};

export type PlatformAccessCheckResult = {
  allowed: boolean;
  reason: string;
};

export type PlatformAuditEvent = {
  id: string;
  account_id: string;
  actor_id: string;
  actor_type: string;
  action: string;
  target_type: string;
  target_id: string;
  space_id: string;
  app_id: string;
  purpose: string;
  decision: string;
  meta: Record<string, unknown>;
};

export type BrandTheme = {
  id: string;
  account_id: string;
  app_id: string;
  name: string;
  primary_color: string;
  secondary_color: string;
  accent_color: string;
  background_color: string;
  surface_color: string;
  text_color: string;
  muted_color: string;
  success_color: string;
  warning_color: string;
  danger_color: string;
  logo_url: string;
  source: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type BrandThemeInput = {
  name?: string;
  primary_color: string;
  secondary_color: string;
  accent_color: string;
  background_color: string;
  surface_color: string;
  text_color: string;
  muted_color: string;
  success_color: string;
  warning_color: string;
  danger_color: string;
  logo_url?: string;
};

export type ServiceKeyInfo = {
  id: string;
  tenant_id: string;
  scopes: string[];
  label: string;
  account_id: string;
  app_id: string;
  space_ids: string[];
  purposes: string[];
  status: string;
  last_used_at: string;
  last_used_endpoint: string;
  use_count: number;
  rotated_from_id: string;
  revoked_at: string;
};

export type ProvisioningBundle = {
  id: string;
  label: string;
  description: string;
  spaces: string[];
  apps: string[];
  modules: string[];
};

export type ProvisionCustomerInput = {
  account_id?: string;
  account_kind?: string;
  bundle_id: string;
  current_migration?: string;
  customer_name: string;
  deployment_id?: string;
  deployment_type: string;
  initial_version: string;
  mint_integration_keys?: boolean;
  module_versions?: Record<string, string>;
  owner_email: string;
  region?: string;
  release_ring: string;
  brand_theme?: BrandThemeInput;
  app_brand_themes?: Record<string, BrandThemeInput>;
  external_provisioning?: boolean;
  dry_run?: boolean;
  callback_url?: string;
};

export type ProvisionedCredential = {
  id: string;
  key: string;
  tenant_id: string;
  account_id: string;
  app_id: string;
  label: string;
  scopes: string[];
  space_ids: string[];
  purposes: string[];
};

export type ProvisioningResult = {
  bundle_id: string;
  account: {
    id: string;
    kind: string;
    name: string;
    owner_user_id: string;
  };
  spaces: Array<{ id: string; kind: string; name: string }>;
  apps: Array<{
    id: string;
    app_id: string;
    enabled_space_ids: string[];
    allowed_purposes: string[];
    display_name: string;
  }>;
  deployment: {
    id: string;
    customer_name: string;
    deployment_type: string;
    region: string;
    release_ring: string;
    current_version: string;
    current_migration: string;
  };
  modules: Array<{ module_id: string; version: string; status: string }>;
  credentials: ProvisionedCredential[];
  brand_theme: BrandTheme;
  app_brand_themes: BrandTheme[];
  provisioning_run?: ProvisioningRun | null;
};

export type ProvisioningRun = {
  id: string;
  account_id: string;
  deployment_id: string;
  bundle_id: string;
  requested_by: string;
  status: string;
  external_provider: string;
  external_run_id: string;
  external_run_url: string;
  target_id: string;
  target_environment: string;
  service_urls: Record<string, string>;
  migration_revision: string;
  smoke_status: string;
  failure_reason: string;
  bootstrap_secret_id: string;
  retry_of_run_id: string;
  created_at: string;
  updated_at: string;
  dispatched_at: string;
  completed_at: string;
  result_payload?: Record<string, unknown>;
};

export type BootstrapSecretResult = {
  secret_id: string;
  plaintext: string;
};

export type OperatorDeployment = {
  id: string;
  customer_name: string;
  environment: string;
  deployment_type: string;
  region: string;
  release_ring: string;
  status: string;
  current_version: string;
  current_migration: string;
  created_at: string;
  is_release_gate: boolean;
  current_version_deployed_at: string;
  last_heartbeat_at: string;
  last_heartbeat_healthy: boolean | null;
  last_reported_version: string;
  last_reported_migration: string;
};

export type OperatorModule = {
  deployment_id: string;
  module_id: string;
  version: string;
  status: string;
};

export type OperatorRelease = {
  version: string;
  git_sha: string;
  modules: Record<string, string>;
  migration_from: string;
  migration_to: string;
  security_notes: string;
  rollback_plan: string;
  status: string;
  created_at: string;
  images: Record<string, string>;
  rollback_kind: string;
  signature: string;
  signing_key_id: string;
  promotion: OperatorReleasePromotion | null;
};

export type OperatorPromotionEvent = {
  id: string;
  action: string;
  from_state: string;
  to_state: string;
  actor: string;
  note: string;
  created_at: string;
};

export type OperatorReleasePromotion = {
  state: string;
  gate_deployment_id: string;
  dev_rollout_id: string;
  dev_started_at: string;
  dev_completed_at: string;
  dev_verified_at: string;
  production_signature_attached: boolean;
  customer_approved_at: string;
  customer_approved_by: string;
  customer_paused_at: string;
  customer_paused_reason: string;
  failure_reason: string;
  events: OperatorPromotionEvent[];
};

export type DevelopmentGate = {
  deployment: OperatorDeployment | null;
  ready: boolean;
  blockers: string[];
};

export type OperatorBackup = {
  id: string;
  deployment_id: string;
  status: string;
  detail: string;
};

export type OperatorHealth = {
  id: string;
  deployment_id: string;
  status: string;
  detail: string;
};

export type OperatorRollout = {
  id: string;
  deployment_id: string;
  target_version: string;
  status: string;
  started_by: string;
  notes: string;
};

export type OperatorUpdatePlan = {
  deployment_id: string;
  target_version: string;
  allowed: boolean;
  reason: string;
  current_modules: Record<string, string>;
  target_modules: Record<string, string>;
  modules_to_update: Record<string, string>;
  rollback_kind: string;
  warnings: string[];
};

export type OperatorCustomer = {
  account: {
    id: string;
    kind: string;
    name: string;
    owner_user_id: string;
    status: string;
  };
  spaces: Array<{ id: string; kind: string; name: string; status: string }>;
  apps: Array<{
    id: string;
    app_id: string;
    display_name: string;
    enabled_space_ids: string[];
    allowed_purposes: string[];
    status: string;
  }>;
  brand_theme: BrandTheme;
  brand_themes: BrandTheme[];
  service_keys: ServiceKeyInfo[];
  deployment: OperatorDeployment | null;
  modules: OperatorModule[];
  backup: OperatorBackup | null;
  health: OperatorHealth | null;
  latest_rollout: OperatorRollout | null;
  readiness: string;
};

export type OperatorObservability = {
  generated_at: string;
  runtime: {
    vector_store: string;
    llm_provider: string;
    embeddings_provider: string;
    async_ingestion: boolean;
  };
  retrieval: {
    top_k: number;
    min_score: number;
  };
  storage: {
    chunks: number;
    intake_records: number;
  };
  service_keys: {
    total: number;
    active: number;
    revoked: number;
  };
  jobs: {
    total: number;
    by_status: Record<string, number>;
    by_type: Record<string, number>;
    recent_failures: Array<{
      id: string;
      type: string;
      tenant_id: string;
      account_id: string;
      space_id: string;
      attempts: number;
      max_attempts: number;
      error: string;
      created_at: string;
      updated_at: string;
      completed_at: string;
    }>;
  };
  security: {
    environment: string;
    production_like: boolean;
    pgvector_required: boolean;
    database_url_configured: boolean;
    rls_enforced: boolean;
    cookie_secure: boolean;
    pii_phase: string;
  };
  worker: {
    expected: boolean;
    pending_jobs: number;
    running_jobs: number;
    failed_jobs: number;
    status: string;
  };
  auth: {
    total_failures: number;
    login_failures: number;
    service_key_failures: number;
    lockouts: number;
    last_failure_at: string;
  };
  api: {
    errors_5xx: number;
    last_error_at: string;
    last_error_route: string;
    last_error_status: number;
  };
  alerts: Array<{
    id: string;
    severity: string;
    title: string;
    detail: string;
    action: string;
    signal: string;
  }>;
};

export type CreateOperatorReleaseInput = {
  version: string;
  git_sha: string;
  modules: Record<string, string>;
  migration_from?: string;
  migration_to?: string;
  security_notes?: string;
  rollback_plan?: string;
  status?: string;
};

export type OperatorRunInput = {
  id?: string;
  status: string;
  detail?: string;
};

export type OperatorRolloutStatusInput = {
  status: string;
  notes?: string;
};

export type KpiWorkspace = {
  account_id: string;
  account_name: string;
  space_id: string;
  space_name: string;
  space_kind: string;
  can_configure: boolean;
  can_write_manual: boolean;
};

export type KpiDefinition = {
  id: string;
  account_id: string;
  space_id: string;
  key: string;
  name: string;
  description: string;
  category: string;
  unit: string;
  source_label: string;
  owner_label: string;
  freshness_minutes: number;
  warning_min: string | null;
  warning_max: string | null;
  critical_min: string | null;
  critical_max: string | null;
  display_order: number;
  status: string;
  created_at: string;
  updated_at: string;
};

export type KpiSnapshot = {
  id: string;
  kpi_id: string;
  value: string;
  observed_at: string;
  received_at: string;
  source_ref: string;
};

export type KpiDashboardItem = {
  definition: KpiDefinition;
  latest: KpiSnapshot | null;
  previous: KpiSnapshot | null;
  history: KpiSnapshot[];
  absolute_delta: string | null;
  percentage_delta: string | null;
  threshold_state: "healthy" | "warning" | "critical" | "awaiting_data";
  freshness_state: "fresh" | "stale" | "awaiting_data";
};

export type KpiDashboard = {
  account_id: string;
  space_id: string;
  generated_at: string;
  can_configure: boolean;
  can_write_manual: boolean;
  items: KpiDashboardItem[];
};

export type CreateKpiDefinitionInput = {
  account_id: string;
  space_id: string;
  key: string;
  name: string;
  description?: string;
  category?: string;
  unit?: string;
  source_label?: string;
  owner_label?: string;
  freshness_minutes?: number;
  warning_min?: string | null;
  warning_max?: string | null;
  critical_min?: string | null;
  critical_max?: string | null;
  display_order?: number;
};

export type UpdateKpiDefinitionInput = Partial<Omit<CreateKpiDefinitionInput, "account_id" | "space_id">> & {
  account_id: string;
  space_id: string;
  status?: "active" | "archived";
};

export type CreateKpiSnapshotInput = {
  account_id: string;
  space_id: string;
  value: string;
  observed_at: string;
  source_ref?: string;
  idempotency_key: string;
};

export type KpiIngestResult = {
  accepted_count: number;
  duplicate_count: number;
  snapshots: KpiSnapshot[];
};

export type PrivacyAuditEvent = {
  id: string;
  account_id: string;
  actor_id: string;
  actor_type: string;
  action: string;
  target_type: string;
  target_id: string;
  space_id: string;
  purpose: string;
  decision: string;
  meta: Record<string, unknown>;
  created_at: string;
};

export type PrivacyExport = {
  account_id: string;
  space_id: string;
  exported_at: string;
  documents: Array<Record<string, unknown>>;
  conversations: Array<Record<string, unknown>>;
  intake_records: Array<Record<string, unknown>>;
  governance: Record<string, Array<Record<string, unknown>>>;
  kpis: Record<string, Array<Record<string, unknown>>>;
  audit_events: PrivacyAuditEvent[];
};

export type PrivacyEraseInput = {
  confirm_account_id: string;
  space_id?: string;
  reason?: string;
};

export type PrivacyEraseResult = {
  account_id: string;
  space_id: string;
  documents_deleted: number;
  chunks_deleted: number;
  conversations_deleted: number;
  intake_records_deleted: number;
  governance_deleted: Record<string, number>;
  kpis_deleted: Record<string, number>;
  audit_event_id: string;
};

export type SourceRecord = {
  title: string;
  classification: string;
  location: string;
  category: string;
  score: number;
};

export type AnswerMeta = {
  sources?: SourceRecord[];
  chunks_used?: number;
  retrieval_min_score?: number;
  best_score?: number | null;
  filtered_chunks?: number;
  history_user_turns_used?: number;
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
  cost_usd?: number | null;
  estimated?: boolean;
  llm?: string;
  [key: string]: unknown;
};

export type ChatScope = {
  account_id?: string;
  space_id?: string;
};

export type AskPayload = ChatScope & {
  question: string;
  conversation_id?: string | null;
};

export type ChatStreamEvent =
  | { type: "conversation"; id: string; title: string }
  | { type: "token"; text: string }
  | { type: "sources"; sources: SourceRecord[] }
  | ({ type: "meta" } & AnswerMeta)
  | { type: "done" };

export function cleanScope(scope: ChatScope = {}): ChatScope {
  const accountId = (scope.account_id || "").trim();
  const spaceId = (scope.space_id || "").trim();
  return accountId && spaceId ? { account_id: accountId, space_id: spaceId } : {};
}

export function scopeQuery(scope: ChatScope = {}): string {
  const clean = cleanScope(scope);
  if (!clean.account_id) {
    return "";
  }
  return `?${new URLSearchParams(clean as Record<string, string>).toString()}`;
}

// --- fleet (Mission Control) ---

export interface FleetDeploymentOverview {
  deployment_id: string;
  customer_name: string;
  environment: string;
  deployment_type: string;
  release_ring: string;
  status: string;
  current_version: string;
  created_at: string;
  current_version_deployed_at: string;
  is_release_gate: boolean;
  healthy: boolean | null;
  reported_version: string;
  migration_revision: string;
  last_reported_at: string;
  last_received_at: string;
  counts: Record<string, number>;
  open_alerts: string[];
}

export interface FleetOverview {
  generated_at: string;
  deployments: FleetDeploymentOverview[];
  total: number;
  healthy: number;
  with_open_alerts: number;
}

export interface FleetRollout {
  id: string;
  target_version: string;
  status: string;
  ring_order: string[];
  current_ring: string;
  failure_tolerance: number;
  started_by: string;
  notes: string;
  created_at: string;
  ring_batch_size: number;
  deployment_ids: string[];
  include_manual_pinned: boolean;
}

export interface FleetRolloutPlan {
  waves: Record<string, string[]>;
  skipped: string[];
  blocked: Record<string, string>;
}

export interface FleetRolloutCreateResult {
  fleet_rollout: FleetRollout | null;
  plan: FleetRolloutPlan;
}

export interface CreateFleetRolloutInput {
  target_version: string;
  callback_url: string;
  failure_tolerance: number;
  dry_run: boolean;
  deployment_ids: string[];
  ring_batch_size: number;
  include_manual_pinned: boolean;
}

export interface FleetKeyInfo {
  id: string;
  deployment_id: string;
  label: string;
  status: string;
  created_at: string;
  last_used_at: string;
}

export interface MintedFleetKey {
  id: string;
  deployment_id: string;
  label: string;
  token: string;
}

export interface DeploymentEnrollment {
  deployment_id: string;
  key_id: string;
  env: Record<string, string>;
}
