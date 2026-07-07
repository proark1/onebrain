// Entry point — auth gate, then wire chats, uploads, chat, and the doc sidebar.

import { getMe, listDocuments } from "./api.js";
import { renderUserBar, showLogin } from "./auth.js";
import { initChat, loadConversation, newChat } from "./chat.js";
import { initConversations, refreshConversations } from "./conversations.js";
import { el, qs } from "./dom.js";
import { CLASS_COLORS } from "./state.js";
import { initUpload } from "./upload.js";

async function refreshDocuments() {
  const docs = await listDocuments();
  const list = qs("#docList");
  qs("#docCount").textContent = docs.length;

  if (!docs.length) {
    list.replaceChildren(el("li", { class: "doc-meta", style: "padding:8px 9px" }, "No documents visible to your role."));
    return;
  }
  list.replaceChildren(...docs.map((doc) => el("li", { class: "doc-item", title: doc.title },
    el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[doc.classification] || "var(--muted)"}` }),
    el("div", { class: "doc-info" },
      el("div", { class: "doc-title" }, doc.title),
      el("div", { class: "doc-meta" }, `${doc.classification} · ${doc.category} · ${doc.location}`)))));
}

async function initApp(me) {
  qs("#loginScreen").hidden = true;
  qs(".app").hidden = false;
  renderUserBar(me);

  initChat({ onConversationChange: () => refreshConversations() });
  initConversations({ onSelect: (id) => loadConversation(id) });
  initUpload({ onUploaded: () => refreshDocuments() });
  qs("#newChatBtn").addEventListener("click", () => newChat());

  await Promise.all([refreshDocuments(), refreshConversations()]);
}

async function main() {
  const me = await getMe();
  if (!me) { showLogin(); return; }
  await initApp(me);
}

main();
