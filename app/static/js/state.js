// Client state. Identity now lives in an httpOnly session cookie (set at login),
// so the client only tracks the current conversation.

let conversationId = null;
let workspaceScope = { account_id: "", space_id: "" };

export const getConversationId = () => conversationId;
export const setConversationId = (id) => { conversationId = id; };

export const getWorkspaceScope = () => ({ ...workspaceScope });
export const hasWorkspaceScope = () => Boolean(workspaceScope.account_id && workspaceScope.space_id);
export function setWorkspaceScope(scope = {}) {
  const accountId = (scope.account_id || "").trim();
  const spaceId = (scope.space_id || "").trim();
  workspaceScope = accountId && spaceId
    ? { account_id: accountId, space_id: spaceId }
    : { account_id: "", space_id: "" };
}

// Classification -> colour, shared by sidebar and source chips.
export const CLASS_COLORS = {
  public: "var(--cls-public)",
  internal: "var(--cls-internal)",
  confidential: "var(--cls-confidential)",
  restricted: "var(--cls-restricted)",
};
