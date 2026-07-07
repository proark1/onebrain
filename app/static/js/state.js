// App session state: the current role + location, persisted to localStorage.
// These become request headers, standing in for real auth in the prototype.

const state = {
  role: localStorage.getItem("ob_role") || "front_desk",
  location: localStorage.getItem("ob_location") || "munich",
};

export const getState = () => ({ ...state });

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
