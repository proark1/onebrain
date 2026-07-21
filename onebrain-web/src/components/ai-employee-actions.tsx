"use client";

import { useMemo, useState } from "react";
import { decideAiAction, executeAiAction } from "@/lib/onebrain-client";
import type { AiActionProposal, AiEmployee } from "@/lib/onebrain-types";

type Props = {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  actions: AiActionProposal[];
  onActionsChanged: () => Promise<void>;
};

export function AiEmployeeActions({ accountId, spaceId, agents, actions, onActionsChanged }: Props) {
  const [selectedId, setSelectedId] = useState(actions[0]?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.employee_id, agent])), [agents]);
  const selected = actions.find((row) => row.id === selectedId) ?? actions[0] ?? null;

  async function decide(decision: "approved" | "rejected" | "changes_requested" | "duplicate") {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      await decideAiAction(selected.id, accountId, spaceId, decision);
      await onActionsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The decision could not be recorded.");
    } finally {
      setBusy(false);
    }
  }

  async function execute() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      await executeAiAction(selected.id, accountId, spaceId);
      await onActionsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The action could not be executed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="aiApprovalLayout">
      <aside className="aiApprovalQueue">
        <header><span className="eyebrow">Human control</span><h2>Approval queue</h2><p>{actions.filter((row) => row.status === "proposed").length} waiting for review</p></header>
        {actions.map((action) => <button className={selected?.id === action.id ? "active" : ""} key={action.id} onClick={() => setSelectedId(action.id)} type="button"><span className={`aiRisk ${action.risk_level}`}>{action.risk_level}</span><strong>{action.payload_summary}</strong><small>{agentById.get(action.employee_id)?.name} · {action.status}</small></button>)}
        {!actions.length ? <p className="aiEmptyCopy">No action proposals are waiting.</p> : null}
      </aside>
      <div className="aiApprovalDetail">
        {selected ? <>
          <header><div><span className="eyebrow">{selected.action_type.replaceAll("_", " ")}</span><h2>{selected.payload_summary}</h2></div><span className={`aiDecisionStatus ${selected.status}`}>{selected.status.replaceAll("_", " ")}</span></header>
          <div className="aiApprovalFacts"><div><span>Employee</span><strong>{agentById.get(selected.employee_id)?.name}</strong></div><div><span>Target</span><strong>{selected.target_system}</strong></div><div><span>Approver</span><strong>{selected.required_approver_role.replaceAll("_", " ")}</strong></div><div><span>Expires</span><strong>{new Date(selected.expires_at).toLocaleString()}</strong></div></div>
          <p className="aiApprovalReason">{selected.reason}</p>
          <div className="aiPayload"><header><span>Exact normalized payload</span><code>{selected.payload_hash}</code></header><pre>{JSON.stringify(selected.payload, null, 2)}</pre></div>
          <div className="aiSourceChips">{selected.source_record_ids.map((id) => <span key={id}>Source · {id}</span>)}</div>
          {error ? <p className="inlineError" role="alert">{error}</p> : null}
          <footer className="aiApprovalControls">
            {selected.status === "proposed" || selected.status === "changes_requested" ? <>
              <button disabled={busy} onClick={() => void decide("approved")} type="button">Approve exact payload</button>
              <button className="quiet" disabled={busy} onClick={() => void decide("changes_requested")} type="button">Request changes</button>
              <button className="danger" disabled={busy} onClick={() => void decide("rejected")} type="button">Reject</button>
            </> : null}
            {selected.status === "approved" ? <button disabled={busy} onClick={() => void execute()} type="button">Execute approved action</button> : null}
          </footer>
        </> : <div className="aiStageEmpty"><span>APPROVE</span><h2>No proposal selected</h2><p>Reviewers always see the exact payload, source trail, hash, risk, approver, and expiry.</p></div>}
      </div>
    </section>
  );
}
