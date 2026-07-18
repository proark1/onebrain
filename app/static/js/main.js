// Entry point — auth gate, then wire chats, uploads, chat, and the doc sidebar.

import { approveDocument, getMe, listDocuments, listPending } from "./api.js";
import { renderUserBar, showLogin } from "./auth.js";
import { initChat, loadConversation, newChat } from "./chat.js";
import { initConversations, refreshConversations } from "./conversations.js";
import { el, qs, toast } from "./dom.js";
import { initOperator } from "./operator.js";
import { CLASS_COLORS, getWorkspaceScope } from "./state.js";
import { initWorkspace } from "./workspace.js";

async function refreshDocuments() {
  const docs = await listDocuments(getWorkspaceScope());
  const list = qs("#docList");
  qs("#docCount").textContent = docs.length;

  if (!docs.length) {
    list.replaceChildren(el("li", { class: "doc-meta", style: "padding:8px 9px" }, "No documents visible here."));
    return;
  }
  list.replaceChildren(...docs.map((doc) => el("li", { class: "doc-item", title: doc.title },
    el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[doc.classification] || "var(--muted)"}` }),
    el("div", { class: "doc-info" },
      el("div", { class: "doc-title" }, doc.title),
      el("div", { class: "doc-meta" }, `${doc.classification} · ${doc.category} · ${doc.location}`)))));
}

// The review queue: documents held in quarantine until a second, sufficiently
// cleared person approves them. Hidden entirely when there's nothing to review.
async function refreshReview() {
  let pending = [];
  try {
    pending = await listPending(getWorkspaceScope());
  } catch {
    pending = [];   // e.g. a role that can't review — just show nothing
  }
  const section = qs("#reviewSection");
  const list = qs("#reviewList");
  qs("#reviewCount").textContent = pending.length;
  section.hidden = pending.length === 0;
  if (!pending.length) { list.replaceChildren(); return; }

  list.replaceChildren(...pending.map((doc) => {
    const info = el("div", { class: "doc-info" },
      el("div", { class: "doc-title", title: doc.title }, doc.title),
      el("div", { class: "doc-meta" }, `${doc.classification} · ${doc.category} · ${doc.location}`));
    if (doc.has_pii) info.append(el("div", { class: "review-warn" }, "⚠ possible personal data"));

    const approve = el("button", { class: "review-approve", type: "button", title: "Approve & publish" }, "Approve");
    approve.addEventListener("click", async () => {
      approve.disabled = true;
      approve.textContent = "…";
      try {
        await approveDocument(doc.doc_id, getWorkspaceScope());
        toast(`Approved "${doc.title}"`);
        await Promise.all([refreshReview(), refreshDocuments()]);
      } catch (err) {
        toast(err.message);
        approve.disabled = false;
        approve.textContent = "Approve";
      }
    });
    return el("li", { class: "review-item" }, info, approve);
  }));
}

async function initApp(me) {
  qs("#loginScreen").hidden = true;
  qs(".app").hidden = false;
  renderUserBar(me);

  initChat({ onConversationChange: () => refreshConversations() });
  initConversations({ onSelect: (id) => loadConversation(id) });
  const operator = initOperator(me);
  await initWorkspace(me, {
    onChange: () => {
      operator.showChat();
      newChat();
      Promise.all([refreshDocuments(), refreshConversations(), refreshReview()])
        .catch((err) => toast(err.message));
    },
  });
  qs("#newChatBtn").addEventListener("click", () => {
    operator.showChat();
    newChat();
  });

  await Promise.all([refreshDocuments(), refreshConversations(), refreshReview()]);
}

async function main() {
  const me = await getMe();
  if (!me) { showLogin(); return; }
  await initApp(me);
}

main();
