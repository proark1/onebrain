"use client";

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type RefObject } from "react";
import {
  askStream,
  deleteConversation,
  getConversation,
  listConversations,
} from "@/lib/onebrain-client";
import type {
  AnswerMeta,
  ChatStreamEvent,
  ConversationSummary,
  SessionInfo,
  SourceRecord,
} from "@/lib/onebrain-types";

type UiMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  meta?: AnswerMeta;
  status?: "streaming" | "failed" | "complete";
};

type ChatShellProps = {
  initialConversations: ConversationSummary[];
  session: SessionInfo;
};

const FUTURE_NAV = ["Documents", "Spaces", "Privacy", "Operator"];

function messageId(prefix: string): string {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(date);
}

function formatCost(usd?: number | null): string {
  if (usd === null || usd === undefined) {
    return "";
  }
  if (usd === 0) {
    return "free";
  }
  const cents = usd * 100;
  if (cents < 0.01) {
    return "<0.01c";
  }
  if (cents < 1) {
    return `${cents.toFixed(2)}c`;
  }
  if (cents < 10) {
    return `${cents.toFixed(1)}c`;
  }
  return `${Math.round(cents)}c`;
}

function dedupeSources(sources: SourceRecord[] = []): SourceRecord[] {
  const seen = new Set<string>();
  return sources.filter((source) => {
    const key = [source.title, source.classification, source.category, source.location].join("|");
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function metaParts(meta?: AnswerMeta): string[] {
  if (!meta) {
    return [];
  }
  const parts: string[] = [];
  if (meta.chunks_used) {
    parts.push(`${meta.chunks_used} chunk${meta.chunks_used === 1 ? "" : "s"}`);
  }
  if (meta.total_tokens) {
    parts.push(`${meta.estimated ? "~" : ""}${meta.total_tokens.toLocaleString()} tokens`);
  }
  const cost = formatCost(meta.cost_usd);
  if (cost) {
    parts.push(cost);
  }
  if (meta.llm) {
    parts.push(meta.llm);
  }
  return parts;
}

export function ChatShell({ initialConversations, session }: ChatShellProps) {
  const [conversations, setConversations] = useState<ConversationSummary[]>(initialConversations);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingList, setLoadingList] = useState(false);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [error, setError] = useState("");
  const threadRef = useRef<HTMLDivElement>(null);

  const selectedConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === selectedId) ?? null,
    [conversations, selectedId],
  );

  async function refreshConversations(activeId = selectedId) {
    setLoadingList(true);
    try {
      const next = await listConversations();
      setConversations(next);
      if (activeId && !next.some((conversation) => conversation.id === activeId)) {
        setSelectedId(null);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load conversations.");
    } finally {
      setLoadingList(false);
    }
  }

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight, behavior: "smooth" });
  }, [messages]);

  async function openConversation(id: string) {
    if (busy) {
      return;
    }
    setSelectedId(id);
    setLoadingConversation(true);
    setError("");
    try {
      const conversation = await getConversation(id);
      setMessages(conversation.messages.map((message, index) => ({
        id: `${conversation.id}_${index}`,
        role: message.role === "user" ? "user" : "assistant",
        content: message.content,
        meta: message.meta,
        status: "complete",
      })));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load this conversation.");
    } finally {
      setLoadingConversation(false);
    }
  }

  function startNewChat() {
    if (busy) {
      return;
    }
    setSelectedId(null);
    setMessages([]);
    setError("");
  }

  async function removeConversation(id: string) {
    if (busy) {
      return;
    }
    setError("");
    try {
      await deleteConversation(id);
      if (selectedId === id) {
        setSelectedId(null);
        setMessages([]);
      }
      await refreshConversations(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not delete this conversation.");
    }
  }

  function appendAssistantToken(messageIdValue: string, token: string) {
    setMessages((current) => current.map((message) => (
      message.id === messageIdValue
        ? { ...message, content: `${message.content}${token}` }
        : message
    )));
  }

  function patchAssistant(messageIdValue: string, patch: Partial<UiMessage>) {
    setMessages((current) => current.map((message) => (
      message.id === messageIdValue ? { ...message, ...patch, meta: { ...message.meta, ...patch.meta } } : message
    )));
  }

  async function sendMessage() {
    const question = draft.trim();
    if (!question || busy) {
      return;
    }

    const assistantId = messageId("assistant");
    setDraft("");
    setBusy(true);
    setError("");
    setMessages((current) => [
      ...current,
      { id: messageId("user"), role: "user", content: question, status: "complete" },
      { id: assistantId, role: "assistant", content: "", status: "streaming", meta: {} },
    ]);

    try {
      await askStream({ question, conversation_id: selectedId }, (event: ChatStreamEvent) => {
        if (event.type === "conversation") {
          setSelectedId(event.id);
        } else if (event.type === "token") {
          appendAssistantToken(assistantId, event.text);
        } else if (event.type === "sources") {
          patchAssistant(assistantId, { meta: { sources: event.sources } });
        } else if (event.type === "meta") {
          patchAssistant(assistantId, { meta: event });
        } else if (event.type === "done") {
          patchAssistant(assistantId, { status: "complete" });
        }
      });
      patchAssistant(assistantId, { status: "complete" });
      await refreshConversations(selectedId);
    } catch (err) {
      patchAssistant(assistantId, {
        status: "failed",
        content: "The answer stream stopped before OneBrain finished. Try sending again.",
      });
      setError(err instanceof Error ? err.message : "The answer stream failed.");
    } finally {
      setBusy(false);
    }
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage();
    }
  }

  return (
    <main className="chatShell">
      <ConversationSidebar
        conversations={conversations}
        currentId={selectedId}
        loading={loadingList}
        onDelete={removeConversation}
        onNewChat={startNewChat}
        onSelect={openConversation}
        session={session}
      />

      <section className="chatMain">
        <header className="chatTopbar">
          <div>
            <p className="eyebrow">OneBrain chat</p>
            <h1>{selectedConversation?.title || "New conversation"}</h1>
          </div>
          <div className="scopePill">
            <span className="statusDot" />
            {session.role_label}
          </div>
        </header>

        {error ? <div className="inlineError">{error}</div> : null}

        <MessageThread
          busy={busy}
          loading={loadingConversation}
          messages={messages}
          threadRef={threadRef}
        />

        <Composer
          busy={busy}
          draft={draft}
          onChange={setDraft}
          onKeyDown={onComposerKeyDown}
          onSend={sendMessage}
        />
      </section>
    </main>
  );
}

function ConversationSidebar({
  conversations,
  currentId,
  loading,
  onDelete,
  onNewChat,
  onSelect,
  session,
}: {
  conversations: ConversationSummary[];
  currentId: string | null;
  loading: boolean;
  onDelete: (id: string) => Promise<void>;
  onNewChat: () => void;
  onSelect: (id: string) => Promise<void>;
  session: SessionInfo;
}) {
  return (
    <aside className="chatSidebar" aria-label="Chat navigation">
      <div className="brandBlock">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <p>{session.display_name || session.email}</p>
      </div>

      <button className="newChatButton" type="button" onClick={onNewChat}>
        New chat
      </button>

      <section className="sidebarSection">
        <div className="sectionHead">
          <span>Recent chats</span>
          <span>{conversations.length}</span>
        </div>
        <div className="conversationList">
          {loading ? <p className="mutedLine">Loading chats...</p> : null}
          {!loading && conversations.length === 0 ? <p className="mutedLine">No saved chats yet.</p> : null}
          {conversations.map((conversation) => (
            <div className={conversation.id === currentId ? "conversationItem active" : "conversationItem"} key={conversation.id}>
              <button type="button" onClick={() => void onSelect(conversation.id)}>
                <span>{conversation.title || "New chat"}</span>
                <small>{formatDate(conversation.updated_at)}</small>
              </button>
              <button
                aria-label={`Delete ${conversation.title || "chat"}`}
                className="deleteConversation"
                type="button"
                onClick={() => void onDelete(conversation.id)}
              >
                X
              </button>
            </div>
          ))}
        </div>
      </section>

      <nav className="futureNav" aria-label="Future sections">
        {FUTURE_NAV.map((item) => (
          <span aria-disabled="true" key={item}>{item}</span>
        ))}
      </nav>
    </aside>
  );
}

function MessageThread({
  busy,
  loading,
  messages,
  threadRef,
}: {
  busy: boolean;
  loading: boolean;
  messages: UiMessage[];
  threadRef: RefObject<HTMLDivElement | null>;
}) {
  if (loading) {
    return <div className="messageThread" ref={threadRef}><div className="emptyState">Loading conversation...</div></div>;
  }

  return (
    <div className="messageThread" ref={threadRef}>
      {messages.length === 0 ? <EmptyChat /> : null}
      {messages.map((message) => (
        <article className={`messageRow ${message.role}`} key={message.id}>
          <div className="messageBubble">
            <p>{message.content || (message.status === "streaming" ? "Thinking..." : "")}</p>
            {message.role === "assistant" ? <AnswerDetails meta={message.meta} status={message.status} /> : null}
          </div>
        </article>
      ))}
      {busy ? <div className="streamMarker">Streaming answer</div> : null}
    </div>
  );
}

function EmptyChat() {
  return (
    <div className="emptyState">
      <div className="emptyMark">ob</div>
      <h2>Ask from approved knowledge</h2>
      <p>Answers are generated only from documents your current role can access.</p>
      <div className="promptGrid">
        <span>What are the opening hours?</span>
        <span>How do I handle a refund?</span>
        <span>What changed in the latest policy?</span>
      </div>
    </div>
  );
}

function Composer({
  busy,
  draft,
  onChange,
  onKeyDown,
  onSend,
}: {
  busy: boolean;
  draft: string;
  onChange: (value: string) => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onSend: () => Promise<void>;
}) {
  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        void onSend();
      }}
    >
      <textarea
        aria-label="Ask OneBrain"
        disabled={busy}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Ask OneBrain from the knowledge you can access..."
        rows={1}
        value={draft}
      />
      <button disabled={busy || !draft.trim()} type="submit">
        {busy ? "Wait" : "Send"}
      </button>
    </form>
  );
}

function AnswerDetails({ meta, status }: { meta?: AnswerMeta; status?: UiMessage["status"] }) {
  const sources = dedupeSources(meta?.sources);
  const parts = metaParts(meta);
  if (!sources.length && !parts.length && status !== "failed") {
    return null;
  }

  return (
    <div className="answerDetails">
      {sources.length ? (
        <div className="sourceRail" aria-label="Sources">
          {sources.map((source) => (
            <span
              className={`sourceChip source-${source.classification || "internal"}`}
              key={`${source.title}-${source.classification}-${source.category}-${source.location}`}
              title={`${source.classification} / ${source.category} / ${source.location}`}
            >
              {source.title}
            </span>
          ))}
        </div>
      ) : null}
      {parts.length ? <p className="answerMeta">{parts.join(" / ")}</p> : null}
      {status === "failed" ? <p className="answerMeta danger">Stream failed</p> : null}
    </div>
  );
}
