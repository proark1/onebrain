"use client";

import { useMemo, useState } from "react";
import type { AiEmployee, AiWorkProduct } from "@/lib/onebrain-types";

export function AiEmployeeWork({ agents, work }: { agents: AiEmployee[]; work: AiWorkProduct[] }) {
  const [selectedId, setSelectedId] = useState(work[0]?.id ?? "");
  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.employee_id, agent])), [agents]);
  const selected = work.find((row) => row.id === selectedId) ?? work[0] ?? null;
  return (
    <section className="aiWorkLayout">
      <header className="aiSectionLead"><div><span className="eyebrow">Internal work</span><h2>Reports, briefs, plans, and tasks</h2></div><p>Every artifact stays in its account and space with source provenance, classification, retention, and privacy controls.</p></header>
      {work.length ? <div className="aiWorkDesk">
        <div className="aiWorkIndex">{work.map((item) => <button className={selected?.id === item.id ? "active" : ""} key={item.id} onClick={() => setSelectedId(item.id)} type="button"><span>{item.record_type}</span><strong>{item.title}</strong><small>{agentById.get(item.employee_id)?.name || item.employee_id} · {item.classification}</small></button>)}</div>
        {selected ? <article className="aiDocumentPreview"><header><div><span className="eyebrow">{selected.record_type} · {selected.classification}</span><h2>{selected.title}</h2></div><span>{agentById.get(selected.employee_id)?.name}</span></header><p>{selected.content}</p><footer><span>{selected.source_record_ids.length} source records</span><span>{new Date(selected.created_at).toLocaleString()}</span></footer></article> : null}
      </div> : <div className="aiEmptyPanel"><span>WORK</span><h2>No employee work products yet</h2><p>Approved reports, briefs, plans, policies, and tasks will appear here with their source trail.</p></div>}
    </section>
  );
}
