// Chat surface: composer, streaming, saved-conversation loading, and the
// per-answer sources + cost footer.

import { askStream, getConversation } from "./api.js";
import { el, qs } from "./dom.js";
import { CLASS_COLORS, getConversationId, getWorkspaceScope, setConversationId } from "./state.js";

const SUGGESTIONS = [
  { q: "What are the opening hours?", hint: "public · everyone can see this" },
  { q: "How do I handle a refund?", hint: "internal · front-desk only" },
  { q: "What are the trainer salary bands?", hint: "restricted · HR only" },
  { q: "What was Q1 revenue by location?", hint: "confidential · finance only" },
];

let messages, input, form, sendBtn, busy = false;
let onConversationChange = () => {};

export function initChat({ onConversationChange: cb } = {}) {
  messages = qs("#messages");
  input = qs("#input");
  form = qs("#composer");
  sendBtn = qs("#send");
  if (cb) onConversationChange = cb;

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

export function newChat() {
  setConversationId(null);
  messages.replaceChildren();
  renderEmptyState();
  onConversationChange(null);
}

export async function loadConversation(id) {
  const conv = await getConversation(id, getWorkspaceScope()).catch(() => null);
  if (!conv) return;
  setConversationId(conv.id);
  messages.replaceChildren();
  for (const m of conv.messages) {
    if (m.role === "user") addUserMessage(m.content);
    else addAssistantStatic(m.content, m.meta || {});
  }
  scroll();
  onConversationChange(conv.id);
}

async function send(raw) {
  const question = raw.trim();
  if (!question || busy) return;
  busy = true;
  clearEmptyState();

  input.value = "";
  autoGrow();
  sendBtn.disabled = true;

  addUserMessage(question);
  const { answer, body } = addAssistantStreaming();
  scroll();

  try {
    await askStream(question, getConversationId(), getWorkspaceScope(), (event) => handleEvent(event, answer, body));
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
  if (event.type === "conversation") {
    setConversationId(event.id);
    onConversationChange(event.id);
  } else if (event.type === "token") {
    answer.textContent += event.text;
    scroll();
  } else if (event.type === "sources" && event.sources.length) {
    body.append(renderSources(event.sources));
  } else if (event.type === "meta") {
    body.append(el("div", { class: "answer-foot" }, metaLine(event)));
  }
}

// --- message rendering ------------------------------------------------
function addUserMessage(text) {
  messages.append(el("div", { class: "msg-row user" }, el("div", { class: "bubble-user" }, text)));
}

function addAssistantStreaming() {
  const answer = el("div", { class: "answer streaming" });
  const body = el("div", { class: "assistant-body" }, answer);
  messages.append(el("div", { class: "msg-row" },
    el("div", { class: "msg-assistant" }, el("div", { class: "avatar" }, "ob"), body)));
  return { answer, body };
}

function addAssistantStatic(content, meta) {
  const answer = el("div", { class: "answer" });
  answer.textContent = content;
  const body = el("div", { class: "assistant-body" }, answer);
  if (meta.sources && meta.sources.length) body.append(renderSources(meta.sources));
  if (meta.total_tokens || meta.cost_usd !== undefined) body.append(el("div", { class: "answer-foot" }, metaLine(meta)));
  messages.append(el("div", { class: "msg-row" },
    el("div", { class: "msg-assistant" }, el("div", { class: "avatar" }, "ob"), body)));
}

function renderSources(sources) {
  return el("div", { class: "sources" },
    ...sources.map((s) => el("span", { class: "chip", title: `${s.classification} · ${s.category} · ${s.location}` },
      el("span", { class: "doc-dot", style: `background:${CLASS_COLORS[s.classification] || "var(--muted)"}` }),
      s.title)));
}

function formatCost(usd) {
  if (usd === null || usd === undefined) return null;
  if (usd === 0) return "free";
  const cents = usd * 100;               // per-request cost is almost always a fraction of a cent
  if (cents < 0.01) return "<0.01¢";
  if (cents < 1) return `${cents.toFixed(2)}¢`;   // 0.14¢
  if (cents < 10) return `${cents.toFixed(1)}¢`;  // 4.2¢
  return `${Math.round(cents)}¢`;                  // 42¢
}

function metaLine(e) {
  const tokens = e.total_tokens ?? e.approx_tokens ?? 0;
  const approx = e.estimated ? "~" : "";
  const cost = formatCost(e.cost_usd);
  const parts = [];
  if (e.chunks_used !== undefined && e.chunks_used !== null) {
    parts.push(`answered from ${e.chunks_used} chunk${e.chunks_used === 1 ? "" : "s"}`);
  }
  parts.push(`${approx}${(tokens || 0).toLocaleString()} tokens`);
  if (cost) parts.push(`≈ ${cost}`);
  if (e.llm) parts.push(e.llm);
  return parts.join(" · ");
}

// --- empty state ------------------------------------------------------
function renderEmptyState() {
  const grid = el("div", { class: "suggestions" },
    ...SUGGESTIONS.map((s) => el("button", { class: "suggestion", type: "button", onclick: () => send(s.q) },
      el("b", {}, s.q), el("span", {}, s.hint))));
  messages.append(el("div", { class: "empty", id: "emptyState" },
    el("div", { class: "empty-mark" }, "ob"),
    el("h1", {}, "Ask onebrain"),
    el("p", {}, "It answers only from documents your current role is allowed to see, and remembers this conversation."),
    grid));
}

function clearEmptyState() { qs("#emptyState")?.remove(); }
function autoGrow() { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 200)}px`; }
function scroll() { messages.scrollTop = messages.scrollHeight; }
