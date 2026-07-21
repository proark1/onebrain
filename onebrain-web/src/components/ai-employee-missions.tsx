"use client";

import { useMemo, useState } from "react";
import {
  cancelAiMission,
  createAiMission,
  getAiMission,
  streamAiMission,
} from "@/lib/onebrain-client";
import type { AiEmployee, AiEmployeeStreamEvent, AiMission } from "@/lib/onebrain-types";

type Props = {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  missions: AiMission[];
  maxSquadSize: number;
  onMissionsChanged: () => Promise<void>;
};

const ACCOUNTABLE_IDS = new Set([
  "chief_of_staff", "chief_operating_officer", "chief_product_technology_officer", "chief_marketing_officer",
]);

export function AiEmployeeMissions({ accountId, spaceId, agents, missions, maxSquadSize, onMissionsChanged }: Props) {
  const [goal, setGoal] = useState("");
  const [accountableId, setAccountableId] = useState("chief_operating_officer");
  const [participantIds, setParticipantIds] = useState<string[]>(["chief_of_staff", "chief_operating_officer"]);
  const [selected, setSelected] = useState<AiMission | null>(missions[0] ?? null);
  const [events, setEvents] = useState<AiEmployeeStreamEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const agentById = useMemo(() => new Map(agents.map((agent) => [agent.employee_id, agent])), [agents]);

  function setAccountable(employeeId: string) {
    setAccountableId(employeeId);
    setParticipantIds((current) => current.includes(employeeId) ? current : [...current, employeeId].slice(0, maxSquadSize));
  }

  function toggleParticipant(employeeId: string) {
    if (employeeId === "chief_of_staff" || employeeId === accountableId) return;
    setParticipantIds((current) => current.includes(employeeId)
      ? current.filter((id) => id !== employeeId)
      : current.length < maxSquadSize ? [...current, employeeId] : current);
  }

  async function createMission() {
    if (!goal.trim()) return;
    setBusy(true);
    setError("");
    try {
      const created = await createAiMission({
        account_id: accountId,
        space_id: spaceId,
        goal: goal.trim(),
        accountable_employee_id: accountableId,
        participant_ids: participantIds,
      });
      setGoal("");
      setSelected(created);
      setEvents([]);
      await onMissionsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Mission could not be created.");
    } finally {
      setBusy(false);
    }
  }

  async function loadMission(missionId: string) {
    setBusy(true);
    setError("");
    try {
      setSelected(await getAiMission(missionId, accountId, spaceId));
      setEvents([]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Mission could not be loaded.");
    } finally {
      setBusy(false);
    }
  }

  async function runMission() {
    if (!selected || busy) return;
    setBusy(true);
    setError("");
    setEvents([]);
    try {
      await streamAiMission(selected.id, accountId, spaceId, (event) => {
        setEvents((current) => [...current, event]);
        if (event.type === "error") setError(String(event.message ?? "Mission failed."));
      });
      setSelected(await getAiMission(selected.id, accountId, spaceId));
      await onMissionsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Mission could not run.");
    } finally {
      setBusy(false);
    }
  }

  async function cancelMission() {
    if (!selected) return;
    setBusy(true);
    try {
      setSelected(await cancelAiMission(selected.id, accountId, spaceId));
      await onMissionsChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Mission could not be cancelled.");
    } finally {
      setBusy(false);
    }
  }

  const visibleTurns = events.filter((event) => event.type === "agent_turn");
  const storedMessages = selected?.messages ?? [];

  return (
    <section className="aiMissionLayout">
      <aside className="aiMissionBuilder">
        <header><span className="eyebrow">Mission brief</span><h2>Assemble a squad</h2><p>Clara orchestrates. One chief remains accountable. Specialists challenge once.</p></header>
        <label>Outcome<textarea onChange={(event) => setGoal(event.target.value)} placeholder="What must the team decide or deliver?" rows={4} value={goal} /></label>
        <label>Accountable chief<select onChange={(event) => setAccountable(event.target.value)} value={accountableId}>
          {agents.filter((agent) => ACCOUNTABLE_IDS.has(agent.employee_id)).map((agent) => <option key={agent.employee_id} value={agent.employee_id}>{agent.name} · {agent.role}</option>)}
        </select></label>
        <fieldset className="aiSquadPicker"><legend>Squad · {participantIds.length}/{maxSquadSize}</legend>
          {agents.map((agent) => {
            const required = agent.employee_id === "chief_of_staff" || agent.employee_id === accountableId;
            return <label key={agent.employee_id}><input checked={participantIds.includes(agent.employee_id)} disabled={required || (!participantIds.includes(agent.employee_id) && participantIds.length >= maxSquadSize)} onChange={() => toggleParticipant(agent.employee_id)} type="checkbox" /><span>{agent.name}<small>{agent.role}</small></span></label>;
          })}
        </fieldset>
        <button className="aiPrimaryAction" disabled={busy || !goal.trim()} onClick={createMission} type="button">Create mission</button>
      </aside>

      <div className="aiMissionDesk">
        <div className="aiMissionIndex">
          <header><span className="eyebrow">Mission ledger</span><strong>{missions.length} missions</strong></header>
          {missions.map((mission) => <button className={selected?.id === mission.id ? "active" : ""} key={mission.id} onClick={() => void loadMission(mission.id)} type="button"><span>{mission.goal}</span><small>{mission.status} · {mission.phase.replaceAll("_", " ")}</small></button>)}
          {!missions.length ? <p className="aiEmptyCopy">No missions yet. Build the first squad on the left.</p> : null}
        </div>
        <div className="aiMissionResult">
          {selected ? <>
            <header>
              <div><span className="eyebrow">{selected.status} · {selected.phase.replaceAll("_", " ")}</span><h2>{selected.goal}</h2></div>
              <div className="aiMissionControls">
                {!["completed", "cancelled"].includes(selected.status) ? <button disabled={busy} onClick={runMission} type="button">{busy ? "Team working…" : selected.status === "paused" ? "Resume" : "Run mission"}</button> : null}
                {!["completed", "cancelled"].includes(selected.status) ? <button className="quiet" disabled={busy} onClick={cancelMission} type="button">Cancel</button> : null}
              </div>
            </header>
            <div className="aiMissionRoster">{selected.participants.map((participant) => <span className={participant.mission_role} key={participant.employee_id}>{agentById.get(participant.employee_id)?.name || participant.employee_id}<small>{participant.mission_role}</small></span>)}</div>
            {error ? <p className="inlineError" role="alert">{error}</p> : null}
            <div className="aiMissionTurns" aria-live="polite">
              {(visibleTurns.length ? visibleTurns : storedMessages.map((message) => ({
                type: "agent_turn", employee_id: message.speaker_id, content: message.content, phase: "stored",
              }))).map((event, index) => <article key={`${event.employee_id}-${event.phase}-${index}`}><span>{event.phase?.replaceAll("_", " ")} · {agentById.get(String(event.employee_id))?.name || event.employee_id}</span><p>{String(event.content || "")}</p></article>)}
              {!visibleTurns.length && !storedMessages.length ? <div className="aiStageEmpty"><span>6 MAX</span><h2>The squad is ready</h2><p>Run the mission to see each employee’s position, challenge, accountable plan, and Clara’s final synthesis.</p></div> : null}
            </div>
            <footer><span>{selected.usage.prompt_tokens + selected.usage.completion_tokens} tokens</span><span>€{selected.usage.cost_usd.toFixed(4)} model cost</span><span>{selected.participants.length} participants</span></footer>
          </> : <div className="aiStageEmpty"><span>MISSION</span><h2>Choose a mission</h2><p>The full discussion remains durable and separated by employee turn.</p></div>}
        </div>
      </div>
    </section>
  );
}
