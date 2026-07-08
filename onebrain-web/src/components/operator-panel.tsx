"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
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
  provisionCustomer,
  recordOperatorBackup,
  recordOperatorHealth,
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
  | "";

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
  if (status === "success") {
    return "success";
  }
  if (status === "running" || status === "pending" || status === "paused") {
    return "running";
  }
  if (status === "failed") {
    return "failed";
  }
  return "";
}

function safeCredentialLabel(credential: ProvisionedCredential): string {
  return credential.label || credential.app_id || credential.id;
}

export function OperatorPanel() {
  const [bundles, setBundles] = useState<ProvisioningBundle[]>([]);
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [deployments, setDeployments] = useState<DeploymentRow[]>([]);
  const [releases, setReleases] = useState<OperatorRelease[]>([]);
  const [credentials, setCredentials] = useState<ProvisionedCredential[]>([]);
  const [plans, setPlans] = useState<Record<string, OperatorUpdatePlan>>({});
  const [targetReleaseByDeployment, setTargetReleaseByDeployment] = useState<Record<string, string>>({});
  const [busyAction, setBusyAction] = useState<BusyAction>("");
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [provisionName, setProvisionName] = useState("");
  const [provisionBundle, setProvisionBundle] = useState("full_stack");
  const [provisionVersion, setProvisionVersion] = useState("0.1.0");
  const [provisionRing, setProvisionRing] = useState("manual");
  const [provisionType, setProvisionType] = useState("dedicated_railway");
  const [provisionRegion, setProvisionRegion] = useState("");
  const [provisionAccountId, setProvisionAccountId] = useState("");
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
      const [nextBundles, nextCustomers, nextDeployments, nextReleases] = await Promise.all([
        listProvisioningBundles(),
        listOperatorCustomers(),
        listOperatorDeployments(),
        listOperatorReleases(),
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
        const [nextBundles, nextCustomers, nextDeployments, nextReleases] = await Promise.all([
          listProvisioningBundles(),
          listOperatorCustomers(),
          listOperatorDeployments(),
          listOperatorReleases(),
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
        region: provisionRegion,
        release_ring: provisionRing,
      });
      setCredentials(result.credentials || []);
      setProvisionName("");
      setProvisionAccountId("");
      setNotice(`${result.account.name} provisioned.`);
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

  return (
    <div className="operatorWorkspace">
      <header className="documentsTopbar">
        <div>
          <p className="eyebrow">Control plane</p>
          <h1>Operator</h1>
        </div>
        <button className="secondaryButton" disabled={Boolean(busyAction)} type="button" onClick={() => void loadOperator()}>
          {busyAction === "load" ? "Refreshing" : "Refresh"}
        </button>
      </header>

      {error ? <div className="inlineError">{error}</div> : null}
      {notice ? <div className="inlineNotice">{notice}</div> : null}

      <section className="operatorSummary" aria-label="Operator summary">
        <SummaryStat label="customers" value={customers.length} />
        <SummaryStat label="deployed" value={customerStats.deployed} />
        <SummaryStat label="healthy" value={customerStats.healthy} />
        <SummaryStat label="attention" value={customerStats.attention} />
        <SummaryStat label="active keys" value={customerStats.activeKeys} />
      </section>

      <div className="operatorGrid">
        <section className="operatorPanel" aria-labelledby="provisionTitle">
          <div className="panelHead">
            <div>
              <p className="eyebrow">Provisioning</p>
              <h2 id="provisionTitle">Create customer</h2>
            </div>
            <span>{bundles.length}</span>
          </div>
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
            <BrandThemeEditor value={provisionBrandTheme} onChange={setProvisionBrandTheme} />
            <button className="primaryButton" disabled={!provisionName.trim() || !provisionBundle || Boolean(busyAction)} type="submit">
              {busyAction === "provision" ? "Provisioning" : "Provision customer"}
            </button>
          </form>
          <CredentialPanel credentials={credentials} onCopy={copyCredential} />
        </section>

        <section className="operatorPanel" aria-labelledby="customerReadinessTitle">
          <div className="panelHead">
            <div>
              <p className="eyebrow">Readiness</p>
              <h2 id="customerReadinessTitle">Customers</h2>
            </div>
            <span>{customers.length}</span>
          </div>
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
        </section>
      </div>

      <div className="operatorGrid wide">
        <section className="operatorPanel" aria-labelledby="releaseTitle">
          <div className="panelHead">
            <div>
              <p className="eyebrow">Releases</p>
              <h2 id="releaseTitle">Manifest</h2>
            </div>
            <span>{releases.length}</span>
          </div>
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
        </section>

        <section className="operatorPanel" aria-labelledby="deploymentTitle">
          <div className="panelHead">
            <div>
              <p className="eyebrow">Rollouts</p>
              <h2 id="deploymentTitle">Deployments</h2>
            </div>
            <span>{deployments.length}</span>
          </div>
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
        </section>
      </div>
    </div>
  );
}

function SummaryStat({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <strong>{value}</strong>
      <span>{label}</span>
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
    <section className="credentialPanel" aria-label="Provisioned credentials">
      <div className="panelHead compactHead">
        <div>
          <p className="eyebrow">Credentials</p>
          <h2>New keys</h2>
        </div>
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
