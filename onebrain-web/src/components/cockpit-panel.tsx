"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { AiEmployeeDirectory } from "@/components/ai-employee-directory";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import { StatusSummary } from "@/components/operational/status-summary";
import { Timestamp } from "@/components/operational/timestamp";
import {
  getAiEmployeeTeam,
  getOperatorObservability,
  listAiEmployeeWorkspaces,
  listOperatorCustomers,
  listPlatformAccounts,
  listPlatformApps,
  listPlatformSpaces,
} from "@/lib/onebrain-client";
import { type OperationalStatus } from "@/lib/operational";
import type {
  AiEmployeeTeam,
  OperatorCustomer,
  OperatorObservability,
  PlatformAppInstallation,
  PlatformSpace,
} from "@/lib/onebrain-types";

type LoadState = "idle" | "loading";

/** One connected app row, built from either the operator or the platform surface. */
type ConnectedApp = {
  accountName: string;
  displayName: string;
  id: string;
  purposes: number;
  spaces: number;
  status: string;
};

type WorkspaceSnapshot = {
  apps: ConnectedApp[];
  spaces: PlatformSpace[];
};

async function fetchCockpitData(): Promise<[OperatorObservability, OperatorCustomer[]]> {
  return Promise.all([
    getOperatorObservability(),
    listOperatorCustomers(),
  ]);
}

/**
 * Customer-scoped equivalent of fetchCockpitData. A customer deployment (and the
 * development gate) denies /api/operator at both the router and the edge, so the
 * same counts are assembled from the always-mounted platform surface instead.
 */
async function fetchWorkspaceSnapshot(): Promise<WorkspaceSnapshot> {
  const accounts = await listPlatformAccounts();
  const perAccount = await Promise.all(accounts.map(async (account) => {
    const [apps, spaces] = await Promise.all([
      listPlatformApps(account.id),
      listPlatformSpaces(account.id),
    ]);
    return { accountName: account.name, apps, spaces };
  }));
  return {
    apps: perAccount.flatMap((entry) => entry.apps.map(
      (app: PlatformAppInstallation) => connectedApp(entry.accountName, app),
    )),
    spaces: perAccount.flatMap((entry) => entry.spaces),
  };
}

function connectedApp(accountName: string, app: {
  allowed_purposes: string[];
  app_id: string;
  display_name: string;
  enabled_space_ids: string[];
  id: string;
  status: string;
}): ConnectedApp {
  return {
    accountName,
    displayName: app.display_name || app.app_id,
    id: app.id,
    purposes: app.allowed_purposes.length,
    spaces: app.enabled_space_ids.length,
    status: app.status,
  };
}

async function fetchCanonicalEmployeeTeam(): Promise<AiEmployeeTeam | null> {
  try {
    const workspaces = await listAiEmployeeWorkspaces();
    const workspace = workspaces.find((item) => item.installation_status === "active") ?? workspaces[0];
    if (!workspace) return null;
    return await getAiEmployeeTeam(workspace.account_id, workspace.space_id);
  } catch {
    return null;
  }
}

function labelFor(value: string): string {
  return (value || "none").replace(/_/g, " ");
}

function statusTone(value: string): "danger" | "neutral" | "running" | "success" | "warning" {
  if (["active", "clear", "healthy", "success", "dpia_signed"].includes(value)) {
    return "success";
  }
  if (["running", "backlog", "updating"].includes(value)) {
    return "running";
  }
  if (["attention", "warning", "synthetic", "unknown"].includes(value)) {
    return "warning";
  }
  if (["critical", "failed", "false"].includes(value)) {
    return "danger";
  }
  return "neutral";
}

function alertTone(severity: string): "danger" | "warning" {
  return severity === "critical" ? "danger" : "warning";
}

/**
 * Status is the one page both surfaces share, so it is the one page that cannot
 * assume the operator control plane exists. Select the variant with the same
 * server-issued capability that mounts /api/operator (and that /operator and
 * /fleet guard on): a customer box and the development gate deny that namespace
 * at both the router and the edge, so the fleet variant must never load there.
 */
export function CockpitPanel({ isOperatorSurface }: { isOperatorSurface: boolean }) {
  return isOperatorSurface ? <MissionControlCockpit /> : <WorkspaceCockpit />;
}

function WorkspaceCockpit() {
  const [snapshot, setSnapshot] = useState<WorkspaceSnapshot | null>(null);
  const [employeeTeam, setEmployeeTeam] = useState<AiEmployeeTeam | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState("");

  const loadWorkspace = useCallback(async () => {
    setLoadState("loading");
    setError("");
    try {
      const [nextSnapshot, nextEmployeeTeam] = await Promise.all([
        fetchWorkspaceSnapshot(),
        fetchCanonicalEmployeeTeam(),
      ]);
      setSnapshot(nextSnapshot);
      setEmployeeTeam(nextEmployeeTeam);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load workspace status.");
    } finally {
      setLoadState("idle");
    }
  }, []);

  useEffect(() => {
    let active = true;
    async function loadInitial() {
      try {
        const [nextSnapshot, nextEmployeeTeam] = await Promise.all([
          fetchWorkspaceSnapshot(),
          fetchCanonicalEmployeeTeam(),
        ]);
        if (!active) {
          return;
        }
        setSnapshot(nextSnapshot);
        setEmployeeTeam(nextEmployeeTeam);
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Could not load workspace status.");
        }
      } finally {
        if (active) {
          setLoadState("idle");
        }
      }
    }
    void loadInitial();
    return () => {
      active = false;
    };
  }, []);

  const apps = snapshot?.apps ?? [];
  const status: OperationalStatus = !snapshot
    ? {
      condition: "Not yet reported",
      explanation: "This workspace has not returned its configuration yet.",
      nextAction: "Refresh Status in a moment. If it stays empty, check the OneBrain service connection.",
      tone: "neutral",
    }
    : apps.length === 0
      ? {
        condition: "Pending",
        explanation: "Your workspace is ready, but no apps are connected to it yet.",
        nextAction: "Open Apps to connect the data sources this workspace should use.",
        tone: "running",
      }
      : {
        condition: "Healthy",
        explanation: "Your workspace is online and serving its connected apps.",
        nextAction: "No immediate action is needed. Keep an eye on Apps and Privacy.",
        tone: "success",
      };

  return (
    <div className="cockpitWorkspace">
      <PageHeader
        description="See how this workspace is set up and what it is connected to."
        eyebrow="Workspace"
        title="Status"
        actions={(
          <button className="secondaryButton" disabled={loadState === "loading"} type="button" onClick={() => void loadWorkspace()}>
            {loadState === "loading" ? "Refreshing" : "Refresh"}
          </button>
        )}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}

      <StatusSummary status={status}>
        <div className="brainActionRail">
          <Link className="primaryButton" href="/spaces">Open Apps</Link>
          <Link className="secondaryButton" href="/privacy">Privacy</Link>
        </div>
      </StatusSummary>

      <MetricStrip
        metrics={[
          { label: "spaces", value: snapshot ? snapshot.spaces.length : "-" },
          { label: "connected apps", value: snapshot ? apps.length : "-" },
          { label: "AI employees", value: employeeTeam?.agents.length ?? "-" },
        ]}
      />

      <EmployeeDirectoryPanel team={employeeTeam} />

      <section className="cockpitGrid" aria-label="OneBrain workspace status">
        <ConnectedAppsPanel apps={apps} loaded={Boolean(snapshot)} />
      </section>
    </div>
  );
}

function MissionControlCockpit() {
  const [observability, setObservability] = useState<OperatorObservability | null>(null);
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [employeeTeam, setEmployeeTeam] = useState<AiEmployeeTeam | null>(null);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState("");

  const loadCockpit = useCallback(async () => {
    setLoadState("loading");
    setError("");
    try {
      const [[nextObservability, nextCustomers], nextEmployeeTeam] = await Promise.all([
        fetchCockpitData(),
        fetchCanonicalEmployeeTeam(),
      ]);
      setObservability(nextObservability);
      setCustomers(nextCustomers);
      setEmployeeTeam(nextEmployeeTeam);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load cockpit.");
    } finally {
      setLoadState("idle");
    }
  }, []);

  useEffect(() => {
    let active = true;
    async function loadInitial() {
      try {
        const [[nextObservability, nextCustomers], nextEmployeeTeam] = await Promise.all([
          fetchCockpitData(),
          fetchCanonicalEmployeeTeam(),
        ]);
        if (!active) {
          return;
        }
        setObservability(nextObservability);
        setCustomers(nextCustomers);
        setEmployeeTeam(nextEmployeeTeam);
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Could not load cockpit.");
        }
      } finally {
        if (active) {
          setLoadState("idle");
        }
      }
    }
    void loadInitial();
    return () => {
      active = false;
    };
  }, []);

  const connectedApps = useMemo(() => (
    customers.flatMap((customer) => customer.apps.map(
      (app) => connectedApp(customer.account.name, app),
    ))
  ), [customers]);

  const alerts = observability?.alerts ?? [];
  const runtime = observability?.runtime;
  const retrieval = observability?.retrieval;
  const storage = observability?.storage;
  const serviceKeys = observability?.service_keys;
  const jobStatus = observability?.jobs?.by_status ?? {};
  const jobTypes = observability?.jobs?.by_type ?? {};
  const recentFailures = observability?.jobs?.recent_failures ?? [];
  const security = observability?.security;
  const worker = observability?.worker;
  const auth = observability?.auth;
  const api = observability?.api;
  const criticalAlerts = alerts.filter((alert) => alert.severity === "critical").length;
  const warningAlerts = alerts.filter((alert) => alert.severity !== "critical").length;
  const failedJobs = jobStatus.failed ?? 0;
  const pendingJobs = (jobStatus.queued ?? 0) + (jobStatus.retrying ?? 0);
  const missingCoreSignals = Boolean(observability && (!runtime || !storage || !security || !worker || !auth || !api));
  const databaseRequired = Boolean(security?.pgvector_required || runtime?.vector_store === "pgvector");
  const rlsValue = !databaseRequired ? "not required" : security?.rls_enforced ? "enforced" : "not enforced";
  const rlsTone = !databaseRequired ? "neutral" : security?.rls_enforced ? "success" : "danger";
  const databaseValue = !databaseRequired ? "not required" : security?.database_url_configured ? "configured" : "missing";
  const databaseTone = !databaseRequired ? "neutral" : security?.database_url_configured ? "success" : "warning";
  const brainStatus: OperationalStatus = !observability
    ? {
      condition: "Not yet reported",
      explanation: "Mission Control is waiting for its first complete status report.",
      nextAction: "Refresh Status in a moment. If it stays empty, check the development service connection.",
      tone: "neutral",
    }
    : criticalAlerts
      ? {
        condition: "Needs attention",
        explanation: "A critical signal needs intervention before this environment can be trusted.",
        nextAction: alerts[0]?.action || "Open the alert details and decide the recovery action.",
        tone: "danger",
      }
      : missingCoreSignals
        ? {
          condition: "Needs attention",
          explanation: "OneBrain is online, but not every runtime, worker, or security signal is reporting yet.",
          nextAction: "Review the missing signals before relying on this environment.",
          tone: "warning",
        }
        : warningAlerts || failedJobs || pendingJobs
          ? {
            condition: "Needs attention",
            explanation: "OneBrain is online, but one or more signals should be reviewed.",
            nextAction: alerts[0]?.action || "Review the open alerts and queued work in Control.",
            tone: "warning",
          }
          : connectedApps.length === 0
            ? {
              condition: "Pending",
              explanation: "The core service is ready, but no apps are connected to it yet.",
              nextAction: "Open Control to provision a customer and select the modules it needs.",
              tone: "running",
            }
            : {
              condition: "Healthy",
              explanation: "OneBrain is online, governed, and ready to serve its connected apps.",
              nextAction: "No immediate action is needed. Keep monitoring the latest reports.",
              tone: "success",
            };

  return (
    <div className="cockpitWorkspace">
      <PageHeader
        description="See what needs attention across OneBrain and act on the latest signals."
        eyebrow="Mission Control"
        title="Status"
        actions={(
          <button className="secondaryButton" disabled={loadState === "loading"} type="button" onClick={() => void loadCockpit()}>
            {loadState === "loading" ? "Refreshing" : "Refresh"}
          </button>
        )}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}

      <StatusSummary status={brainStatus} updatedAt={observability?.generated_at} updatedLabel="Refreshed at">
        <div className="brainActionRail">
          <Link className="primaryButton" href="/operator">Open Control</Link>
          <Link className="secondaryButton" href="/fleet">View Fleet</Link>
        </div>
      </StatusSummary>

      <MetricStrip
        metrics={[
          {
            label: "alerts",
            tone: criticalAlerts ? "danger" : warningAlerts ? "warning" : "success",
            value: observability ? alerts.length : "-",
          },
          { label: "customers", value: customers.length },
          { label: "connected apps", value: connectedApps.length },
          { label: "intake records", value: storage?.intake_records ?? "-" },
          { label: "active keys", value: serviceKeys?.active ?? "-" },
          { label: "pending jobs", tone: pendingJobs ? "warning" : "success", value: pendingJobs },
        ]}
      />

      <EmployeeDirectoryPanel team={employeeTeam} />

      <section className="cockpitGrid" aria-label="OneBrain cockpit">
        <Panel eyebrow="Signals" title="Alerts" count={alerts.length}>
          <div className="cockpitList">
            {observability && alerts.length === 0 ? (
              <div className="signalRow success">
                <div>
                  <strong>No active alerts</strong>
                  <span>Core runtime, queue, auth, and API signals are clear for this process.</span>
                </div>
                <StatusBadge tone="success">clear</StatusBadge>
              </div>
            ) : null}
            {alerts.map((alert) => (
              <article className={`signalRow ${alertTone(alert.severity)}`} key={alert.id}>
                <div>
                  <strong>{alert.title}</strong>
                  <span>{alert.detail}</span>
                  <small>{alert.action}</small>
                </div>
                <StatusBadge tone={alertTone(alert.severity)}>{alert.severity}</StatusBadge>
              </article>
            ))}
            {!observability && !error ? <p className="mutedLine">Loading observability signals.</p> : null}
          </div>
        </Panel>

        <ConnectedAppsPanel apps={connectedApps} loaded={Boolean(observability)} />

        <Panel eyebrow="Data flow" title="Jobs and storage">
          <div className="statusMatrix">
            <Signal label="Worker" value={observability ? labelFor(worker?.status || "unknown") : "-"} tone={statusTone(worker?.status || "")} />
            <Signal label="Failed jobs" value={String(failedJobs)} tone={failedJobs ? "danger" : "success"} />
            <Signal label="Running jobs" value={String(worker?.running_jobs ?? 0)} tone={worker?.running_jobs ? "running" : "success"} />
            <Signal label="Chunks" value={String(storage?.chunks ?? "-")} tone="neutral" />
            <Signal label="Job types" value={String(Object.keys(jobTypes).length)} tone="neutral" />
            <Signal label="Retrieval min score" value={String(retrieval?.min_score ?? "-")} tone="neutral" />
          </div>
        </Panel>

        <Panel eyebrow="Security" title="Privacy and access">
          <div className="statusMatrix">
            <Signal label="RLS" value={rlsValue} tone={rlsTone} />
            <Signal label="Database URL" value={databaseValue} tone={databaseTone} />
            <Signal label="Secure cookies" value={security?.cookie_secure ? "enabled" : "disabled"} tone={security?.cookie_secure ? "success" : "warning"} />
            <Signal label="Auth failures" value={String(auth?.total_failures ?? 0)} tone={auth?.total_failures ? "warning" : "success"} />
            <Signal label="Service-key failures" value={String(auth?.service_key_failures ?? 0)} tone={auth?.service_key_failures ? "warning" : "success"} />
            <Signal label="API errors" value={String(api?.errors_5xx ?? 0)} tone={api?.errors_5xx ? "warning" : "success"} />
          </div>
        </Panel>

        <Panel eyebrow="Failures" title="Recent job failures" count={recentFailures.length}>
          <div className="cockpitList">
            {observability && recentFailures.length === 0 ? (
              <p className="mutedLine">No failed jobs in the recent failure ledger.</p>
            ) : null}
            {recentFailures.slice(0, 5).map((job) => (
              <article className="signalRow danger" key={job.id}>
                <div>
                  <strong>{job.type}</strong>
                  <span>{job.error || "No error detail recorded."}</span>
                  <small>{job.attempts}/{job.max_attempts} attempts</small>
                  <Timestamp className="signalTimestamp" label="Last reported" value={job.completed_at || job.updated_at} />
                </div>
                <StatusBadge tone="danger">failed</StatusBadge>
              </article>
            ))}
          </div>
        </Panel>
      </section>
    </div>
  );
}

function EmployeeDirectoryPanel({ team }: { team: AiEmployeeTeam | null }) {
  return (
    <Panel
      actions={<Link className="secondaryButton" href="/ai-employees">Open AI Employees</Link>}
      count={team?.agents.length ?? 0}
      eyebrow="AI Employees"
      title="Employee directory"
    >
      <p className="mutedLine">Every employee is visible here. Expand one only when you need their working rules, safe actions, or technical details.</p>
      {team ? <AiEmployeeDirectory employees={team.agents} /> : <p className="aiDirectoryEmpty">No active AI Employees team is available to this session yet.</p>}
    </Panel>
  );
}

function ConnectedAppsPanel({ apps, loaded }: { apps: ConnectedApp[]; loaded: boolean }) {
  return (
    <Panel eyebrow="Apps" title="Connected data sources" count={apps.length}>
      <div className="cockpitList">
        {loaded && apps.length === 0 ? <p className="mutedLine">No apps connected yet.</p> : null}
        {apps.slice(0, 8).map((app) => (
          <article className="signalRow" key={app.id}>
            <div>
              <strong>{app.displayName}</strong>
              <span>{app.accountName} / {app.spaces} spaces / {app.purposes} purposes</span>
            </div>
            <StatusBadge tone={statusTone(app.status)}>{labelFor(app.status)}</StatusBadge>
          </article>
        ))}
      </div>
    </Panel>
  );
}

function Signal({
  label,
  tone,
  value,
}: {
  label: string;
  tone: "danger" | "neutral" | "running" | "success" | "warning";
  value: string;
}) {
  return (
    <div className="signalTile">
      <span>{label}</span>
      <strong>{value}</strong>
      <i className={`signalDot ${tone}`} aria-hidden="true" />
    </div>
  );
}
