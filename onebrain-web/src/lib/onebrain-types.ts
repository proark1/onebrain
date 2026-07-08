export type SessionInfo = {
  role_id: string;
  role_label: string;
  clearance: string;
  location_label: string;
  tenant_id: string;
  display_name: string;
  email: string;
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
