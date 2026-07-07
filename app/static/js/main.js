// Entry point — wires session, chats, uploads, chat, and the document sidebar.

import { listDocuments } from "./api.js";
import { initChat, loadConversation, newChat } from "./chat.js";
import { initConversations, refreshConversations } from "./conversations.js";
import { el, qs } from "./dom.js";
import { initSession } from "./session.js";
import { CLASS_COLORS } from "./state.js";
import { initUpload } from "./upload.js";

async function refreshDocuments() {
  const docs = await listDocuments();
  const list = qs("#docList");
  qs("#docCount").textContent = docs.length;

  if (!docs.length) {
    list.replaceChildren(el("li", { class: "doc-meta", style: "padding:8px 9px" }, "No documents visible to this role."));
    return;
  }
  list.replaceChildren(...docs.map((doc) => el("li", { class: "doc-item", title: doc.title },
    el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[doc.classification] || "var(--muted)"}` }),
    el("div", { class: "doc-info" },
      el("div", { class: "doc-title" }, doc.title),
      el("div", { class: "doc-meta" }, `${doc.classification} · ${doc.category} · ${doc.location}`)))));
}

// Switching role changes both what's visible and which saved chats apply, so
// reset to a fresh chat and refresh both lists.
async function onRoleChange() {
  newChat();
  await Promise.all([refreshDocuments(), refreshConversations()]);
}

async function main() {
  initChat({ onConversationChange: () => refreshConversations() });
  initConversations({ onSelect: (id) => loadConversation(id) });
  initUpload({ onUploaded: () => refreshDocuments() });
  qs("#newChatBtn").addEventListener("click", () => newChat());

  await initSession({ onChange: onRoleChange });
  await Promise.all([refreshDocuments(), refreshConversations()]);
}

main();
