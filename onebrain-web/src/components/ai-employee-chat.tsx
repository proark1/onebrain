"use client";

import { useEffect, useMemo, useState } from "react";
import {
  createAiConversation,
  listAiMessages,
  streamAiTurn,
} from "@/lib/onebrain-client";
import type { AiEmployee, AiEmployeeConversation, AiEmployeeMessage } from "@/lib/onebrain-types";

type Props = {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  conversations: AiEmployeeConversation[];
  onConversationsChanged: () => Promise<void>;
};

export function AiEmployeeChat({ accountId, spaceId, agents, conversations, onConversationsChanged }: Props) {
  const [selectedId, setSelectedId] = useState(conversations[0]?.id ?? "");
  const [newEmployeeId, setNewEmployeeId] = useState("chief_of_staff");
  const [messages, setMessages] = useState<AiEmployeeMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [streamedAnswer, setStreamedAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.employee_id, agent])), [agents]);
  const activeSelectedId = selectedId || conversations[0]?.id || "";
  const selected = conversations.find((row) => row.id === activeSelectedId) ?? null;

  useEffect(() => {
    let active = true;
    if (!activeSelectedId) return () => { active = false; };
    void listAiMessages(activeSelectedId, accountId, spaceId)
      .then((rows) => { if (active) setMessages(rows); })
      .catch((reason: Error) => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, [accountId, activeSelectedId, spaceId]);

  async function createConversation() {
    setBusy(true);
    setError("");
    try {
      const agent = agentById.get(newEmployeeId);
      const created = await createAiConversation(
        accountId, spaceId, newEmployeeId, `Conversation with ${agent?.name || "AI employee"}`,
      );
      await onConversationsChanged();
      setSelectedId(created.id);
      setMessages([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Conversation could not be created.");
    } finally {
      setBusy(false);
    }
  }

  async function sendTurn() {
    const prompt = question.trim();
    if (!selected || !prompt || busy) return;
    setBusy(true);
    setError("");
    setQuestion("");
    setStreamedAnswer("");
    const optimistic: AiEmployeeMessage = {
      id: "local-pending-human-turn",
      conversation_id: selected.id,
      speaker_type: "human",
      speaker_id: "you",
      visibility: "shared",
      content: prompt,
      citations: [],
      run_id: "",
      created_at: "",
    };
    setMessages((current) => [...current, optimistic]);
    try {
      await streamAiTurn(selected.id, accountId, spaceId, prompt, (event) => {
        if (event.type === "text") setStreamedAnswer((current) => current + String(event.text ?? ""));
        if (event.type === "error") setError(String(event.message ?? "The employee could not answer."));
      });
      setMessages(await listAiMessages(selected.id, accountId, spaceId));
      setStreamedAnswer("");
      await onConversationsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The employee could not answer.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="aiChatLayout">
      <aside className="aiChatList">
        <header><span className="eyebrow">Direct desk</span><h2>Conversations</h2></header>
        <div className="aiNewConversation">
          <select aria-label="AI employee" onChange={(event) => setNewEmployeeId(event.target.value)} value={newEmployeeId}>
            {agents.filter((agent) => agent.status === "active").map((agent) => (
              <option key={agent.employee_id} value={agent.employee_id}>{agent.name} · {agent.role}</option>
            ))}
          </select>
          <button disabled={busy} onClick={createConversation} type="button">New chat</button>
        </div>
        <div className="aiConversationRows">
          {conversations.length ? conversations.map((conversation) => {
            const agent = agentById.get(conversation.employee_id);
            return (
              <button className={activeSelectedId === conversation.id ? "active" : ""} key={conversation.id} onClick={() => setSelectedId(conversation.id)} type="button">
                <span>{agent?.name || conversation.employee_id}</span>
                <small>{conversation.title}</small>
              </button>
            );
          }) : <p className="aiEmptyCopy">Choose an employee and open the first conversation.</p>}
        </div>
      </aside>

      <div className="aiChatStage">
        {selected ? (
          <>
            <header>
              <div><span className="eyebrow">{agentById.get(selected.employee_id)?.role}</span><h2>{agentById.get(selected.employee_id)?.name}</h2></div>
              <span className="aiConfigStamp">Character {selected.character_version_id.split("_").at(-1)} · persistent</span>
            </header>
            <div className="aiMessages" aria-live="polite">
              {messages.map((message) => (
                <article className={message.speaker_type === "human" ? "human" : "employee"} key={message.id}>
                  <span>{message.speaker_type === "human" ? "You" : agentById.get(message.speaker_id)?.name || "AI employee"}</span>
                  <p>{message.content}</p>
                  {message.citations.length ? <small>Sources · {message.citations.join(" · ")}</small> : null}
                </article>
              ))}
              {streamedAnswer ? <article className="employee streaming"><span>{agentById.get(selected.employee_id)?.name}</span><p>{streamedAnswer}</p></article> : null}
            </div>
            {error ? <p className="inlineError" role="alert">{error}</p> : null}
            <form className="aiChatComposer" onSubmit={(event) => { event.preventDefault(); void sendTurn(); }}>
              <textarea aria-label="Message" onChange={(event) => setQuestion(event.target.value)} placeholder={`Ask ${agentById.get(selected.employee_id)?.name || "this employee"} to analyze, plan, or draft…`} rows={3} value={question} />
              <button disabled={busy || !question.trim()} type="submit">{busy ? "Working…" : "Send"}</button>
            </form>
          </>
        ) : <div className="aiStageEmpty"><span>CHAT</span><h2>Open a persistent employee desk</h2><p>Each employee keeps a separate character version, model policy, history, and approved memory.</p></div>}
      </div>
    </section>
  );
}
