// The role / location switcher — the heart of the gating demo.
// Changing role re-fetches the visible document list, so you *see* access change.

import { getLocations, getMe, getRoles } from "./api.js";
import { qs } from "./dom.js";
import { getState, setLocation, setRole } from "./state.js";

export async function initSession({ onChange }) {
  const roleSelect = qs("#roleSelect");
  const locationSelect = qs("#locationSelect");
  const uploadLocation = qs("#uploadLocation");
  const { role, location } = getState();

  const [roles, locations] = await Promise.all([getRoles(), getLocations()]);

  roleSelect.replaceChildren(
    ...roles.map((r) => new Option(r.label, r.id, false, r.id === role)),
  );
  const locationOptions = (withGlobal) => [
    ...(withGlobal ? [new Option("Global", "global")] : []),
    ...locations.map((l) => new Option(capitalize(l), l)),
  ];
  locationSelect.replaceChildren(...locationOptions(false));
  locationSelect.value = location;
  uploadLocation.replaceChildren(...locationOptions(true));

  roleSelect.addEventListener("change", async () => {
    setRole(roleSelect.value);
    await refreshMe();
    onChange();
  });
  locationSelect.addEventListener("change", async () => {
    setLocation(locationSelect.value);
    await refreshMe();
    onChange();
  });

  await refreshMe();
}

async function refreshMe() {
  const me = await getMe();
  qs("#clearanceNote").textContent = `${me.role_label} · ${me.clearance} clearance`;
  qs("#locationField").style.display = me.location_label === "all locations" ? "none" : "";
}

const capitalize = (s) => s.charAt(0).toUpperCase() + s.slice(1);
