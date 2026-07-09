"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { MetricStrip, Notice, PageHeader, Panel, Tabs } from "@/components/admin-ui";
import {
  createOperatorRelease,
  getOperatorUpdatePlan,
  latestOperatorBackup,
  latestOperatorHealth,
  listOperatorCustomers,
  listOperatorDeploymentModules,
  listOperatorDeployments,
  listOperatorReleases,
  listOperatorRollouts,
  listProvisioningBundles,
  listProvisioningRuns,
  provisionCustomer,
  readBootstrapSecret,
  recordOperatorBackup,
  recordOperatorHealth,
  retryProvisioningRun,
  revokeAccountServiceKey,
  startOperatorRollout,
  updateOperatorRollout,
} from "@/lib/onebrain-client";
import type {
  BrandTheme,
  BrandThemeInput,
  OperatorBackup,
  OperatorCustomer,
  OperatorDeployment,
  OperatorHealth,
  OperatorModule,
  OperatorRelease,
  OperatorRollout,
  OperatorUpdatePlan,
  ProvisionedCredential,
  ProvisioningBundle,
  ProvisioningRun,
  ServiceKeyInfo,
} from "@/lib/onebrain-types";

type DeploymentRow = {
  backup: OperatorBackup | null;
  deployment: OperatorDeployment;
  health: OperatorHealth | null;
  modules: OperatorModule[];
  rollouts: OperatorRollout[];
};

type BusyAction =
  | "load"
  | "provision"
  | "release"
  | "plan"
  | "rollout"
  | "backup"
  | "health"
  | "status"
  | "revoke"
  | "retry"
  | "secret"
  | "";

type OperatorTab = "customers" | "provisioning" | "releases" | "rollouts" | "credentials";

const DEPLOYMENT_TYPES = [
  { label: "Dedicated Railway", value: "dedicated_railway" },
  { label: "Shared Railway", value: "shared_railway" },
  { label: "Dedicated server", value: "dedicated_server" },
  { label: "Customer owned", value: "customer_owned" },
];

const RELEASE_RINGS = [
  { label: "Manual", value: "manual" },
  { label: "Internal", value: "internal" },
  { label: "Pilot", value: "pilot" },
  { label: "Early", value: "early" },
  { label: "Stable", value: "stable" },
];

const RELEASE_STATUSES = [
  { label: "Draft", value: "draft" },
  { label: "Active", value: "active" },
  { label: "Deprecated", value: "deprecated" },
];

const DEFAULT_BRAND_THEME: BrandThemeInput = {
  name: "Assad Dar",
  primary_color: "#16191e",
  secondary_color: "#3e5573",
  accent_color: "#a66e2f",
  background_color: "#f4f2ee",
  surface_color: "#ffffff",
  text_color: "#101828",
  muted_color: "#5f6671",
  success_color: "#1f7a4d",
  warning_color: "#b98a4e",
  danger_color: "#b4453e",
  logo_url: "",
};

const BRAND_COLOR_FIELDS: Array<{ key: keyof BrandThemeInput; label: string }> = [
  { key: "primary_color", label: "Primary" },
  { key: "secondary_color", label: "Secondary" },
  { key: "accent_color", label: "Accent" },
  { key: "background_color", label: "Background" },
  { key: "surface_color", label: "Surface" },
  { key: "text_color", label: "Text" },
  { key: "muted_color", label: "Muted" },
  { key: "success_color", label: "Success" },
  { key: "warning_color", label: "Warning" },
  { key: "danger_color", label: "Danger" },
];

function labelFor(value: string): string {
  return (value || "none").replace(/_/g, " ");
}

function activeCount(items: Array<{ status: string }> = []): number {
  return items.filter((item) => item.status === "active").length;
}

function readinessTone(readiness: string): string {
  if (readiness === "healthy") {
    return "success";
  }
  if (readiness === "updating") {
    return "running";
  }
  if (["backup_failed", "health_failed", "rollout_failed"].includes(readiness)) {
    return "failed";
  }
  return "";
}

function runTone(status = ""): string {
  if (status === "success" || status === "succeeded") {
    return "success";
  }
  if (status === "running" || status === "pending" || status === "paused" || status === "dispatched") {
    return "running";
  }
  if (status === "failed" || status === "dispatch_failed" || status === "cancelled") {
    return "failed";
  }
  return "";
}

function safeCredentialLabel(credential: ProvisionedCredential): string {
  return credential.label || credential.app_id || credential.id;
}

function stringListPayload(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
}

export function OperatorPanel() {
  const [bundles, setBundles] = useState<ProvisioningBundle[]>([]);
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [deployments, setDeployments] = useState<DeploymentRow[]>([]);
  const [releases, setReleases] = useState<OperatorRelease[]>([]);
  const [provisioningRuns, setProvisioningRuns] = useState<ProvisioningRun[]>([]);
  const [credentials, setCredentials] = useState<ProvisionedCredential[]>([]);
  const [bootstrapSecrets, setBootstrapSecrets] = useState<Record<string, string>>({});
  const [plans, setPlans] = useState<Record<string, OperatorUpdatePlan>>({});
  const [targetReleaseByDeployment, setTargetReleaseByDeployment] = useState<Record<string, string>>({});
  const [busyAction, setBusyAction] = useState<BusyAction>("");
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [activeTab, setActiveTab] = useState<OperatorTab>("customers");

  const [provisionName, setProvisionName] = useState("");
  const [provisionBundle, setProvisionBundle] = useState("full_stack");
  const [provisionVersion, setProvisionVersion] = useState("0.1.0");
  const [provisionRing, setProvisionRing] = useState("manual");
  const [provisionType, setProvisionType] = useState("dedicated_railway");
  const [provisionRegion, setProvisionRegion] = useState("");
  const [provisionAccountId, setProvisionAccountId] = useState("");
  const [provisionExternal, setProvisionExternal] = useState(false);
  const [provisionDryRun, setProvisionDryRun] = useState(true);
  const [provisionCallbackUrl, setProvisionCallbackUrl] = useState(() =>
    typeof window === "undefined" ? "" : `${window.location.origin}/api/onebrain/provisioning/runs/{run_id}/callback`,
  );
  const [provisionBrandTheme, setProvisionBrandTheme] = useState<BrandThemeInput>(DEFAULT_BRAND_THEME);

  const [releaseSourceId, setReleaseSourceId] = useState("");
  const [releaseVersion, setReleaseVersion] = useState("");
  const [releaseModuleVersion, setReleaseModuleVersion] = useState("");
  const [releaseGitSha, setReleaseGitSha] = useState("");
  const [releaseStatus, setReleaseStatus] = useState("draft");
  const [migrationFrom, setMigrationFrom] = useState("");
  const [migrationTo, setMigrationTo] = useState("");
  const [securityNotes, setSecurityNotes] = useState("");
  const [rollbackPlan, setRollbackPlan] = useState("");

  const customerStats = useMemo(() => {
    const deployed = customers.filter((row) => row.deployment).length;
    const healthy = customers.filter((row) => row.readiness === "healthy").length;
    const attention = customers.filter((row) => ["backup_failed", "health_failed", "rollout_failed"].includes(row.readiness)).length;
    const activeKeys = customers.reduce((total, row) => total + activeCount(row.service_keys), 0);
    return { activeKeys, attention, deployed, healthy };
  }, [customers]);

  const releaseSource = useMemo(
    () => deployments.find((row) => row.deployment.id === releaseSourceId) ?? null,
    [deployments, releaseSourceId],
  );

  const loadOperator = useCallback(async () => {
    setBusyAction("load");
    setBusyId("");
    setError("");
    try {
      const [nextBundles, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns] = await Promise.all([
        listProvisioningBundles(),
        listOperatorCustomers(),
        listOperatorDeployments(),
        listOperatorReleases(),
        listProvisioningRuns(),
      ]);
      const nextRows = await Promise.all(nextDeployments.map(async (deployment) => {
        const [modules, rollouts, backup, health] = await Promise.all([
          listOperatorDeploymentModules(deployment.id),
          listOperatorRollouts(deployment.id),
          latestOperatorBackup(deployment.id),
          latestOperatorHealth(deployment.id),
        ]);
        return { backup, deployment, health, modules, rollouts };
      }));

      setBundles(nextBundles);
      setCustomers(nextCustomers);
      setDeployments(nextRows);
      setReleases(nextReleases);
      setProvisioningRuns(nextProvisioningRuns);
      setProvisionBundle((current) => nextBundles.some((bundle) => bundle.id === current) ? current : nextBundles[0]?.id ?? "");
      setReleaseSourceId((current) => current && nextRows.some((row) => row.deployment.id === current)
        ? current
        : nextRows[0]?.deployment.id ?? "");
      setTargetReleaseByDeployment((current) => {
        const next: Record<string, string> = {};
        for (const row of nextRows) {
          const selected = current[row.deployment.id];
          next[row.deployment.id] = selected && nextReleases.some((release) => release.version === selected)
            ? selected
            : nextReleases[nextReleases.length - 1]?.version ?? "";
        }
        return next;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load operator dashboard.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }, []);

  useEffect(() => {
    let active = true;
    async function loadInitial() {
      setBusyAction("load");
      setError("");
      try {
        const [nextBundles, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns] = await Promise.all([
          listProvisioningBundles(),
          listOperatorCustomers(),
          listOperatorDeployments(),
          listOperatorReleases(),
          listProvisioningRuns(),
        ]);
        const nextRows = await Promise.all(nextDeployments.map(async (deployment) => {
          const [modules, rollouts, backup, health] = await Promise.all([
            listOperatorDeploymentModules(deployment.id),
            listOperatorRollouts(deployment.id),
            latestOperatorBackup(deployment.id),
            latestOperatorHealth(deployment.id),
          ]);
          return { backup, deployment, health, modules, rollouts };
        }));
        if (!active) {
          return;
        }
        setBundles(nextBundles);
        setCustomers(nextCustomers);
        setDeployments(nextRows);
        setReleases(nextReleases);
        setProvisioningRuns(nextProvisioningRuns);
        setProvisionBundle(nextBundles[0]?.id ?? "full_stack");
        setReleaseSourceId(nextRows[0]?.deployment.id ?? "");
        setTargetReleaseByDeployment(Object.fromEntries(nextRows.map((row) => [
          row.deployment.id,
          nextReleases[nextReleases.length - 1]?.version ?? "",
        ])));
      } catch (err) {
        if (active) {
          setError(err instanceof Error ? err.message : "Could not load operator dashboard.");
        }
      } finally {
        if (active) {
          setBusyAction("");
        }
      }
    }
    void loadInitial();
    return () => {
      active = false;
    };
  }, []);

  async function onProvision(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!provisionName.trim() || !provisionBundle || busyAction) {
      return;
    }
    setBusyAction("provision");
    setError("");
    setNotice("");
    try {
      const result = await provisionCustomer({
        account_id: provisionAccountId,
        brand_theme: {
          ...provisionBrandTheme,
          name: provisionBrandTheme.name?.trim() || provisionName.trim(),
        },
        bundle_id: provisionBundle,
        customer_name: provisionName,
        deployment_type: provisionType,
        initial_version: provisionVersion,
        external_provisioning: provisionExternal,
        dry_run: provisionDryRun,
        callback_url: provisionExternal ? provisionCallbackUrl : "",
        region: provisionRegion,
        release_ring: provisionRing,
      });
      setCredentials(result.credentials || []);
      if (result.provisioning_run) {
        setProvisioningRuns((current) => [result.provisioning_run as ProvisioningRun, ...current]);
      }
      setProvisionName("");
      setProvisionAccountId("");
      setNotice(result.provisioning_run
        ? `${result.account.name} provisioned; external run ${labelFor(result.provisioning_run.status)}.`
        : `${result.account.name} provisioned.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Provisioning failed.");
    } finally {
      setBusyAction("");
    }
  }

  async function onCreateRelease(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!releaseSource || !releaseVersion.trim() || !releaseGitSha.trim() || busyAction) {
      return;
    }
    const moduleVersion = releaseModuleVersion.trim() || releaseVersion.trim();
    setBusyAction("release");
    setError("");
    setNotice("");
    try {
      const modules = Object.fromEntries(releaseSource.modules.map((module) => [module.module_id, moduleVersion]));
      const release = await createOperatorRelease({
        git_sha: releaseGitSha,
        migration_from: migrationFrom,
        migration_to: migrationTo,
        modules,
        rollback_plan: rollbackPlan,
        security_notes: securityNotes,
        status: releaseStatus,
        version: releaseVersion,
      });
      setNotice(`Release ${release.version} created.`);
      setReleaseVersion("");
      setReleaseModuleVersion("");
      setReleaseGitSha("");
      setMigrationFrom("");
      setMigrationTo("");
      setSecurityNotes("");
      setRollbackPlan("");
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Release creation failed.");
    } finally {
      setBusyAction("");
    }
  }

  async function onPlan(row: DeploymentRow) {
    const targetVersion = targetReleaseByDeployment[row.deployment.id] || releases[releases.length - 1]?.version || "";
    if (!targetVersion || busyAction) {
      return;
    }
    setBusyAction("plan");
    setBusyId(row.deployment.id);
    setError("");
    setNotice("");
    try {
      const plan = await getOperatorUpdatePlan(row.deployment.id, targetVersion);
      setPlans((current) => ({ ...current, [row.deployment.id]: plan }));
      setNotice(plan.allowed ? "Update plan allowed." : `Update blocked: ${labelFor(plan.reason)}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Update plan failed.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onStartRollout(row: DeploymentRow) {
    const targetVersion = targetReleaseByDeployment[row.deployment.id] || "";
    if (!targetVersion || busyAction) {
      return;
    }
    setBusyAction("rollout");
    setBusyId(row.deployment.id);
    setError("");
    setNotice("");
    try {
      await startOperatorRollout(row.deployment.id, targetVersion);
      setNotice(`Rollout queued for ${row.deployment.customer_name}.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start rollout.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onRecordRun(row: DeploymentRow, kind: "backup" | "health") {
    if (busyAction) {
      return;
    }
    setBusyAction(kind);
    setBusyId(row.deployment.id);
    setError("");
    setNotice("");
    try {
      if (kind === "backup") {
        await recordOperatorBackup(row.deployment.id, { detail: "Operator recorded pre-update backup.", status: "success" });
        setNotice(`Backup recorded for ${row.deployment.customer_name}.`);
      } else {
        await recordOperatorHealth(row.deployment.id, { detail: "Operator recorded health check.", status: "success" });
        setNotice(`Health recorded for ${row.deployment.customer_name}.`);
      }
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not record ${kind}.`);
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onUpdateRollout(rollout: OperatorRollout, status: string) {
    if (busyAction) {
      return;
    }
    setBusyAction("status");
    setBusyId(rollout.id);
    setError("");
    setNotice("");
    try {
      await updateOperatorRollout(rollout.id, { notes: `Marked ${status} from Next.js operator dashboard.`, status });
      setNotice(`Rollout marked ${status}.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update rollout.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onRevokeKey(accountId: string, key: ServiceKeyInfo) {
    if (busyAction || key.status !== "active") {
      return;
    }
    setBusyAction("revoke");
    setBusyId(key.id);
    setError("");
    setNotice("");
    try {
      await revokeAccountServiceKey(accountId, key.id);
      setNotice(`${key.app_id || key.id} key revoked.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not revoke key.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function copyCredential(credential: ProvisionedCredential) {
    setError("");
    try {
      await navigator.clipboard.writeText(credential.key);
      setNotice(`${safeCredentialLabel(credential)} key copied.`);
    } catch {
      setError("Could not copy the credential.");
    }
  }

  async function onRetryRun(run: ProvisioningRun) {
    if (busyAction) {
      return;
    }
    setBusyAction("retry");
    setBusyId(run.id);
    setError("");
    setNotice("");
    try {
      const retried = await retryProvisioningRun(run.id);
      setProvisioningRuns((current) => [retried, ...current]);
      setNotice(`Retry ${retried.id} started.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not retry provisioning run.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onReadSecret(run: ProvisioningRun) {
    if (busyAction || !run.bootstrap_secret_id) {
      return;
    }
    setBusyAction("secret");
    setBusyId(run.id);
    setError("");
    setNotice("");
    try {
      const secret = await readBootstrapSecret(run.id);
      setBootstrapSecrets((current) => ({ ...current, [run.id]: secret.plaintext }));
      setNotice(`Bootstrap secret ${secret.secret_id} read once.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not read bootstrap secret.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  const operatorTabs = [
    { id: "customers", label: "Customers", meta: customers.length },
    { id: "provisioning", label: "Provisioning", meta: provisioningRuns.length },
    { id: "releases", label: "Releases", meta: releases.length },
    { id: "rollouts", label: "Rollouts", meta: deployments.length },
    { id: "credentials", label: "Credentials", meta: credentials.length },
  ] satisfies Array<{ id: OperatorTab; label: string; meta: number }>;

  return (
    <div className="operatorWorkspace">
      <PageHeader
        eyebrow="Control plane"
        title="Operator"
        meta={(
          <>
            <span className="scopePill">
              <span aria-hidden="true" className="statusDot" />
              {busyAction ? labelFor(busyAction) : "Live"}
            </span>
            <span className="scopePill">{deployments.length} deployments</span>
          </>
        )}
        actions={(
          <button className="secondaryButton" disabled={Boolean(busyAction)} type="button" onClick={() => void loadOperator()}>
            {busyAction === "load" ? "Refreshing" : "Refresh"}
          </button>
        )}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "customers", value: customers.length },
          { label: "deployed", value: customerStats.deployed },
          { label: "healthy", tone: "success", value: customerStats.healthy },
          { label: "attention", tone: customerStats.attention ? "danger" : undefined, value: customerStats.attention },
          { label: "active keys", value: customerStats.activeKeys },
        ]}
      />

      <Tabs active={activeTab} items={operatorTabs} label="Operator sections" onChange={(tab) => setActiveTab(tab)} />

      {activeTab === "customers" ? (
        <Panel eyebrow="Readiness" title="Customers" count={customers.length}>
          <div className="operatorList">
            {customers.length === 0 ? <p className="mutedLine">No customers provisioned yet.</p> : null}
            {customers.map((customer) => (
              <CustomerRow
                busy={busyAction === "revoke"}
                busyId={busyId}
                customer={customer}
                key={customer.account.id}
                onRevokeKey={onRevokeKey}
              />
            ))}
          </div>
        </Panel>
      ) : null}

      {activeTab === "provisioning" ? (
        <div className="operatorGrid">
          <Panel eyebrow="Provisioning" title="Create customer" count={bundles.length}>
            <BundleList bundles={bundles} />
            <form className="operatorForm" onSubmit={(event) => void onProvision(event)}>
              <TextField label="Customer name" value={provisionName} onChange={setProvisionName} />
              <SelectField
                label="Bundle"
                options={bundles.map((bundle) => ({ label: bundle.label, value: bundle.id }))}
                value={provisionBundle}
                onChange={setProvisionBundle}
              />
              <div className="operatorFormGrid">
                <TextField label="Initial version" value={provisionVersion} onChange={setProvisionVersion} />
                <SelectField label="Release ring" options={RELEASE_RINGS} value={provisionRing} onChange={setProvisionRing} />
                <SelectField label="Deployment type" options={DEPLOYMENT_TYPES} value={provisionType} onChange={setProvisionType} />
                <TextField label="Region" value={provisionRegion} onChange={setProvisionRegion} />
              </div>
              <TextField label="Optional account id" value={provisionAccountId} onChange={setProvisionAccountId} />
              <div className="operatorChecks" aria-label="External provisioning options">
                <label>
                  <input
                    checked={provisionExternal}
                    onChange={(event) => setProvisionExternal(event.target.checked)}
                    type="checkbox"
                  />
                  Dispatch external workflow
                </label>
                <label>
                  <input
                    checked={provisionDryRun}
                    disabled={!provisionExternal}
                    onChange={(event) => setProvisionDryRun(event.target.checked)}
                    type="checkbox"
                  />
                  Dry run
                </label>
              </div>
              {provisionExternal ? (
                <TextField label="Callback URL" value={provisionCallbackUrl} onChange={setProvisionCallbackUrl} />
              ) : null}
              <BrandThemeEditor value={provisionBrandTheme} onChange={setProvisionBrandTheme} />
              <button
                className="primaryButton"
                disabled={!provisionName.trim() || !provisionBundle || (provisionExternal && !provisionCallbackUrl.trim()) || Boolean(busyAction)}
                type="submit"
              >
                {busyAction === "provision" ? "Provisioning" : "Provision customer"}
              </button>
            </form>
            <CredentialPanel credentials={credentials} onCopy={copyCredential} />
          </Panel>

          <Panel eyebrow="Runs" title="Provisioning ledger" count={provisioningRuns.length}>
            <ProvisioningRunList
              bootstrapSecrets={bootstrapSecrets}
              busyAction={busyAction}
              busyId={busyId}
              onReadSecret={onReadSecret}
              onRetry={onRetryRun}
              runs={provisioningRuns}
            />
          </Panel>
        </div>
      ) : null}

      {activeTab === "releases" ? (
        <Panel eyebrow="Releases" title="Manifest" count={releases.length}>
          <ReleaseRail releases={releases} />
          <form className="operatorForm" onSubmit={(event) => void onCreateRelease(event)}>
            <SelectField
              label="Source deployment"
              options={deployments.map((row) => ({
                label: `${row.deployment.customer_name} / ${row.deployment.id}`,
                value: row.deployment.id,
              }))}
              value={releaseSourceId}
              onChange={(value) => {
                setReleaseSourceId(value);
                const row = deployments.find((item) => item.deployment.id === value);
                setMigrationFrom(row?.deployment.current_migration || "");
              }}
            />
            <ModulePreview row={releaseSource} targetVersion={releaseModuleVersion || releaseVersion || "target"} />
            <div className="operatorFormGrid">
              <TextField label="Release version" value={releaseVersion} onChange={setReleaseVersion} />
              <TextField label="Module version" value={releaseModuleVersion} onChange={setReleaseModuleVersion} />
              <TextField label="Git SHA" value={releaseGitSha} onChange={setReleaseGitSha} />
              <SelectField label="Status" options={RELEASE_STATUSES} value={releaseStatus} onChange={setReleaseStatus} />
            </div>
            <div className="operatorFormGrid">
              <TextField label="Migration from" value={migrationFrom} onChange={setMigrationFrom} />
              <TextField label="Migration to" value={migrationTo} onChange={setMigrationTo} />
            </div>
            <TextareaField label="Security notes" value={securityNotes} onChange={setSecurityNotes} />
            <TextareaField label="Rollback plan" value={rollbackPlan} onChange={setRollbackPlan} />
            <button
              className="primaryButton"
              disabled={!releaseSource || !releaseVersion.trim() || !releaseGitSha.trim() || Boolean(busyAction)}
              type="submit"
            >
              {busyAction === "release" ? "Creating" : "Create release"}
            </button>
          </form>
        </Panel>
      ) : null}

      {activeTab === "rollouts" ? (
        <Panel eyebrow="Rollouts" title="Deployments" count={deployments.length}>
          <div className="operatorList">
            {deployments.length === 0 ? <p className="mutedLine">No deployments tracked yet.</p> : null}
            {deployments.map((row) => (
              <DeploymentCard
                busyAction={busyAction}
                busyId={busyId}
                key={row.deployment.id}
                onPlan={onPlan}
                onRecordRun={onRecordRun}
                onStartRollout={onStartRollout}
                onTargetReleaseChange={(deploymentId, version) => {
                  setTargetReleaseByDeployment((current) => ({ ...current, [deploymentId]: version }));
                  setPlans((current) => {
                    const next = { ...current };
                    delete next[deploymentId];
                    return next;
                  });
                }}
                onUpdateRollout={onUpdateRollout}
                plan={plans[row.deployment.id]}
                releases={releases}
                row={row}
                targetRelease={targetReleaseByDeployment[row.deployment.id] || ""}
              />
            ))}
          </div>
        </Panel>
      ) : null}

      {activeTab === "credentials" ? (
        <Panel eyebrow="Service keys" title="Provisioned credentials" count={credentials.length}>
          {credentials.length ? (
            <CredentialPanel credentials={credentials} onCopy={copyCredential} />
          ) : (
            <p className="mutedLine">Provision a customer to display newly issued keys.</p>
          )}
        </Panel>
      ) : null}
    </div>
  );
}

function BundleList({ bundles }: { bundles: ProvisioningBundle[] }) {
  if (!bundles.length) {
    return <p className="mutedLine">No provisioning bundles registered.</p>;
  }
  return (
    <div className="operatorTokenList">
      {bundles.map((bundle) => (
        <article className="operatorToken" key={bundle.id}>
          <strong>{bundle.label}</strong>
          <span>{bundle.description}</span>
          <div className="pillRail">
            {bundle.spaces.map((space) => <span key={space}>{labelFor(space)}</span>)}
            <span>{bundle.modules.length} modules</span>
          </div>
        </article>
      ))}
    </div>
  );
}

function CredentialPanel({
  credentials,
  onCopy,
}: {
  credentials: ProvisionedCredential[];
  onCopy: (credential: ProvisionedCredential) => Promise<void>;
}) {
  if (!credentials.length) {
    return null;
  }
  return (
    <div className="credentialList" aria-label="Provisioned credentials">
      <div className="sectionHead">
        <span>New keys</span>
        <span>{credentials.length}</span>
      </div>
      {credentials.map((credential) => (
        <article className="credentialRow" key={credential.id}>
          <div>
            <strong>{safeCredentialLabel(credential)}</strong>
            <span>{credential.id}</span>
            <code>{credential.key}</code>
          </div>
          <button className="secondaryButton" type="button" onClick={() => void onCopy(credential)}>
            Copy
          </button>
        </article>
      ))}
    </div>
  );
}

function ProvisioningRunList({
  bootstrapSecrets,
  busyAction,
  busyId,
  onReadSecret,
  onRetry,
  runs,
}: {
  bootstrapSecrets: Record<string, string>;
  busyAction: BusyAction;
  busyId: string;
  onReadSecret: (run: ProvisioningRun) => void;
  onRetry: (run: ProvisioningRun) => void;
  runs: ProvisioningRun[];
}) {
  const visibleRuns = runs.slice(0, 8);
  const retryable = new Set(["failed", "cancelled", "dispatch_failed"]);

  return (
    <section className="runLedger" aria-labelledby="provisioningRunsTitle">
      <div className="panelHead compact">
        <div>
          <p className="eyebrow">Runs</p>
          <h3 id="provisioningRunsTitle">Provisioning ledger</h3>
        </div>
        <span>{runs.length}</span>
      </div>
      <div className="operatorList compactList">
        {visibleRuns.length === 0 ? <p className="mutedLine">No external runs yet.</p> : null}
        {visibleRuns.map((run) => {
          const pendingModules = stringListPayload(run.result_payload?.module_services_pending_code);
          return (
          <article className="operatorRow compactRow" key={run.id}>
            <div className="operatorRowMain">
              <div className="operatorRowTitle">
                <strong>{run.account_id}</strong>
                <span className={`statusPill ${runTone(run.status)}`}>{labelFor(run.status)}</span>
              </div>
              <p>{run.deployment_id}</p>
              <div className="operatorMeta">
                <span>{run.bundle_id}</span>
                {run.railway_project_id ? <span>{run.railway_project_id}</span> : null}
                {run.external_run_url ? (
                  <a href={run.external_run_url} rel="noreferrer" target="_blank">workflow</a>
                ) : null}
                {run.smoke_status ? <span>{labelFor(run.smoke_status)}</span> : null}
              </div>
              {Object.entries(run.service_urls || {}).length ? (
                <div className="operatorMeta">
                  {Object.entries(run.service_urls).map(([label, url]) => (
                    url.startsWith("http") ? (
                      <a href={url} key={label} rel="noreferrer" target="_blank">{labelFor(label)}</a>
                    ) : (
                      <span key={label}>{labelFor(label)}</span>
                    )
                  ))}
                </div>
              ) : null}
              {pendingModules.length ? (
                <div className="operatorMeta">
                  {pendingModules.map((module) => <span key={module}>{module} pending image</span>)}
                </div>
              ) : null}
              {run.failure_reason ? <p className="operatorMuted">{run.failure_reason}</p> : null}
              {bootstrapSecrets[run.id] ? <code className="credentialSecret">{bootstrapSecrets[run.id]}</code> : null}
            </div>
            <div className="operatorButtonGrid">
              <button
                className="secondaryButton"
                disabled={Boolean(busyAction) || !retryable.has(run.status)}
                onClick={() => onRetry(run)}
                type="button"
              >
                {busyAction === "retry" && busyId === run.id ? "Retrying" : "Retry"}
              </button>
              <button
                className="secondaryButton"
                disabled={Boolean(busyAction) || !run.bootstrap_secret_id || Boolean(bootstrapSecrets[run.id])}
                onClick={() => onReadSecret(run)}
                type="button"
              >
                {busyAction === "secret" && busyId === run.id ? "Reading" : "Secret"}
              </button>
            </div>
          </article>
          );
        })}
      </div>
    </section>
  );
}

function CustomerRow({
  busy,
  busyId,
  customer,
  onRevokeKey,
}: {
  busy: boolean;
  busyId: string;
  customer: OperatorCustomer;
  onRevokeKey: (accountId: string, key: ServiceKeyInfo) => Promise<void>;
}) {
  const deployment = customer.deployment;
  return (
    <article className="operatorRow">
      <div className="operatorRowMain">
        <div className="operatorRowTitle">
          <strong>{customer.account.name}</strong>
          <code>{customer.account.id}</code>
        </div>
        <span>
          {deployment
            ? `${deployment.deployment_type} / ${deployment.release_ring} / ${deployment.current_version || "no version"}`
            : `${customer.account.kind} / no deployment`}
        </span>
        <div className="pillRail">
          <span>{customer.spaces.length} spaces</span>
          <span>{customer.apps.length} apps</span>
          <span>{activeCount(customer.service_keys)} active keys</span>
          {customer.modules.slice(0, 5).map((module) => (
            <span key={`${module.module_id}-${module.version}`}>{module.module_id} {module.version}</span>
          ))}
        </div>
        <ThemeSwatches theme={customer.brand_theme} />
        {customer.service_keys.length ? (
          <div className="serviceKeyList">
            {customer.service_keys.map((key) => (
              <div className="serviceKeyLine" key={key.id}>
                <span>{key.app_id || "tenant"} / {key.status} / {key.id}</span>
                <button
                  className="textButton dangerLink"
                  disabled={key.status !== "active" || (busy && busyId === key.id)}
                  type="button"
                  onClick={() => void onRevokeKey(customer.account.id, key)}
                >
                  {busy && busyId === key.id ? "Revoking" : "Revoke"}
                </button>
              </div>
            ))}
          </div>
        ) : null}
      </div>
      <div className="operatorSignals">
        <span className={`readinessChip ${readinessTone(customer.readiness)}`.trim()}>{labelFor(customer.readiness)}</span>
        <span className={`readinessChip ${runTone(customer.backup?.status)}`.trim()}>backup {customer.backup?.status || "none"}</span>
        <span className={`readinessChip ${runTone(customer.health?.status)}`.trim()}>health {customer.health?.status || "none"}</span>
        <span>{customer.latest_rollout ? `${customer.latest_rollout.target_version} / ${customer.latest_rollout.status}` : "no rollout"}</span>
      </div>
    </article>
  );
}

function ReleaseRail({ releases }: { releases: OperatorRelease[] }) {
  if (!releases.length) {
    return <p className="mutedLine">No release manifests registered.</p>;
  }
  return (
    <div className="releaseRail">
      {releases.map((release) => (
        <span className="releaseToken" key={release.version} title={release.git_sha}>
          <strong>{release.version}</strong>
          <small>{release.status} / {Object.keys(release.modules).length} modules</small>
        </span>
      ))}
    </div>
  );
}

function ModulePreview({ row, targetVersion }: { row: DeploymentRow | null; targetVersion: string }) {
  if (!row) {
    return <p className="mutedLine">Provision a deployment before creating a release.</p>;
  }
  return (
    <div className="pillRail">
      {row.modules.length === 0 ? <span>No modules on source deployment</span> : null}
      {row.modules.map((module) => (
        <span key={module.module_id}>{module.module_id} {"->"} {targetVersion}</span>
      ))}
    </div>
  );
}

function BrandThemeEditor({
  onChange,
  value,
}: {
  onChange: (value: BrandThemeInput) => void;
  value: BrandThemeInput;
}) {
  function update(key: keyof BrandThemeInput, nextValue: string) {
    onChange({ ...value, [key]: nextValue });
  }

  return (
    <section className="brandThemeEditor" aria-label="Brand colors">
      <div className="panelHead compactHead">
        <div>
          <p className="eyebrow">Brand</p>
          <h2>Theme colors</h2>
        </div>
      </div>
      <TextField label="Theme name" value={value.name || ""} onChange={(next) => update("name", next)} />
      <div className="brandColorGrid">
        {BRAND_COLOR_FIELDS.map((field) => (
          <ColorField
            key={field.key}
            label={field.label}
            value={String(value[field.key] || "")}
            onChange={(next) => update(field.key, next)}
          />
        ))}
      </div>
      <ThemeSwatches theme={value} />
    </section>
  );
}

function ColorField({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label className="brandColorField">
      <span>{label}</span>
      <input
        aria-label={`${label} color`}
        className="colorInput"
        type="color"
        value={value || "#000000"}
        onChange={(event) => onChange(event.target.value)}
      />
      <code>{value}</code>
    </label>
  );
}

function ThemeSwatches({ theme }: { theme: BrandTheme | BrandThemeInput }) {
  const swatches: Array<[string, string]> = [
    ["Primary", theme.primary_color || "#000000"],
    ["Accent", theme.accent_color || "#000000"],
    ["Background", theme.background_color || "#000000"],
    ["Text", theme.text_color || "#000000"],
  ];

  return (
    <div className="themeSwatches" aria-label="Theme preview">
      {swatches.map(([label, color]) => (
        <span className="themeSwatch" key={label}>
          <i aria-hidden="true" style={{ backgroundColor: color }} />
          {label}
        </span>
      ))}
    </div>
  );
}

function DeploymentCard({
  busyAction,
  busyId,
  onPlan,
  onRecordRun,
  onStartRollout,
  onTargetReleaseChange,
  onUpdateRollout,
  plan,
  releases,
  row,
  targetRelease,
}: {
  busyAction: BusyAction;
  busyId: string;
  onPlan: (row: DeploymentRow) => Promise<void>;
  onRecordRun: (row: DeploymentRow, kind: "backup" | "health") => Promise<void>;
  onStartRollout: (row: DeploymentRow) => Promise<void>;
  onTargetReleaseChange: (deploymentId: string, version: string) => void;
  onUpdateRollout: (rollout: OperatorRollout, status: string) => Promise<void>;
  plan?: OperatorUpdatePlan;
  releases: OperatorRelease[];
  row: DeploymentRow;
  targetRelease: string;
}) {
  const activeBusy = Boolean(busyAction && busyId === row.deployment.id);
  const latestRollouts = row.rollouts.slice(-3).reverse();
  const canStart = Boolean(plan?.allowed && Object.keys(plan.modules_to_update || {}).length);
  return (
    <article className="deploymentCard">
      <div className="operatorRowTitle">
        <strong>{row.deployment.customer_name}</strong>
        <code>{row.deployment.id}</code>
      </div>
      <span className="operatorMuted">
        {row.deployment.deployment_type} / {row.deployment.release_ring} / {row.deployment.current_version || "no version"}
      </span>
      <div className="pillRail">
        {row.modules.map((module) => (
          <span key={module.module_id}>{module.module_id} {module.version}</span>
        ))}
      </div>
      <div className="runSignals">
        <span className={`readinessChip ${runTone(row.backup?.status)}`.trim()}>backup {row.backup?.status || "none"}</span>
        <span className={`readinessChip ${runTone(row.health?.status)}`.trim()}>health {row.health?.status || "none"}</span>
      </div>
      <div className="deploymentControls">
        <SelectField
          label="Target release"
          options={releases.map((release) => ({ label: release.version, value: release.version }))}
          value={targetRelease}
          onChange={(value) => onTargetReleaseChange(row.deployment.id, value)}
        />
        <div className="operatorButtonGrid">
          <button className="secondaryButton" disabled={!targetRelease || activeBusy} type="button" onClick={() => void onPlan(row)}>
            {busyAction === "plan" && busyId === row.deployment.id ? "Planning" : "Plan"}
          </button>
          <button className="primaryButton" disabled={!canStart || activeBusy} type="button" onClick={() => void onStartRollout(row)}>
            {busyAction === "rollout" && busyId === row.deployment.id ? "Starting" : "Start rollout"}
          </button>
          <button className="secondaryButton" disabled={activeBusy} type="button" onClick={() => void onRecordRun(row, "backup")}>
            Backup ok
          </button>
          <button className="secondaryButton" disabled={activeBusy} type="button" onClick={() => void onRecordRun(row, "health")}>
            Health ok
          </button>
        </div>
      </div>
      {plan ? <PlanResult plan={plan} /> : null}
      <div className="rolloutList">
        {latestRollouts.length === 0 ? <p className="operatorMuted">No rollouts.</p> : null}
        {latestRollouts.map((rollout) => (
          <div className="rolloutLine" key={rollout.id}>
            <span>{rollout.target_version} / {rollout.status}</span>
            {!["success", "failed"].includes(rollout.status) ? (
              <div className="rolloutActions">
                {["running", "success", "failed"].map((status) => (
                  <button
                    className={status === "failed" ? "textButton dangerLink" : "textButton"}
                    disabled={busyAction === "status" && busyId === rollout.id}
                    key={status}
                    type="button"
                    onClick={() => void onUpdateRollout(rollout, status)}
                  >
                    {labelFor(status)}
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ))}
      </div>
    </article>
  );
}

function PlanResult({ plan }: { plan: OperatorUpdatePlan }) {
  return (
    <div className={plan.allowed ? "planResult allowed" : "planResult denied"}>
      <strong>{plan.allowed ? "Allowed" : "Blocked"}</strong>
      <span>{labelFor(plan.reason)}</span>
      <div className="pillRail">
        {Object.entries(plan.modules_to_update).map(([moduleId, version]) => (
          <span key={moduleId}>{moduleId} {"->"} {version}</span>
        ))}
      </div>
    </div>
  );
}

function SelectField({
  label,
  onChange,
  options,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  options: Array<{ label: string; value: string }>;
  value: string;
}) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <select className="select" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.length === 0 ? <option value="">None available</option> : null}
        {options.map((option) => (
          <option key={option.value} value={option.value}>{option.label}</option>
        ))}
      </select>
    </label>
  );
}

function TextField({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <input className="input" value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function TextareaField({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <textarea className="textarea" rows={3} value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}
