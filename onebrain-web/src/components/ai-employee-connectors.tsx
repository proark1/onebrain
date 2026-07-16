"use client";

import { useState } from "react";
import {
  configureGoogleCalendar,
  listGoogleCalendars,
  revokeGoogleCalendar,
  startGoogleCalendarOAuth,
} from "@/lib/onebrain-client";
import type { AiConnectorBinding, AiConnectorHealth, AiEmployee } from "@/lib/onebrain-types";

const CAPABILITIES = [
  ["calendar_read", "Read calendar list"],
  ["calendar_create_event", "Create approved events"],
  ["calendar_update_event", "Update approved events"],
  ["calendar_cancel_event", "Cancel approved events"],
  ["calendar_create_private_focus", "Automate private self-only focus blocks"],
] as const;

type Props = {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  bindings: AiConnectorBinding[];
  health: AiConnectorHealth[];
  canManage: boolean;
  onConnectorsChanged: () => Promise<void>;
};

function BindingEditor({ accountId, spaceId, agents, binding, onChanged }: {
  accountId: string;
  spaceId: string;
  agents: AiEmployee[];
  binding: AiConnectorBinding;
  onChanged: () => Promise<void>;
}) {
  const [employeeIds, setEmployeeIds] = useState(binding.employee_ids);
  const [capabilities, setCapabilities] = useState(binding.capabilities);
  const [resourceIds, setResourceIds] = useState(binding.resource_ids.join("\n"));
  const [calendars, setCalendars] = useState<{ id: string; summary: string; primary: boolean; access_role: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  function toggle(list: string[], value: string, setter: (next: string[]) => void) {
    setter(list.includes(value) ? list.filter((item) => item !== value) : [...list, value]);
  }

  async function discoverCalendars() {
    setBusy(true);
    setError("");
    try {
      setCalendars(await listGoogleCalendars(binding.id, accountId, spaceId));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Calendars could not be loaded.");
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    setBusy(true);
    setError("");
    try {
      await configureGoogleCalendar(binding.id, {
        account_id: accountId,
        space_id: spaceId,
        employee_ids: employeeIds,
        capabilities,
        resource_ids: resourceIds.split(/\r?\n|,/).map((value) => value.trim()).filter(Boolean),
      });
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connector grants could not be saved.");
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setBusy(true);
    setError("");
    try {
      await revokeGoogleCalendar(binding.id, accountId, spaceId);
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Connector could not be revoked.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="aiConnectorEditor">
      <div className="aiConnectorGrantColumn"><h3>Assigned employees</h3>{agents.map((agent) => <label key={agent.employee_id}><input checked={employeeIds.includes(agent.employee_id)} onChange={() => toggle(employeeIds, agent.employee_id, setEmployeeIds)} type="checkbox" /><span>{agent.name}<small>{agent.role}</small></span></label>)}</div>
      <div className="aiConnectorGrantColumn"><h3>Capabilities</h3>{CAPABILITIES.map(([id, label]) => <label key={id}><input checked={capabilities.includes(id)} onChange={() => toggle(capabilities, id, setCapabilities)} type="checkbox" /><span>{label}<small>{id === "calendar_create_private_focus" ? "Only private, no attendees, low-risk data" : "Human approval remains required for writes"}</small></span></label>)}</div>
      <div className="aiCalendarAllowlist"><h3>Allowed calendars</h3><textarea onChange={(event) => setResourceIds(event.target.value)} rows={5} value={resourceIds} /><button className="quiet" disabled={busy || !capabilities.includes("calendar_read")} onClick={discoverCalendars} type="button">Discover calendars</button>{calendars.map((calendar) => <button key={calendar.id} onClick={() => setResourceIds((current) => current.split(/\r?\n/).includes(calendar.id) ? current : `${current.trim()}\n${calendar.id}`.trim())} type="button"><span>{calendar.summary}</span><small>{calendar.primary ? "Primary" : calendar.access_role}</small></button>)}</div>
      {error ? <p className="inlineError">{error}</p> : null}
      <footer><button disabled={busy || !employeeIds.length || !capabilities.length || !resourceIds.trim()} onClick={save} type="button">Save grants</button><button className="danger" disabled={busy} onClick={revoke} type="button">Revoke connection</button></footer>
    </div>
  );
}

export function AiEmployeeConnectors({ accountId, spaceId, agents, bindings, health, canManage, onConnectorsChanged }: Props) {
  const [selectedId, setSelectedId] = useState(bindings.find((row) => row.status === "active")?.id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const googleHealth = health.find((row) => row.provider === "google_calendar");
  const selected = bindings.find((row) => row.id === selectedId) ?? bindings[0] ?? null;

  async function connect() {
    setBusy(true);
    setError("");
    try {
      const started = await startGoogleCalendarOAuth({
        account_id: accountId,
        space_id: spaceId,
        employee_ids: ["chief_of_staff"],
        capabilities: ["calendar_read", "calendar_create_event"],
        resource_ids: ["primary"],
      });
      sessionStorage.setItem("onebrain.google-calendar.oauth", JSON.stringify({ account_id: accountId, space_id: spaceId }));
      window.location.assign(started.authorization_url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Google Calendar connection could not start.");
      setBusy(false);
    }
  }

  return (
    <section className="aiConnectors">
      <header className="aiSectionLead"><div><span className="eyebrow">Connected work</span><h2>Tools stay narrower than the job</h2></div><p>Credentials never enter prompts or records. Every employee receives named capabilities and a resource allowlist.</p></header>
      <div className="aiConnectorHero">
        <div className="aiGoogleMark" aria-hidden="true"><span /><span /><span /><span /></div>
        <div><span className="eyebrow">Google Workspace</span><h2>Calendar</h2><p>Read calendars, prepare events, and execute payload-bound writes with deterministic retry protection.</p></div>
        <div className="aiConnectorState"><span className={googleHealth?.available ? "ready" : "off"}>{googleHealth?.available ? "OAuth ready" : "Needs configuration"}</span><strong>{bindings.filter((row) => row.status === "active").length} active bindings</strong><small>{googleHealth?.reason || "Encrypted OAuth token storage enabled"}</small></div>
        {canManage ? <button disabled={busy || !googleHealth?.available} onClick={connect} type="button">Connect Google Calendar</button> : null}
      </div>
      {error ? <p className="inlineError">{error}</p> : null}
      {bindings.length ? <div className="aiConnectorDesk"><nav>{bindings.map((binding) => <button className={selected?.id === binding.id ? "active" : ""} key={binding.id} onClick={() => setSelectedId(binding.id)} type="button"><span>{binding.provider.replaceAll("_", " ")}</span><small>{binding.status} · {binding.resource_ids.length} calendars</small></button>)}</nav>{selected ? <BindingEditor accountId={accountId} agents={agents} binding={selected} key={`${selected.id}-${selected.updated_at}`} onChanged={onConnectorsChanged} spaceId={spaceId} /> : null}</div> : <div className="aiEmptyPanel"><span>CONNECT</span><h2>No external tools connected</h2><p>The team already works inside OneBrain. Connect Calendar when you are ready to approve real scheduling actions.</p></div>}
    </section>
  );
}
