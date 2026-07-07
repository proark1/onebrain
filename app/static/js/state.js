// App session state: role + location (become request headers), a persistent
// device session id (scopes saved chats), and the current conversation.

function makeSessionId() {
  let s = localStorage.getItem("ob_session");
  if (!s) {
    s = crypto.randomUUID ? crypto.randomUUID() : `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    localStorage.setItem("ob_session", s);
  }
  return s;
}

const state = {
  role: localStorage.getItem("ob_role") || "front_desk",
  location: localStorage.getItem("ob_location") || "munich",
  session: makeSessionId(),
  conversationId: null,
};

export const getState = () => ({ ...state });
export const getSession = () => state.session;
export const getConversationId = () => state.conversationId;
export const setConversationId = (id) => { state.conversationId = id; };

export function setRole(role) {
  state.role = role;
  localStorage.setItem("ob_role", role);
}

export function setLocation(location) {
  state.location = location;
  localStorage.setItem("ob_location", location);
}

// Classification -> colour, shared by sidebar and source chips.
export const CLASS_COLORS = {
  public: "var(--cls-public)",
  internal: "var(--cls-internal)",
  confidential: "var(--cls-confidential)",
  restricted: "var(--cls-restricted)",
};
