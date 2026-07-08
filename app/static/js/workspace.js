// Account/space selector for admin workspaces.

import { listPlatformAccounts, listPlatformSpaces } from "./api.js";
import { qs, toast } from "./dom.js";
import { getWorkspaceScope, setWorkspaceScope } from "./state.js";

const KIND_LABELS = {
  personal: "Personal",
  business: "Business",
  customer_service: "Customer service",
  shared: "Shared",
  family: "Family",
};

const option = (value, label) => {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
};

function spaceLabel(space) {
  const kind = KIND_LABELS[space.kind] || space.kind || "Space";
  return `${space.name || space.id} (${kind})`;
}

export async function initWorkspace(me, { onChange } = {}) {
  const panel = qs("#workspacePanel");
  if (!panel || me.role_id !== "admin") return;

  const accountSelect = qs("#workspaceAccount");
  const spaceSelect = qs("#workspaceSpace");
  const badge = qs("#workspaceBadge");

  try {
    const accounts = await listPlatformAccounts();
    const account = accounts.find((item) => item.id === me.tenant_id);
    if (!account) {
      setWorkspaceScope();
      panel.hidden = true;
      return;
    }

    const spaces = await listPlatformSpaces(account.id);
    accountSelect.replaceChildren(option(account.id, account.name || account.id));
    spaceSelect.replaceChildren(
      option("", "All visible data"),
      ...spaces.map((space) => option(space.id, spaceLabel(space))),
    );

    const current = getWorkspaceScope();
    if (current.account_id === account.id && spaces.some((space) => space.id === current.space_id)) {
      spaceSelect.value = current.space_id;
    } else if (spaces.length) {
      spaceSelect.value = spaces[0].id;
      setWorkspaceScope({ account_id: account.id, space_id: spaces[0].id });
    } else {
      setWorkspaceScope();
      spaceSelect.value = "";
    }

    const sync = () => {
      if (spaceSelect.value) {
        setWorkspaceScope({ account_id: account.id, space_id: spaceSelect.value });
        const selected = spaces.find((space) => space.id === spaceSelect.value);
        badge.textContent = selected?.kind ? (KIND_LABELS[selected.kind] || selected.kind) : "Space";
      } else {
        setWorkspaceScope();
        badge.textContent = "All";
      }
    };

    sync();
    spaceSelect.addEventListener("change", () => {
      sync();
      if (onChange) onChange();
    });
    panel.hidden = false;
  } catch (err) {
    setWorkspaceScope();
    panel.hidden = true;
    toast(err.message || "Workspace selector unavailable");
  }
}
