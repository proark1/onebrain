// Client state. Identity now lives in an httpOnly session cookie (set at login),
// so the client only tracks the current conversation.

let conversationId = null;

export const getConversationId = () => conversationId;
export const setConversationId = (id) => { conversationId = id; };

// Classification -> colour, shared by sidebar and source chips.
export const CLASS_COLORS = {
  public: "var(--cls-public)",
  internal: "var(--cls-internal)",
  confidential: "var(--cls-confidential)",
  restricted: "var(--cls-restricted)",
};
