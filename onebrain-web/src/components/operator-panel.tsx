"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { MetricStrip, Notice, PageHeader, Panel, Tabs } from "@/components/admin-ui";
import {
  approveOperatorRelease,
  designateDevelopmentGate,
  getDevelopmentGate,
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
  pauseOperatorRelease,
  provisionDevelopmentGate,
  resumeOperatorRelease,
  retryDevelopmentRelease,
  retryProvisioningRun,
  revokeAccountServiceKey,
  startOperatorRollout,
  uploadProductionSignature,
  yankOperatorRelease,
} from "@/lib/onebrain-client";
import type {
  BrandTheme,
  BrandThemeInput,
  DevelopmentGate,
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
  | "promotion"
  | "gate"
  | "plan"
  | "rollout"
  | "revoke"
  | "retry"
  | "secret"
  | "";

type OperatorTab = "customers" | "provisioning" | "releases" | "rollouts" | "credentials";

const HETZNER_DEPLOYMENT_TYPE = "dedicated_server";
const HETZNER_REGION = "nbg1";

const RELEASE_RINGS = [
  { label: "Manual", value: "manual" },
  { label: "Internal", value: "internal" },
  { label: "Pilot", value: "pilot" },
  { label: "Early", value: "early" },
  { label: "Stable", value: "stable" },
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

const RELEASE_DATE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  timeZone: "UTC",
  year: "numeric",
});

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

function releaseTimestamp(release: OperatorRelease): number {
  const timestamp = Date.parse(release.created_at);
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function releaseOptionLabel(release: OperatorRelease): string {
  const timestamp = releaseTimestamp(release);
  return timestamp ? `${release.version} - ${RELEASE_DATE_FORMATTER.format(timestamp)}` : release.version;
}

function customerApprovedReleases(releases: OperatorRelease[]): OperatorRelease[] {
  const promotionAware = releases.some((release) => release.promotion);
  return releases.filter((release) => promotionAware
    ? release.promotion?.state === "customer_approved"
    : release.status === "active");
}

export function OperatorPanel() {
  const [bundles, setBundles] = useState<ProvisioningBundle[]>([]);
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [deployments, setDeployments] = useState<DeploymentRow[]>([]);
  const [releases, setReleases] = useState<OperatorRelease[]>([]);
  const [developmentGate, setDevelopmentGate] = useState<DevelopmentGate | null>(null);
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
  const [showProvisionForm, setShowProvisionForm] = useState(false);
  const [signatureByRelease, setSignatureByRelease] = useState<Record<string, string>>({});
  const [signatureKeyByRelease, setSignatureKeyByRelease] = useState<Record<string, string>>({});
  const [promotionNoteByRelease, setPromotionNoteByRelease] = useState<Record<string, string>>({});
  const [developmentOwnerEmail, setDevelopmentOwnerEmail] = useState("");

  const [provisionName, setProvisionName] = useState("");
  const [provisionOwnerEmail, setProvisionOwnerEmail] = useState("");
  const [provisionBundle, setProvisionBundle] = useState("full_stack");
  const [selectedProvisionVersion, setSelectedProvisionVersion] = useState("");
  const [provisionRing, setProvisionRing] = useState("manual");
  const [provisionAccountId, setProvisionAccountId] = useState("");
  const [provisionCallbackUrl] = useState(() =>
    typeof window === "undefined" ? "" : `${window.location.origin}/api/onebrain/provisioning/runs/{run_id}/callback`,
  );
  const [provisionBrandTheme, setProvisionBrandTheme] = useState<BrandThemeInput>(DEFAULT_BRAND_THEME);


  const customerStats = useMemo(() => {
    const deployed = customers.filter((row) => row.deployment).length;
    const healthy = customers.filter((row) => row.readiness === "healthy").length;
    const attention = customers.filter((row) => ["backup_failed", "health_failed", "rollout_failed"].includes(row.readiness)).length;
    const activeKeys = customers.reduce((total, row) => total + activeCount(row.service_keys), 0);
    return { activeKeys, attention, deployed, healthy };
  }, [customers]);

  const selectedProvisionBundle = useMemo(
    () => bundles.find((bundle) => bundle.id === provisionBundle) ?? null,
    [bundles, provisionBundle],
  );

  const eligibleProvisionReleases = useMemo(() => {
    if (!selectedProvisionBundle) {
      return [];
    }
    return customerApprovedReleases(releases)
      .filter((release) => selectedProvisionBundle.modules.every((moduleId) => Boolean(release.images?.[moduleId])))
      .sort((left, right) => {
        const timestampDifference = releaseTimestamp(right) - releaseTimestamp(left);
        return timestampDifference || right.version.localeCompare(left.version, undefined, { numeric: true });
      });
  }, [releases, selectedProvisionBundle]);

  const approvedCustomerReleases = useMemo(
    () => customerApprovedReleases(releases),
    [releases],
  );

  const hasSelectedProvisionVersion = eligibleProvisionReleases.some(
    (release) => release.version === selectedProvisionVersion,
  );
  const provisionVersion = hasSelectedProvisionVersion
    ? selectedProvisionVersion
    : eligibleProvisionReleases[0]?.version ?? "";
  const hasDeployableProvisionVersion = Boolean(provisionVersion);

  const loadOperator = useCallback(async () => {
    setBusyAction("load");
    setBusyId("");
    setError("");
    try {
      const [nextBundles, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns, nextGate] = await Promise.all([
        listProvisioningBundles(),
        listOperatorCustomers(),
        listOperatorDeployments(),
        listOperatorReleases(),
        listProvisioningRuns(),
        getDevelopmentGate().catch(() => null),
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
      setDevelopmentGate(nextGate);
      setProvisionBundle((current) => nextBundles.some((bundle) => bundle.id === current) ? current : nextBundles[0]?.id ?? "");
      setTargetReleaseByDeployment((current) => {
        const approved = customerApprovedReleases(nextReleases);
        const next: Record<string, string> = {};
        for (const row of nextRows) {
          const selected = current[row.deployment.id];
          next[row.deployment.id] = selected && approved.some((release) => release.version === selected)
            ? selected
            : approved[approved.length - 1]?.version ?? "";
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
        const [nextBundles, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns, nextGate] = await Promise.all([
          listProvisioningBundles(),
          listOperatorCustomers(),
          listOperatorDeployments(),
          listOperatorReleases(),
          listProvisioningRuns(),
        getDevelopmentGate().catch(() => null),
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
        setDevelopmentGate(nextGate);
        setProvisionBundle((current) => nextBundles.some((bundle) => bundle.id === current)
          ? current
          : nextBundles[0]?.id ?? "");
        const approved = customerApprovedReleases(nextReleases);
        setTargetReleaseByDeployment(Object.fromEntries(nextRows.map((row) => [
          row.deployment.id,
          approved[approved.length - 1]?.version ?? "",
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
    if (!provisionName.trim() || !provisionOwnerEmail.trim() || !provisionBundle
      || !hasDeployableProvisionVersion || !provisionCallbackUrl.trim() || busyAction) {
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
        deployment_type: HETZNER_DEPLOYMENT_TYPE,
        initial_version: provisionVersion,
        owner_email: provisionOwnerEmail,
        external_provisioning: true,
        dry_run: false,
        callback_url: provisionCallbackUrl,
        region: HETZNER_REGION,
        release_ring: provisionRing,
      });
      setCredentials(result.credentials || []);
      if (result.provisioning_run) {
        setProvisioningRuns((current) => [result.provisioning_run as ProvisioningRun, ...current]);
      }
      const dispatchFailed = result.provisioning_run?.status === "dispatch_failed";
      setProvisionName("");
      setProvisionOwnerEmail("");
      setProvisionAccountId("");
      setShowProvisionForm(false);
      if (!dispatchFailed) {
        setNotice(result.provisioning_run
          ? `${result.account.name} provisioned; Hetzner run ${labelFor(result.provisioning_run.status)}.`
          : `${result.account.name} provisioned.`);
      }
      await loadOperator();
      if (dispatchFailed) {
        setError(`${result.account.name} was created, but the Hetzner dispatch failed. Review and retry the provisioning run.`);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Provisioning failed.");
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

  async function onDesignateGate(deploymentId: string) {
    if (busyAction) {
      return;
    }
    setBusyAction("gate");
    setBusyId(deploymentId);
    setError("");
    setNotice("");
    try {
      const gate = await designateDevelopmentGate(deploymentId);
      setDevelopmentGate(gate);
      setNotice(`${gate.deployment?.customer_name || deploymentId} is the development gate.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not designate the development gate.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onPrepareDevelopmentGate() {
    if (busyAction || !developmentOwnerEmail.trim()) {
      return;
    }
    setBusyAction("gate");
    setBusyId("provision");
    setError("");
    setNotice("");
    try {
      const result = await provisionDevelopmentGate(developmentOwnerEmail.trim(), true);
      setNotice(`Development server dry run created for ${result.deployment.id}. Review it before live provisioning.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not prepare development provisioning.");
    } finally {
      setBusyAction("");
      setBusyId("");
    }
  }

  async function onPromotionAction(
    release: OperatorRelease,
    action: "retry" | "signature" | "approve" | "pause" | "resume" | "yank",
  ) {
    if (busyAction) {
      return;
    }
    setBusyAction("promotion");
    setBusyId(release.version);
    setError("");
    setNotice("");
    const note = promotionNoteByRelease[release.version] || "";
    try {
      if (action === "retry") {
        await retryDevelopmentRelease(release.version, note);
      } else if (action === "signature") {
        await uploadProductionSignature(
          release.version,
          signatureByRelease[release.version] || "",
          signatureKeyByRelease[release.version] || "",
        );
      } else if (action === "approve") {
        await approveOperatorRelease(release.version, note);
      } else if (action === "pause") {
        await pauseOperatorRelease(release.version, note);
      } else if (action === "resume") {
        await resumeOperatorRelease(release.version, note);
      } else {
        await yankOperatorRelease(release.version, note);
      }
      setNotice(`${release.version}: ${labelFor(action)} completed.`);
      await loadOperator();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${action} release.`);
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
          <>
            <button className="secondaryButton" disabled={Boolean(busyAction)} type="button" onClick={() => void loadOperator()}>
              {busyAction === "load" ? "Refreshing" : "Refresh"}
            </button>
            <button
              className="primaryButton"
              type="button"
              onClick={() => {
                setActiveTab("provisioning");
                setShowProvisionForm(true);
              }}
            >
              Provision
            </button>
          </>
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
          <Panel
            actions={<button className="secondaryButton" type="button" onClick={() => setShowProvisionForm((current) => !current)}>{showProvisionForm ? "Close" : "Create customer"}</button>}
            eyebrow="Provisioning"
            title="Customer setup"
            count={bundles.length}
          >
            <BundleList bundles={bundles} />
            {showProvisionForm ? (
              <form className="operatorForm compactWorkflow" onSubmit={(event) => void onProvision(event)}>
                <TextField label="Customer name" required value={provisionName} onChange={setProvisionName} />
                <TextField label="Owner email" required type="email" value={provisionOwnerEmail} onChange={setProvisionOwnerEmail} />
                <SelectField
                  label="Bundle"
                  options={bundles.map((bundle) => ({ label: bundle.label, value: bundle.id }))}
                  value={provisionBundle}
                  onChange={setProvisionBundle}
                />
                <div className="operatorFormGrid">
                  <SelectField
                    label="Initial version"
                    options={eligibleProvisionReleases.map((release) => ({
                      label: releaseOptionLabel(release),
                      value: release.version,
                    }))}
                    value={provisionVersion}
                    onChange={setSelectedProvisionVersion}
                  />
                  <SelectField label="Release ring" options={RELEASE_RINGS} value={provisionRing} onChange={setProvisionRing} />
                  <ReadOnlyField label="Deployment type" value="Dedicated Hetzner server" />
                  <ReadOnlyField label="Region" value="Nuremberg (nbg1)" />
                </div>
                {eligibleProvisionReleases.length === 0 ? (
                  <p className="mutedLine">No deployable release. Activate a release with images for every module in this bundle.</p>
                ) : null}
                <TextField label="Optional account id" value={provisionAccountId} onChange={setProvisionAccountId} />
                <BrandThemeEditor value={provisionBrandTheme} onChange={setProvisionBrandTheme} />
                <button
                  className="primaryButton"
                  disabled={!provisionName.trim() || !provisionOwnerEmail.trim() || !provisionBundle
                    || !hasDeployableProvisionVersion || !provisionCallbackUrl.trim() || Boolean(busyAction)}
                  type="submit"
                >
                  {busyAction === "provision" ? "Provisioning" : "Provision customer"}
                </button>
              </form>
            ) : (
              <p className="mutedLine">Open customer setup when you are ready to provision a new account.</p>
            )}
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
        <div className="operatorGrid wide">
          <Panel eyebrow="Development" title="Release gate">
            <DevelopmentGateCard
              busyAction={busyAction}
              busyId={busyId}
              deployments={deployments.map((row) => row.deployment)}
              ownerEmail={developmentOwnerEmail}
              gate={developmentGate}
              onDesignate={onDesignateGate}
              onOwnerEmailChange={setDevelopmentOwnerEmail}
              onPrepare={onPrepareDevelopmentGate}
            />
          </Panel>
          <Panel eyebrow="Promotion ledger" title="Releases" count={releases.length}>
            <ReleasePromotionLedger
              busyAction={busyAction}
              busyId={busyId}
              notes={promotionNoteByRelease}
              onAction={onPromotionAction}
              onNoteChange={(version, value) => setPromotionNoteByRelease((current) => ({ ...current, [version]: value }))}
              onSignatureChange={(version, value) => setSignatureByRelease((current) => ({ ...current, [version]: value }))}
              onSignatureKeyChange={(version, value) => setSignatureKeyByRelease((current) => ({ ...current, [version]: value }))}
              releases={releases}
              signatureKeys={signatureKeyByRelease}
              signatures={signatureByRelease}
            />
          </Panel>
        </div>
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
                onStartRollout={onStartRollout}
                onTargetReleaseChange={(deploymentId, version) => {
                  setTargetReleaseByDeployment((current) => ({ ...current, [deploymentId]: version }));
                  setPlans((current) => {
                    const next = { ...current };
                    delete next[deploymentId];
                    return next;
                  });
                }}
                plan={plans[row.deployment.id]}
                releases={approvedCustomerReleases}
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
            {bundle.apps.map((app) => <span key={app}>{labelFor(app)}</span>)}
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

function formattedDate(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isNaN(timestamp) ? "not recorded" : RELEASE_DATE_FORMATTER.format(timestamp);
}

function DevelopmentGateCard({
  busyAction,
  busyId,
  deployments,
  gate,
  onDesignate,
  onOwnerEmailChange,
  onPrepare,
  ownerEmail,
}: {
  busyAction: BusyAction;
  busyId: string;
  deployments: OperatorDeployment[];
  gate: DevelopmentGate | null;
  onDesignate: (deploymentId: string) => Promise<void>;
  onOwnerEmailChange: (value: string) => void;
  onPrepare: () => Promise<void>;
  ownerEmail: string;
}) {
  const candidates = deployments.filter((deployment) => deployment.environment === "development"
    && deployment.deployment_type === "dedicated_server" && !deployment.is_release_gate);
  const deployment = gate?.deployment;
  return (
    <div className="gateCard">
      <div className="promotionPath" aria-label="Release promotion path">
        <span className="complete">Green build</span>
        <i aria-hidden="true" />
        <span className={deployment ? "complete" : ""}>Dev server</span>
        <i aria-hidden="true" />
        <span className={gate?.ready ? "complete" : ""}>Healthy proof</span>
        <i aria-hidden="true" />
        <span>Customer approval</span>
      </div>
      {deployment ? (
        <article className="gateIdentity">
          <div className="operatorRowTitle">
            <strong>{deployment.customer_name}</strong>
            <span className={`statusPill ${gate?.ready ? "success" : "failed"}`}>{gate?.ready ? "ready" : "blocked"}</span>
          </div>
          <code>{deployment.id}</code>
          <dl className="gateFacts">
            <div><dt>Installed</dt><dd>{deployment.current_version || "none"}</dd></div>
            <div><dt>Installed on</dt><dd>{formattedDate(deployment.current_version_deployed_at)}</dd></div>
            <div><dt>Last seen</dt><dd>{formattedDate(deployment.last_heartbeat_at)}</dd></div>
            <div><dt>Health</dt><dd>{deployment.last_heartbeat_healthy ? "healthy" : "not healthy"}</dd></div>
          </dl>
          {gate?.blockers.length ? (
            <div className="pillRail">{gate.blockers.map((blocker) => <span key={blocker}>{labelFor(blocker)}</span>)}</div>
          ) : null}
        </article>
      ) : (
        <div className="signatureForm">
          <p className="mutedLine">No development gate is designated. Prepare the dedicated onebrain-only server, enroll it, and wait for a healthy heartbeat.</p>
          <TextField label="Development owner email" required type="email" value={ownerEmail} onChange={onOwnerEmailChange} />
          <button
            className="secondaryButton"
            disabled={Boolean(busyAction) || !ownerEmail.trim()}
            type="button"
            onClick={() => void onPrepare()}
          >
            {busyAction === "gate" && busyId === "provision" ? "Preparing" : "Prepare dry run"}
          </button>
          <small className="operatorMuted">Live Hetzner creation remains an activation step requiring separate confirmation.</small>
        </div>
      )}
      {candidates.map((candidate) => (
        <div className="gateCandidate" key={candidate.id}>
          <span><strong>{candidate.customer_name}</strong><small>{candidate.id}</small></span>
          <button
            className="secondaryButton"
            disabled={Boolean(busyAction) || candidate.last_heartbeat_healthy !== true}
            type="button"
            onClick={() => void onDesignate(candidate.id)}
          >
            {busyAction === "gate" && busyId === candidate.id ? "Designating" : "Use as gate"}
          </button>
        </div>
      ))}
    </div>
  );
}

type PromotionAction = "retry" | "signature" | "approve" | "pause" | "resume" | "yank";

function ReleasePromotionLedger({
  busyAction,
  busyId,
  notes,
  onAction,
  onNoteChange,
  onSignatureChange,
  onSignatureKeyChange,
  releases,
  signatureKeys,
  signatures,
}: {
  busyAction: BusyAction;
  busyId: string;
  notes: Record<string, string>;
  onAction: (release: OperatorRelease, action: PromotionAction) => Promise<void>;
  onNoteChange: (version: string, value: string) => void;
  onSignatureChange: (version: string, value: string) => void;
  onSignatureKeyChange: (version: string, value: string) => void;
  releases: OperatorRelease[];
  signatureKeys: Record<string, string>;
  signatures: Record<string, string>;
}) {
  if (!releases.length) {
    return <p className="mutedLine">CI has not registered a release candidate yet.</p>;
  }
  return (
    <div className="promotionLedger">
      {releases.map((release) => {
        const promotion = release.promotion;
        const state = promotion?.state || "legacy";
        const busy = busyAction === "promotion" && busyId === release.version;
        const canSign = state === "dev_verified" && !promotion?.production_signature_attached;
        return (
          <article className={`promotionCard state-${state}`} key={release.version}>
            <div className="promotionHeader">
              <div>
                <p className="eyebrow">{formattedDate(release.created_at)}</p>
                <h3>{release.version}</h3>
              </div>
              <span className={`statusPill ${state === "customer_approved" ? "success" : state.includes("failed") || state === "yanked" ? "failed" : "running"}`}>
                {labelFor(state)}
              </span>
            </div>
            <div className="promotionStages" aria-label={`Promotion status for ${release.version}`}>
              <span className={promotion ? "complete" : ""}>Candidate</span>
              <span className={promotion?.dev_verified_at ? "complete" : ""}>Dev verified</span>
              <span className={promotion?.production_signature_attached ? "complete" : ""}>Offline signed</span>
              <span className={state === "customer_approved" ? "complete" : ""}>Customer ready</span>
            </div>
            <div className="operatorMeta">
              <span>{Object.keys(release.modules).length} modules</span>
              <span>{release.rollback_kind || "rollback not classified"}</span>
              <span>{release.git_sha.slice(0, 10)}</span>
            </div>
            {promotion?.failure_reason ? <p className="promotionFailure">{labelFor(promotion.failure_reason)}</p> : null}
            {canSign ? (
              <div className="signatureForm">
                <TextField label="Production key id" value={signatureKeys[release.version] || ""} onChange={(value) => onSignatureKeyChange(release.version, value)} />
                <TextareaField label="Offline signature" value={signatures[release.version] || ""} onChange={(value) => onSignatureChange(release.version, value)} />
                <button
                  className="secondaryButton"
                  disabled={busy || !signatureKeys[release.version]?.trim() || !signatures[release.version]?.trim()}
                  type="button"
                  onClick={() => void onAction(release, "signature")}
                >
                  Verify signature
                </button>
              </div>
            ) : null}
            {promotion && state !== "yanked" ? (
              <TextField label="Review note" value={notes[release.version] || ""} onChange={(value) => onNoteChange(release.version, value)} />
            ) : null}
            <div className="promotionActions">
              {state === "dev_failed" ? <button className="secondaryButton" disabled={busy} type="button" onClick={() => void onAction(release, "retry")}>Retry dev</button> : null}
              {state === "dev_verified" && promotion?.production_signature_attached ? <button className="primaryButton" disabled={busy} type="button" onClick={() => void onAction(release, "approve")}>Approve customers</button> : null}
              {state === "customer_approved" ? <button className="secondaryButton" disabled={busy} type="button" onClick={() => void onAction(release, "pause")}>Pause</button> : null}
              {state === "customer_paused" ? <button className="primaryButton" disabled={busy || !notes[release.version]?.trim()} type="button" onClick={() => void onAction(release, "resume")}>Resume after review</button> : null}
              {promotion && state !== "yanked" ? <button className="textButton dangerLink" disabled={busy} type="button" onClick={() => void onAction(release, "yank")}>Yank</button> : null}
            </div>
            {promotion?.events.length ? (
              <details className="promotionHistory">
                <summary>{promotion.events.length} audit events</summary>
                {promotion.events.slice(-4).reverse().map((event) => (
                  <p key={event.id}><span>{labelFor(event.action)}</span><time>{formattedDate(event.created_at)}</time></p>
                ))}
              </details>
            ) : null}
          </article>
        );
      })}
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
  onStartRollout,
  onTargetReleaseChange,
  plan,
  releases,
  row,
  targetRelease,
}: {
  busyAction: BusyAction;
  busyId: string;
  onPlan: (row: DeploymentRow) => Promise<void>;
  onStartRollout: (row: DeploymentRow) => Promise<void>;
  onTargetReleaseChange: (deploymentId: string, version: string) => void;
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
      <dl className="deploymentDates">
        <div><dt>Server created</dt><dd>{formattedDate(row.deployment.created_at)}</dd></div>
        <div><dt>Version installed</dt><dd>{formattedDate(row.deployment.current_version_deployed_at)}</dd></div>
        <div><dt>Last seen</dt><dd>{formattedDate(row.deployment.last_heartbeat_at)}</dd></div>
      </dl>
      {row.deployment.last_reported_version && row.deployment.last_reported_version !== row.deployment.current_version ? (
        <p className="promotionFailure">Version drift: reports {row.deployment.last_reported_version}, expected {row.deployment.current_version}</p>
      ) : null}
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
        </div>
      </div>
      {plan ? <PlanResult plan={plan} /> : null}
      <div className="rolloutList">
        {latestRollouts.length === 0 ? <p className="operatorMuted">No rollouts.</p> : null}
        {latestRollouts.map((rollout) => (
          <div className="rolloutLine" key={rollout.id}>
            <span>{rollout.target_version} / {rollout.status}</span>
            <small>Authenticated workflow or box reports control this status.</small>
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
      {plan.warnings?.map((warning) => <span key={warning}>Warning: {labelFor(warning)}</span>)}
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

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <input aria-readonly="true" className="input" readOnly value={value} />
    </label>
  );
}

function TextField({
  label,
  onChange,
  required = false,
  type = "text",
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  required?: boolean;
  type?: "email" | "text";
  value: string;
}) {
  return (
    <label className="field">
      <span className="fieldLabel">{label}</span>
      <input
        autoComplete={type === "email" ? "email" : undefined}
        className="input"
        required={required}
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
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
