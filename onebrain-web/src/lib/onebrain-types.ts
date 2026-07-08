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

function cleanScope(scope: ChatScope = {}): ChatScope {
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
