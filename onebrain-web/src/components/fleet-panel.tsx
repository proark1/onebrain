"use client";

import { useCallback, useEffect, useId, useState, type FormEvent } from "react";
import { Notice, PageHeader, Panel, StatusBadge, Tabs } from "@/components/admin-ui";
import { StatusSummary } from "@/components/operational/status-summary";
import { Timestamp } from "@/components/operational/timestamp";
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
  FleetDeploymentOverview,
  FleetOverview,
  FleetRollout,
  FleetStorageCapacity,
  FleetStorageReport,
} from "@/lib/onebrain-types";
import { describeFleetOverview, fleetHealthLabel, fleetHealthTone } from "@/lib/fleet-presentation";

type FleetTab = "overview" | "rollouts" | "keys";
type StatusTone = "success" | "danger" | "running" | "warning" | "neutral";

const TABS: Array<{ id: FleetTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "rollouts", label: "Rollouts" },
  { id: "keys", label: "Enrollment keys" },
];

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
}

function capacityLabel(capacity?: FleetStorageCapacity): string {
  const total = capacity?.total_bytes ?? 0;
  const available = capacity?.available_bytes ?? 0;
  if (total <= 0 || available < 0 || available > total) return "No host signal";
  return `${formatBytes(available)} free · ${Math.round((available * 100) / total)}%`;
}

function StorageSummary({ storage }: { storage?: FleetStorageReport }) {
  return (
    <div className="fleetStorageSummary">
      <div><span>Root</span><strong>{capacityLabel(storage?.root)}</strong></div>
      <div><span>Data</span><strong>{capacityLabel(storage?.data)}</strong></div>
    </div>
  );
}

function DeploymentRow({ deployment }: { deployment: FleetDeploymentOverview }) {
  const [expanded, setExpanded] = useState(false);
  const detailId = useId();
  const displayedVersion = deployment.reported_version || deployment.current_version || "—";
  const versionMismatch = Boolean(
    deployment.reported_version
    && deployment.current_version
    && deployment.reported_version !== deployment.current_version,
  );

  return (
    <>
      <tr className="fleetDeploymentRow">
        <td data-label="Health">
          <div className="fleetHealthCell">
            <button
              aria-controls={detailId}
              aria-expanded={expanded}
              aria-label={`${expanded ? "Hide" : "Show"} details for ${deployment.customer_name || deployment.deployment_id}`}
              className="fleetRowToggle"
              onClick={() => setExpanded((current) => !current)}
              type="button"
            >
              <span aria-hidden="true">›</span>
            </button>
            <StatusBadge tone={fleetHealthTone(deployment.healthy)}>{fleetHealthLabel(deployment.healthy)}</StatusBadge>
          </div>
        </td>
        <td data-label="Deployment">
          <strong>{deployment.customer_name || deployment.deployment_id}</strong>
          <small>{deployment.deployment_id}{deployment.is_release_gate ? " · development gate" : ""} · {deployment.release_ring || "no ring"}</small>
          {deployment.console_url ? (
            <a
              aria-label={`Open the console for ${deployment.customer_name || deployment.deployment_id} in a new tab`}
              className="fleetConsoleLink"
              href={deployment.console_url}
              rel="noreferrer"
              target="_blank"
            >
              Open console<span aria-hidden="true">↗</span>
            </a>
          ) : (
            <small className="fleetNoConsole" title="Set ONEBRAIN_FLEET_BASE_DOMAIN, or provision this deployment, to give it a console link.">
              No console address on record
            </small>
          )}
        </td>
        <td data-label="Release">
          <strong>{displayedVersion}</strong>
          <small>{versionMismatch ? `Registry expects ${deployment.current_version}` : deployment.migration_revision || "No migration reported"}</small>
        </td>
        <td data-label="Activity">
          <Timestamp label="Reported" value={deployment.last_reported_at} />
          <div className="fleetSecondaryTimestamp"><Timestamp label="Received" value={deployment.last_received_at} /></div>
        </td>
        <td data-label="Usage">
          <strong>{deployment.counts?.users ?? "—"} user{deployment.counts?.users === 1 ? "" : "s"}</strong>
          <StorageSummary storage={deployment.storage} />
        </td>
        <td data-label="Alerts">
          {deployment.open_alerts.length
            ? <StatusBadge tone="danger">{deployment.open_alerts.length} open</StatusBadge>
            : <span className="fleetNoAlerts">None</span>}
        </td>
      </tr>
      <tr className="fleetDeploymentDetail" hidden={!expanded} id={detailId}>
        <td colSpan={6}>
          <div className="fleetDetailGrid">
            <div><span>Added</span><Timestamp value={deployment.created_at} /></div>
            <div><span>Version active since</span><Timestamp value={deployment.current_version_deployed_at} /></div>
            <div><span>Environment</span><strong>{deployment.environment || deployment.deployment_type || "Not reported"}</strong></div>
            <div><span>Migration</span><code>{deployment.migration_revision || "Not reported"}</code></div>
            <div><span>User management</span><strong>{deployment.user_management_v1 ? "Ready" : "Upgrade required"}</strong></div>
            <div><span>Open alerts</span><strong>{deployment.open_alerts.length ? deployment.open_alerts.join(", ") : "None"}</strong></div>
          </div>
        </td>
      </tr>
    </>
  );
}

function rolloutStatus(status: string, currentRing: string): { label: string; detail: string; tone: StatusTone } {
  switch (status) {
    case "pending":
      return { label: "Preparing", detail: "Waiting to start the first rollout wave.", tone: "warning" };
    case "running":
      return {
        label: "In progress",
        detail: currentRing ? `Deploying the ${currentRing} ring.` : "Preparing the first rollout wave.",
        tone: "running",
      };
    case "paused":
      return { label: "Paused", detail: "An operator must decide whether to resume or stop it.", tone: "warning" };
    case "succeeded":
      return { label: "Complete", detail: "All planned rollout waves finished.", tone: "success" };
    case "failed":
      return { label: "Needs attention", detail: "A rollout wave failed and needs review.", tone: "danger" };
    case "aborted":
      return { label: "Stopped", detail: "This rollout was stopped before it completed.", tone: "danger" };
    default:
      return { label: "Needs review", detail: "The rollout reported an unfamiliar state.", tone: "neutral" };
  }
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
  // `rollouts` and `keys` start as [], so before the first fetch resolves their
  // tables render exactly like "there are none" -- on the surface where "no
  // rollout is running" and "we have not looked yet" mean opposite things. The
  // overview tab already distinguishes them only because `overview` starts null.
  const [loaded, setLoaded] = useState(false);

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
      setLoaded(true);
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

  /**
   * `confirmMessage` gates the actions that change a live customer's state.
   * It must name the consequence, not just ask "are you sure?".
   */
  async function run<T>(fn: () => Promise<T>, ok: string, confirmMessage?: string): Promise<void> {
    if (confirmMessage && !window.confirm(confirmMessage)) {
      return;
    }
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
      <PageHeader
        actions={<button className="secondaryButton" type="button" onClick={() => void refresh()}>Refresh data</button>}
        description="Monitor health, releases, and enrollment from one place."
        eyebrow="Fleet"
        title="Deployments"
      />

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
        <>
          <StatusSummary status={describeFleetOverview(overview)} updatedAt={overview.generated_at} updatedLabel="Snapshot generated">
            <div className="fleetSummaryCounts" aria-label="Fleet summary counts">
              <div><strong>{overview.total}</strong><span>Total</span></div>
              <div><strong>{overview.healthy}</strong><span>Healthy</span></div>
              <div><strong>{overview.with_open_alerts}</strong><span>Alerts</span></div>
            </div>
          </StatusSummary>
          <Panel
            count={overview.total}
            intro="Every deployment that reports to Mission Control, including this control plane and the development gate. Each row is what the box last told us about itself; open a row for its environment and migration, or use Open console to go to the box itself."
            title="All deployments"
          >
          <div className="tableScroll">
            <table className="adminTable fleetTable">
              <thead>
                <tr>
                  <th>Health</th><th>Deployment</th><th>Release</th><th>Activity</th><th>Usage</th><th>Alerts</th>
                </tr>
              </thead>
              <tbody>
                {overview.deployments.map((deployment) => <DeploymentRow deployment={deployment} key={deployment.deployment_id} />)}
              </tbody>
            </table>
          </div>
          </Panel>
        </>
      ) : null}

      {tab === "overview" && !overview && !error ? <div className="loadingState" role="status">Loading deployments…</div> : null}

      {tab === "rollouts" ? (
        <>
          <Panel
            eyebrow="Steer"
            intro={(
              <>
                Moves <strong>many deployments</strong> onto one release in ordered waves, ring by ring,
                stopping on its own if too many fail. To move a single named customer instead, use
                Control → Rollouts. Leave <strong>Dry run</strong> ticked to rehearse the plan: it reports
                what would happen and changes no version.
              </>
            )}
            title="Start a fleet rollout"
          >
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
          <Panel
            count={rollouts.length}
            eyebrow="Active + history"
            intro="Fleet rollouts that are running now or have already finished, with when each started and when the control plane last recorded a state change. Pause holds the next wave, Abort stops for good — deployments already moved stay on the new release."
            title="Fleet rollouts"
          >
            {!loaded && !error ? <div className="loadingState" role="status">Loading rollouts…</div> : null}
            {loaded && rollouts.length === 0 ? (
              <div className="emptyState">No rollout has been started. Use the form above to plan one.</div>
            ) : null}
            {loaded && rollouts.length > 0 ? (
            <div className="tableScroll">
              <table className="adminTable">
                <thead>
                  <tr><th>Status</th><th>Target version</th><th>Current wave</th><th>Safety policy</th><th>Scope</th><th>Started</th><th>Last updated</th><th>Actions</th></tr>
                </thead>
                <tbody>
                  {rollouts.map((r) => {
                    const status = rolloutStatus(r.status, r.current_ring);
                    const scope = r.deployment_ids.length
                      ? `${r.deployment_ids.length} selected deployment${r.deployment_ids.length === 1 ? "" : "s"}`
                      : "All eligible deployments";

                    return (
                      <tr key={r.id}>
                        <td>
                          <StatusBadge tone={status.tone}>{status.label}</StatusBadge>
                          <div className="muted">{status.detail}</div>
                        </td>
                        <td><code>{r.target_version}</code></td>
                        <td>
                          <strong>{r.current_ring ? `${r.current_ring} ring` : "Not started"}</strong>
                          <div className="muted">Planned: {r.ring_order.length ? r.ring_order.join(" → ") : "No eligible rings"}</div>
                        </td>
                        <td>
                          <strong>{r.ring_batch_size === 1 ? "One customer at a time" : `${r.ring_batch_size} customers at a time`}</strong>
                          <div className="muted">Stops after {r.failure_tolerance} failure{r.failure_tolerance === 1 ? "" : "s"}</div>
                        </td>
                        <td>{scope}</td>
                        <td>
                          <Timestamp label="Started" value={r.created_at} />
                          <div className="muted">By {r.started_by || "system"}</div>
                        </td>
                        <td><Timestamp label="State changed" value={r.updated_at} /></td>
                        <td className="rowActions">
                          {r.status === "running" ? <button onClick={() => run(() => pauseFleetRollout(r.id), "Paused.")}>Pause</button> : null}
                          {r.status === "paused" ? <button onClick={() => run(() => resumeFleetRollout(r.id), "Resumed.")}>Resume</button> : null}
                          {(r.status === "running" || r.status === "paused") ? (
                            <button
                              onClick={() => run(
                                () => abortFleetRollout(r.id),
                                "Aborted.",
                                `Abort this rollout?\n\nDeployments already updated stay on the new release; the rest keep the old one, leaving the fleet on mixed versions. Aborting cannot be resumed -- you would need to create a new rollout.`,
                              )}
                            >
                              Abort
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            ) : null}
          </Panel>
        </>
      ) : null}

      {tab === "keys" ? (
        <Panel
          count={keys.length}
          eyebrow="Enrollment"
          intro={(
            <>
              A deployment cannot report to Mission Control until it holds a key pinned to its own
              deployment id — this is how a new box joins the fleet. Mint one, then use Enroll to get the
              environment variables to set on it. <strong>Revoking a key silences that box:</strong> it stops
              sending heartbeats and cannot fetch its desired state or re-enrol without host access.
            </>
          )}
          title="Fleet keys"
        >
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
          {!loaded && !error ? <div className="loadingState" role="status">Loading enrollment keys…</div> : null}
          {loaded && keys.length === 0 ? (
            <div className="emptyState">No enrollment key exists. Mint one to let a deployment report in.</div>
          ) : null}
          {loaded && keys.length > 0 ? (
          <div className="tableScroll">
            <table className="adminTable">
              <thead>
                <tr><th>Key id</th><th>Deployment</th><th>Label</th><th>Status</th><th>Created</th><th>Last used</th><th></th></tr>
              </thead>
              <tbody>
                {keys.map((k) => (
                  <tr key={k.id}>
                    <td><code>{k.id}</code></td>
                    <td>{k.deployment_id}</td>
                    <td>{k.label || "—"}</td>
                    <td><StatusBadge tone={k.status === "active" ? "success" : "neutral"}>{k.status}</StatusBadge></td>
                    <td><Timestamp label="Created" value={k.created_at} /></td>
                    <td><Timestamp label="Last used" value={k.last_used_at} /></td>
                    <td>{k.status === "active" ? (
                      <button
                        onClick={() => run(
                          () => revokeFleetKey(k.id),
                          "Key revoked.",
                          `Revoke enrollment key ${k.id}?\n\nThe deployment using it stops reporting heartbeats and cannot fetch desired state or re-enrol. Recovering it needs host access. This cannot be undone.`,
                        )}
                      >
                        Revoke
                      </button>
                    ) : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}
