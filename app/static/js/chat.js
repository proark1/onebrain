// Chat surface: composer handling, streaming render, sources + efficiency note.

import { askStream } from "./api.js";
import { el, qs } from "./dom.js";
import { CLASS_COLORS } from "./state.js";

const SUGGESTIONS = [
  { q: "What are the opening hours?", hint: "public · everyone can see this" },
  { q: "How do I handle a refund?", hint: "internal · front-desk only" },
  { q: "What are the trainer salary bands?", hint: "restricted · HR only" },
  { q: "What was Q1 revenue by location?", hint: "confidential · finance only" },
];

let messages, input, form, sendBtn, busy = false;

export function initChat() {
  messages = qs("#messages");
  input = qs("#input");
  form = qs("#composer");
  sendBtn = qs("#send");

  renderEmptyState();
  autoGrow();

  input.addEventListener("input", () => {
    autoGrow();
    sendBtn.disabled = !input.value.trim() || busy;
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); send(input.value); });
  sendBtn.disabled = true;
}

// A subtle system line, e.g. after switching role.
export function systemNote(text) {
  clearEmptyState();
  messages.append(el("div", { class: "msg-row" },
    el("div", { class: "answer-foot", style: "text-align:center" }, text)));
  scroll();
}

async function send(raw) {
  const question = raw.trim();
  if (!question || busy) return;
  busy = true;
  clearEmptyState();

  input.value = "";
  autoGrow();
  sendBtn.disabled = true;

  messages.append(el("div", { class: "msg-row user" }, el("div", { class: "bubble-user" }, question)));

  const answer = el("div", { class: "answer streaming" });
  const body = el("div", { class: "assistant-body" }, answer);
  messages.append(el("div", { class: "msg-row" },
    el("div", { class: "msg-assistant" }, el("div", { class: "avatar" }, "ob"), body)));
  scroll();

  try {
    await askStream(question, (event) => handleEvent(event, answer, body));
  } catch (err) {
    answer.textContent = "Something went wrong reaching the brain. Is the server running?";
  } finally {
    answer.classList.remove("streaming");
    busy = false;
    sendBtn.disabled = !input.value.trim();
    scroll();
  }
}

function handleEvent(event, answer, body) {
  if (event.type === "token") {
    answer.textContent += event.text;
    scroll();
  } else if (event.type === "sources" && event.sources.length) {
    body.append(renderSources(event.sources));
  } else if (event.type === "meta") {
    body.append(el("div", { class: "answer-foot" }, metaLine(event)));
  }
}

function formatCost(usd) {
  if (usd === null || usd === undefined) return null;
  if (usd === 0) return "free";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;       // fractions of a cent
  return `$${usd.toFixed(2)}`;
}

function metaLine(e) {
  const tokens = e.total_tokens ?? e.approx_tokens ?? 0;
  const approx = e.estimated ? "~" : "";
  const cost = formatCost(e.cost_usd);
  const parts = [
    `answered from ${e.chunks_used} chunk${e.chunks_used === 1 ? "" : "s"}`,
    `${approx}${tokens.toLocaleString()} tokens`,
  ];
  if (cost) parts.push(`≈ ${cost}`);
  parts.push(e.llm);
  return parts.join(" · ");
}

function renderSources(sources) {
  return el("div", { class: "sources" },
    ...sources.map((s) => el("span", { class: "chip", title: `${s.classification} · ${s.category} · ${s.location}` },
      el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[s.classification] || "var(--muted)"}` }),
      s.title)));
}

function renderEmptyState() {
  const grid = el("div", { class: "suggestions" },
    ...SUGGESTIONS.map((s) => el("button", {
      class: "suggestion", type: "button",
      onclick: () => send(s.q),
    }, el("b", {}, s.q), el("span", {}, s.hint))));

  messages.append(el("div", { class: "empty", id: "emptyState" },
    el("div", { class: "empty-mark" }, "ob"),
    el("h1", {}, "Ask onebrain"),
    el("p", {}, "It answers only from documents your current role is allowed to see. Try switching roles in the sidebar."),
    grid));
}

function clearEmptyState() {
  qs("#emptyState")?.remove();
}

function autoGrow() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 200)}px`;
}

function scroll() {
  messages.scrollTop = messages.scrollHeight;
}
