"use client";

import { useEffect, useState } from "react";
import {
  createAiCharacterDraft,
  listAiCharacterVersions,
  publishAiCharacter,
  setAiEmployeeStatus,
} from "@/lib/onebrain-client";
import type { AiCharacterVersion, AiEmployee, AiModels } from "@/lib/onebrain-types";

type CharacterForm = {
  display_name: string;
  fictional_age: string;
  country: string;
  pronouns: string;
  biography: string;
  personality: string;
  tone: string;
  strengths: string;
  watch_outs: string;
  working_style: string;
  collaboration_behavior: string;
  role_focus: string;
  character_prompt: string;
};

function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function list(value: unknown): string { return Array.isArray(value) ? value.join(", ") : ""; }

function CharacterEditor({ accountId, spaceId, employee, onChanged }: {
  accountId: string;
  spaceId: string;
  employee: AiEmployee;
  onChanged: () => Promise<void>;
}) {
  const [versions, setVersions] = useState<AiCharacterVersion[]>([]);
  const [draft, setDraft] = useState<AiCharacterVersion | null>(null);
  const [form, setForm] = useState<CharacterForm>({
    display_name: employee.name,
    fictional_age: String(employee.fictional_age),
    country: employee.country,
    pronouns: employee.pronouns,
    biography: employee.biography,
    personality: employee.personality.join(", "),
    tone: employee.tone,
    strengths: employee.strengths.join(", "),
    watch_outs: employee.watch_outs.join(", "),
    working_style: employee.working_style,
    collaboration_behavior: "",
    role_focus: "",
    character_prompt: "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void listAiCharacterVersions(employee.employee_id, accountId, spaceId)
      .then((rows) => {
        if (!active) return;
        setVersions(rows);
        const current = rows.find((row) => row.id === employee.character_version_id);
        if (!current) return;
        const payload = current.payload;
        setForm({
          display_name: text(payload.display_name) || employee.name,
          fictional_age: String(payload.fictional_age || employee.fictional_age),
          country: text(payload.country) || employee.country,
          pronouns: text(payload.pronouns) || employee.pronouns,
          biography: text(payload.biography),
          personality: list(payload.personality),
          tone: text(payload.tone),
          strengths: list(payload.strengths),
          watch_outs: list(payload.watch_outs),
          working_style: text(payload.working_style),
          collaboration_behavior: text(payload.collaboration_behavior),
          role_focus: text(payload.role_focus),
          character_prompt: text(payload.character_prompt),
        });
      })
      .catch((reason: Error) => { if (active) setError(reason.message); });
    return () => { active = false; };
  }, [accountId, employee, spaceId]);

  function field<K extends keyof CharacterForm>(key: K, value: CharacterForm[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function saveDraft() {
    setBusy(true);
    setError("");
    try {
      const split = (value: string) => value.split(",").map((item) => item.trim()).filter(Boolean);
      const saved = await createAiCharacterDraft(employee.employee_id, {
        account_id: accountId,
        space_id: spaceId,
        display_name: form.display_name,
        fictional_age: Number(form.fictional_age),
        country: form.country,
        pronouns: form.pronouns,
        biography: form.biography,
        personality: split(form.personality),
        tone: form.tone,
        strengths: split(form.strengths),
        watch_outs: split(form.watch_outs),
        working_style: form.working_style,
        collaboration_behavior: form.collaboration_behavior,
        role_focus: form.role_focus,
        character_prompt: form.character_prompt,
      });
      setDraft(saved);
      setVersions((current) => [saved, ...current.filter((row) => row.id !== saved.id)]);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Character draft could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function publishDraft() {
    if (!draft) return;
    setBusy(true);
    setError("");
    try {
      await publishAiCharacter(
        employee.employee_id, draft.id, accountId, spaceId, employee.character_version_id,
      );
      setDraft(null);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Character version could not be published.");
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus() {
    setBusy(true);
    setError("");
    try {
      await setAiEmployeeStatus(
        employee.employee_id, accountId, spaceId, employee.status === "active" ? "paused" : "active",
      );
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Employee status could not be changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="aiCharacterEditor">
      <header><div><span className="eyebrow">Character contract</span><h2>{employee.name}</h2><p>{employee.role} · immutable role and access policy</p></div><button className={employee.status === "active" ? "quiet" : ""} disabled={busy} onClick={toggleStatus} type="button">{employee.status === "active" ? "Pause employee" : "Resume employee"}</button></header>
      <div className="aiCharacterForm">
        <label>Name<input onChange={(event) => field("display_name", event.target.value)} value={form.display_name} /></label>
        <label>Fictional age<input min="18" max="80" onChange={(event) => field("fictional_age", event.target.value)} type="number" value={form.fictional_age} /></label>
        <label>Country<input onChange={(event) => field("country", event.target.value)} value={form.country} /></label>
        <label>Pronouns<input onChange={(event) => field("pronouns", event.target.value)} value={form.pronouns} /></label>
        <label className="wide">Biography<textarea onChange={(event) => field("biography", event.target.value)} rows={3} value={form.biography} /></label>
        <label className="wide">Personality · comma separated<input onChange={(event) => field("personality", event.target.value)} value={form.personality} /></label>
        <label className="wide">Tone<textarea onChange={(event) => field("tone", event.target.value)} rows={2} value={form.tone} /></label>
        <label className="wide">Strengths · comma separated<input onChange={(event) => field("strengths", event.target.value)} value={form.strengths} /></label>
        <label className="wide">Watch-outs · comma separated<input onChange={(event) => field("watch_outs", event.target.value)} value={form.watch_outs} /></label>
        <label className="wide">Working style<textarea onChange={(event) => field("working_style", event.target.value)} rows={3} value={form.working_style} /></label>
        <label className="wide">Collaboration behavior<textarea onChange={(event) => field("collaboration_behavior", event.target.value)} rows={3} value={form.collaboration_behavior} /></label>
        <label className="wide">Role focus<textarea onChange={(event) => field("role_focus", event.target.value)} rows={3} value={form.role_focus} /></label>
        <label className="wide">Character prompt<textarea onChange={(event) => field("character_prompt", event.target.value)} rows={8} value={form.character_prompt} /></label>
      </div>
      {error ? <p className="inlineError" role="alert">{error}</p> : null}
      {draft ? <div className="aiDraftReady"><div><strong>Draft version {draft.version} is ready</strong><span>{draft.preview}</span></div><button disabled={busy} onClick={publishDraft} type="button">Publish character</button></div> : null}
      <footer><button disabled={busy} onClick={saveDraft} type="button">{busy ? "Saving…" : "Save as draft"}</button><span>{versions.length} immutable versions · published changes affect new conversations and missions</span></footer>
    </div>
  );
}

export function AiEmployeeAdmin({ accountId, spaceId, agents, models, onTeamChanged }: {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  models: AiModels;
  onTeamChanged: () => Promise<void>;
}) {
  const [selectedId, setSelectedId] = useState(agents[0]?.employee_id ?? "");
  const selected = agents.find((agent) => agent.employee_id === selectedId) ?? agents[0];
  return (
    <section className="aiAdminLayout">
      <aside className="aiAdminAgents"><header><span className="eyebrow">Project admin</span><h2>Characters</h2></header>{agents.map((agent) => <button className={selected?.employee_id === agent.employee_id ? "active" : ""} key={agent.employee_id} onClick={() => setSelectedId(agent.employee_id)} type="button"><span>{agent.name}</span><small>v{agent.character_version} · {agent.status}</small></button>)}</aside>
      <div className="aiAdminStage">{selected ? <CharacterEditor accountId={accountId} employee={selected} key={`${selected.employee_id}-${selected.character_version}`} onChanged={onTeamChanged} spaceId={spaceId} /> : null}<div className="aiModelPosture"><header><span className="eyebrow">Model routing</span><h2>Provider-neutral, Gemini live</h2></header><div>{models.policies.map((policy) => <article key={policy.employee_id}><strong>{agents.find((agent) => agent.employee_id === policy.employee_id)?.name || policy.employee_id}</strong><span>{policy.provider} · {policy.model}</span><small>{policy.data_ceiling} ceiling · €{policy.cost_limit_usd.toFixed(2)} limit</small></article>)}</div></div>
      </div>
    </section>
  );
}
