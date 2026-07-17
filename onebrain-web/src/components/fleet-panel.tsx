"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge, Tabs } from "@/components/admin-ui";
import {
  abortFleetRollout,
  createFleetRollout,
  enrollDeployment,
  getFleetOverview,
  listFleetKeys,
  listFleetRollouts,
  mintFleetKey,
  pauseFleetRollout,
  resumeFleetRollout,
  revokeFleetKey,
} from "@/lib/onebrain-client";
import type {
  CreateFleetRolloutInput,
  FleetKeyInfo,
  FleetOverview,
  FleetRollout,
} from "@/lib/onebrain-types";

type FleetTab = "overview" | "rollouts" | "keys";

const TABS: Array<{ id: FleetTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "rollouts", label: "Rollouts" },
  { id: "keys", label: "Keys" },
];

function healthTone(healthy: boolean | null): "success" | "danger" | "neutral" {
  if (healthy === null) return "neutral";
  return healthy ? "success" : "danger";
}

function rolloutTone(status: string): "success" | "danger" | "running" | "warning" | "neutral" {
  return { running: "running", succeeded: "success", failed: "danger", aborted: "danger", paused: "warning" }[status] as
    | "success" | "danger" | "running" | "warning" | undefined ?? "neutral";
}

export function FleetPanel() {
  const [tab, setTab] = useState<FleetTab>("overview");
  const [overview, setOverview] = useState<FleetOverview | null>(null);
  const [rollouts, setRollouts] = useState<FleetRollout[]>([]);
  const [keys, setKeys] = useState<FleetKeyInfo[]>([]);
  const [error, setError] = useState<string>("");
  const [notice, setNotice] = useState<string>("");
  const [reveal, setReveal] = useState<Record<string, string> | string>("");
  const [unavailable, setUnavailable] = useState(false);

  const [form, setForm] = useState<CreateFleetRolloutInput>({
    target_version: "",
    callback_url: typeof window === "undefined" ? "" : `${window.location.origin}/api/rollouts/{rollout_id}/callback`,
    failure_tolerance: 0,
    dry_run: true,
    deployment_ids: [],
    ring_batch_size: 1,
    include_manual_pinned: false,
  });
  const [mintDeployment, setMintDeployment] = useState("");

  const refresh = useCallback(async () => {
    setError("");
    try {
      const [ov, ro, ks] = await Promise.all([getFleetOverview(), listFleetRollouts(), listFleetKeys()]);
      setOverview(ov);
      setRollouts(ro);
      setKeys(ks);
      setUnavailable(false);
    } catch (err) {
      // Fleet endpoints exist only on a Mission Control (operator_mode) deployment.
      setUnavailable(true);
      setError(err instanceof Error ? err.message : "Fleet control plane is unavailable on this deployment.");
    }
  }, []);

  useEffect(() => {
    // Initial fetch on mount; state updates land after the request resolves.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh();
  }, [refresh]);

  async function run<T>(fn: () => Promise<T>, ok: string): Promise<void> {
    setError("");
    setNotice("");
    try {
      await fn();
      setNotice(ok);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    }
  }

  async function onCreateRollout(event: FormEvent) {
    event.preventDefault();
    setError("");
    setNotice("");
    try {
      const result = await createFleetRollout(form);
      if (!result.fleet_rollout) {
        const blocked = Object.entries(result.plan.blocked)
          .map(([id, reason]) => `${id}: ${reason}`)
          .join("; ");
        setNotice(
          `Nothing deployable. Skipped ${result.plan.skipped.length}. ${blocked ? `Blocked — ${blocked}` : ""}`,
        );
      } else {
        setNotice(`Fleet rollout ${result.fleet_rollout.id} started at ring ${result.fleet_rollout.current_ring}.`);
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    }
  }

  return (
    <div className="adminSurface">
      <PageHeader eyebrow="Mission Control" title="Fleet" meta={overview ? `${overview.total} deployments` : undefined} />

      {unavailable ? (
        <Notice tone="warning">
          The fleet control plane is only available on a Mission Control deployment (ONEBRAIN_OPERATOR_MODE=true).
          {error ? ` (${error})` : ""}
        </Notice>
      ) : null}
      {error && !unavailable ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}
      {reveal ? (
        <Notice tone="warning">
          Shown once — copy now:{" "}
          <code>{typeof reveal === "string" ? reveal : Object.entries(reveal).map(([k, v]) => `${k}=${v}`).join("  ")}</code>
        </Notice>
      ) : null}

      <Tabs<FleetTab> active={tab} items={TABS} label="Fleet sections" onChange={setTab} />

      {tab === "overview" && overview ? (
        <Panel eyebrow="Health" title="Deployments" count={overview.total}>
          <MetricStrip
            metrics={[
              { label: "Total", value: String(overview.total) },
              { label: "Healthy", value: String(overview.healthy) },
              { label: "With alerts", value: String(overview.with_open_alerts) },
            ]}
          />
          <div className="tableScroll">
            <table className="adminTable">
              <thead>
                <tr>
                  <th>Health</th><th>Customer</th><th>Ring</th><th>Version</th>
                  <th>Installed</th><th>Last seen</th><th>Users</th><th>Alerts</th>
                </tr>
              </thead>
              <tbody>
                {overview.deployments.map((d) => (
                  <tr key={d.deployment_id}>
                    <td><StatusBadge tone={healthTone(d.healthy)}>{d.healthy === null ? "no data" : d.healthy ? "healthy" : "unhealthy"}</StatusBadge></td>
                    <td>{d.customer_name || d.deployment_id}{d.is_release_gate ? " · dev gate" : ""}</td>
                    <td>{d.release_ring}</td>
                    <td>{d.reported_version || d.current_version || "—"}{d.reported_version && d.current_version && d.reported_version !== d.current_version ? ` (registry ${d.current_version})` : ""}</td>
                    <td>{d.current_version_deployed_at ? new Date(d.current_version_deployed_at).toLocaleDateString() : "unknown"}</td>
                    <td>{d.last_received_at ? new Date(d.last_received_at).toLocaleString() : "never"}</td>
                    <td>{d.counts?.users ?? "—"}</td>
                    <td>{d.open_alerts.length ? <StatusBadge tone="danger">{d.open_alerts.join(", ")}</StatusBadge> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}

      {tab === "rollouts" ? (
        <>
          <Panel eyebrow="Steer" title="Start a fleet rollout">
            <form className="adminForm" onSubmit={onCreateRollout}>
              <label>Target release version
                <input value={form.target_version} onChange={(e) => setForm({ ...form, target_version: e.target.value })} required />
              </label>
              <label>Callback URL (must contain {"{rollout_id}"})
                <input value={form.callback_url} onChange={(e) => setForm({ ...form, callback_url: e.target.value })} required />
              </label>
              <label>Customer deployment ids (comma separated)
                <input
                  value={form.deployment_ids.join(", ")}
                  onChange={(e) => setForm({
                    ...form,
                    deployment_ids: e.target.value.split(",").map((value) => value.trim()).filter(Boolean),
                  })}
                  placeholder="dep_customer_a, dep_customer_b"
                  required
                />
              </label>
              <p className="muted">Safety policy: one customer at a time, zero tolerated failures.</p>
              <label className="checkboxRow">
                <input type="checkbox" checked={form.dry_run} onChange={(e) => setForm({ ...form, dry_run: e.target.checked })} />
                Dry run (verify plumbing; no version change)
              </label>
              <button type="submit">Plan &amp; start</button>
            </form>
          </Panel>
          <Panel eyebrow="Active + history" title="Fleet rollouts" count={rollouts.length}>
            <div className="tableScroll">
              <table className="adminTable">
                <thead>
                  <tr><th>Status</th><th>Version</th><th>Ring</th><th>Policy</th><th>Started by</th><th>Actions</th></tr>
                </thead>
                <tbody>
                  {rollouts.map((r) => (
                    <tr key={r.id}>
                      <td><StatusBadge tone={rolloutTone(r.status)}>{r.status}</StatusBadge></td>
                      <td>{r.target_version}</td>
                      <td>{r.current_ring || "—"} <span className="muted">({r.ring_order.join(" → ")})</span></td>
                      <td>{r.ring_batch_size} at a time / {r.failure_tolerance} failures</td>
                      <td>{r.started_by}</td>
                      <td className="rowActions">
                        {r.status === "running" ? <button onClick={() => run(() => pauseFleetRollout(r.id), "Paused.")}>Pause</button> : null}
                        {r.status === "paused" ? <button onClick={() => run(() => resumeFleetRollout(r.id), "Resumed.")}>Resume</button> : null}
                        {(r.status === "running" || r.status === "paused") ? <button onClick={() => run(() => abortFleetRollout(r.id), "Aborted.")}>Abort</button> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </>
      ) : null}

      {tab === "keys" ? (
        <Panel eyebrow="Enrollment" title="Fleet keys" count={keys.length}>
          <form className="adminForm inline" onSubmit={(e) => { e.preventDefault(); }}>
            <label>Deployment id
              <input value={mintDeployment} onChange={(e) => setMintDeployment(e.target.value)} placeholder="dep_..." />
            </label>
            <button type="button" disabled={!mintDeployment}
              onClick={() => run(async () => { const m = await mintFleetKey(mintDeployment); setReveal(m.token); }, "Key minted.")}>
              Mint key
            </button>
            <button type="button" disabled={!mintDeployment}
              onClick={() => run(async () => { const e = await enrollDeployment(mintDeployment); setReveal(e.env); }, "Enrollment env generated.")}>
              Enroll (env vars)
            </button>
          </form>
          <div className="tableScroll">
            <table className="adminTable">
              <thead>
                <tr><th>Key id</th><th>Deployment</th><th>Label</th><th>Status</th><th>Last used</th><th></th></tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id}>
                    <td><code>{k.id}</code></td>
                    <td>{k.deployment_id}</td>
                    <td>{k.label || "—"}</td>
                    <td><StatusBadge tone={k.status === "active" ? "success" : "neutral"}>{k.status}</StatusBadge></td>
                    <td>{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
                    <td>{k.status === "active" ? <button onClick={() => run(() => revokeFleetKey(k.id), "Key revoked.")}>Revoke</button> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      ) : null}
    </div>
  );
}
