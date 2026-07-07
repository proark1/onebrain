// The "Recent chats" sidebar — lists saved conversations for the current
// (device, tenant, role), lets you open or delete them.

import { deleteConversation, getConversations } from "./api.js";
import { el, qs, toast } from "./dom.js";
import { getConversationId } from "./state.js";

let onSelect = () => {};

export function initConversations({ onSelect: cb } = {}) {
  if (cb) onSelect = cb;
}

export async function refreshConversations() {
  const list = qs("#chatList");
  const convs = await getConversations().catch(() => []);
  const active = getConversationId();

  if (!convs.length) {
    list.replaceChildren(el("li", { class: "chat-empty" }, "No saved chats yet."));
    return;
  }

  list.replaceChildren(...convs.map((c) =>
    el("li", { class: `chat-item${c.id === active ? " active" : ""}`, title: c.title },
      el("span", { class: "chat-title", onclick: () => onSelect(c.id) }, c.title || "New chat"),
      el("button", {
        class: "chat-del", type: "button", "aria-label": "Delete chat",
        onclick: async (e) => {
          e.stopPropagation();
          await deleteConversation(c.id);
          toast("Chat deleted");
          refreshConversations();
        },
      }, "✕"))));
}
