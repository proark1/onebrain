// Entry point — wires session, uploads, chat, and the document sidebar together.

import { listDocuments } from "./api.js";
import { el, qs } from "./dom.js";
import { initChat, systemNote } from "./chat.js";
import { initSession } from "./session.js";
import { initUpload } from "./upload.js";
import { CLASS_COLORS } from "./state.js";

async function refreshDocuments(note) {
  const docs = await listDocuments();
  const list = qs("#docList");
  qs("#docCount").textContent = docs.length;

  if (!docs.length) {
    list.replaceChildren(el("li", { class: "doc-meta", style: "padding:8px 9px" },
      "No documents visible to this role."));
  } else {
    list.replaceChildren(...docs.map((doc) => el("li", { class: "doc-item", title: doc.title },
      el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[doc.classification] || "var(--muted)"}` }),
      el("div", { class: "doc-info" },
        el("div", { class: "doc-title" }, doc.title),
        el("div", { class: "doc-meta" }, `${doc.classification} · ${doc.category} · ${doc.location}`)))));
  }
  if (note) systemNote(`Now acting as a different role — ${docs.length} documents are visible to you.`);
}

async function main() {
  initChat();
  initUpload({ onUploaded: () => refreshDocuments(false) });
  await initSession({ onChange: () => refreshDocuments(true) });
  await refreshDocuments(false);
}

main();
