"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { MetricStrip, Notice, PageHeader, Panel, Tabs } from "@/components/admin-ui";
import { ExpandableCard } from "@/components/operational/expandable-card";
import { StatusSummary } from "@/components/operational/status-summary";
import { Timestamp } from "@/components/operational/timestamp";
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
  getProvisioningModuleCatalog,
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
  ProvisioningModule,
  ProvisioningModuleCatalog,
  ProvisioningRun,
  ServiceKeyInfo,
} from "@/lib/onebrain-types";
import { describeOperationalStatus, formatOperationalTimestamp } from "@/lib/operational";

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
  { label: "Internal pilot", value: "internal" },
  { label: "Early pilot", value: "pilot" },
  { label: "Early stable", value: "early" },
  { label: "Stable", value: "stable" },
];

const RELEASE_RING_HELP: Record<string, string> = {
  manual: "No automatic rollout. An operator explicitly chooses every update.",
  internal: "Use for the internal team and development validation only.",
  pilot: "Use for a small, agreed pilot group after internal validation.",
  early: "Use for early stable customers after the pilot has proved healthy.",
  stable: "Use for the standard customer rollout after release approval.",
};

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
  const timestamp = formatOperationalTimestamp(release.created_at);
  return timestamp.isMissing ? release.version : `${release.version} — ${timestamp.local}`;
}

function newestFirst(releases: OperatorRelease[]): OperatorRelease[] {
  return [...releases].sort((left, right) => {
    const timestampDifference = releaseTimestamp(right) - releaseTimestamp(left);
    return timestampDifference || right.version.localeCompare(left.version, undefined, { numeric: true });
  });
}

function customerApprovedReleases(releases: OperatorRelease[]): OperatorRelease[] {
  const promotionAware = releases.some((release) => release.promotion);
  return releases
    .filter((release) => promotionAware
      ? release.promotion?.state === "customer_approved"
      : release.status === "active")
    .sort((left, right) => releaseTimestamp(right) - releaseTimestamp(left) || right.version.localeCompare(left.version, undefined, { numeric: true }));
}

function promotionStatus(state: string) {
  if (state === "customer_approved") {
    return { condition: "Customer ready", explanation: "The release has passed the required checks and can be selected for customers.", nextAction: "Choose a customer rollout when you are ready.", tone: "success" as const };
  }
  if (state === "dev_verified") {
    return { condition: "Awaiting production signature", explanation: "Development validation passed; the release still needs its production signature.", nextAction: "Attach the offline production signature.", tone: "warning" as const };
  }
  if (state === "customer_paused") {
    return { condition: "Customer rollout paused", explanation: "This release is not available for new customer rollout until it is reviewed.", nextAction: "Add a review note, then resume or yank the release.", tone: "warning" as const };
  }
  if (state === "dev_failed" || state === "yanked") {
    return { condition: state === "yanked" ? "Release withdrawn" : "Development validation failed", explanation: "This release cannot be selected for customers in its current state.", nextAction: state === "yanked" ? "Create or select a newer release." : "Review the failure, then retry development validation.", tone: "danger" as const };
  }
  return { condition: "Preparing release", explanation: "This release is still progressing through its safety checks.", nextAction: "Wait for development validation to finish.", tone: "running" as const };
}

export function OperatorPanel() {
  const [moduleCatalog, setModuleCatalog] = useState<ProvisioningModuleCatalog | null>(null);
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
  const [lastRefreshedAt, setLastRefreshedAt] = useState("");
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
  const [selectedProvisionModuleIds, setSelectedProvisionModuleIds] = useState<string[]>([]);
  const [selectedProvisionVersion, setSelectedProvisionVersion] = useState("");
  const [provisionRing, setProvisionRing] = useState("manual");
  const [provisionAccountId, setProvisionAccountId] = useState("");
  const [provisionCallbackUrl] = useState(() =>
    typeof window === "undefined" ? "" : `${window.location.origin}/api/provisioning/runs/{run_id}/callback`,
  );
  const [provisionBrandTheme, setProvisionBrandTheme] = useState<BrandThemeInput>(DEFAULT_BRAND_THEME);


  const customerStats = useMemo(() => {
    const deployed = customers.filter((row) => row.deployment).length;
    const healthy = customers.filter((row) => row.readiness === "healthy").length;
    const attention = customers.filter((row) => ["backup_failed", "health_failed", "rollout_failed"].includes(row.readiness)).length;
    const activeKeys = customers.reduce((total, row) => total + activeCount(row.service_keys), 0);
    return { activeKeys, attention, deployed, healthy };
  }, [customers]);

  const rolloutCount = useMemo(
    () => deployments.reduce((total, row) => total + row.rollouts.length, 0),
    [deployments],
  );

  const selectedProvisionModules = useMemo<ProvisioningModule[]>(() => {
    if (!moduleCatalog) {
      return [];
    }
    return [
      moduleCatalog.core,
      ...moduleCatalog.optional_modules.filter((module) => selectedProvisionModuleIds.includes(module.id)),
    ];
  }, [moduleCatalog, selectedProvisionModuleIds]);

  const requiredProvisionServiceIds = useMemo(
    () => [...new Set(selectedProvisionModules.flatMap((module) => module.modules))],
    [selectedProvisionModules],
  );

  const eligibleProvisionReleases = useMemo(() => {
    if (!moduleCatalog) {
      return [];
    }
    return customerApprovedReleases(releases)
      .filter((release) => requiredProvisionServiceIds.every((moduleId) => Boolean(release.images?.[moduleId])));
  }, [moduleCatalog, releases, requiredProvisionServiceIds]);

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
      const [nextModuleCatalog, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns, nextGate] = await Promise.all([
        getProvisioningModuleCatalog(),
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

      setModuleCatalog(nextModuleCatalog);
      setCustomers(nextCustomers);
      setDeployments(nextRows);
      setReleases(newestFirst(nextReleases));
      setProvisioningRuns(nextProvisioningRuns);
      setDevelopmentGate(nextGate);
      setLastRefreshedAt(new Date().toISOString());
      setSelectedProvisionModuleIds((current) => current.filter((moduleId) =>
        nextModuleCatalog.optional_modules.some((module) => module.id === moduleId),
      ));
      setTargetReleaseByDeployment((current) => {
        const approved = customerApprovedReleases(nextReleases);
        const next: Record<string, string> = {};
        for (const row of nextRows) {
          const selected = current[row.deployment.id];
          next[row.deployment.id] = selected && approved.some((release) => release.version === selected)
            ? selected
            : approved[0]?.version ?? "";
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
        const [nextModuleCatalog, nextCustomers, nextDeployments, nextReleases, nextProvisioningRuns, nextGate] = await Promise.all([
          getProvisioningModuleCatalog(),
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
        setModuleCatalog(nextModuleCatalog);
        setCustomers(nextCustomers);
        setDeployments(nextRows);
        setReleases(newestFirst(nextReleases));
        setProvisioningRuns(nextProvisioningRuns);
        setDevelopmentGate(nextGate);
        setLastRefreshedAt(new Date().toISOString());
        setSelectedProvisionModuleIds((current) => current.filter((moduleId) =>
          nextModuleCatalog.optional_modules.some((module) => module.id === moduleId),
        ));
        const approved = customerApprovedReleases(nextReleases);
        setTargetReleaseByDeployment(Object.fromEntries(nextRows.map((row) => [
          row.deployment.id,
          approved[0]?.version ?? "",
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
    if (!provisionName.trim() || !provisionOwnerEmail.trim() || !moduleCatalog
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
        module_ids: selectedProvisionModuleIds,
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
    const targetVersion = targetReleaseByDeployment[row.deployment.id] || approvedCustomerReleases[0]?.version || "";
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
    { id: "rollouts", label: "Rollouts", meta: rolloutCount },
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
            {lastRefreshedAt ? <Timestamp label="Last refreshed" value={lastRefreshedAt} /> : <span className="scopePill">Loading status</span>}
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
            count={moduleCatalog?.optional_modules.length ?? 0}
          >
            <p className="operatorMuted">OneBrain Core is included for every customer. Choose only the optional product modules needed for this customer.</p>
            {showProvisionForm ? (
              <form className="operatorForm compactWorkflow" onSubmit={(event) => void onProvision(event)}>
                <TextField label="Customer name" required value={provisionName} onChange={setProvisionName} />
                <TextField label="Owner email" required type="email" value={provisionOwnerEmail} onChange={setProvisionOwnerEmail} />
                <ModuleSelection
                  catalog={moduleCatalog}
                  onChange={setSelectedProvisionModuleIds}
                  selectedIds={selectedProvisionModuleIds}
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
                  <ReleaseRingField value={provisionRing} onChange={setProvisionRing} />
                  <ReadOnlyField label="Deployment type" value="Dedicated Hetzner server" />
                  <ReadOnlyField label="Region" value="Nuremberg (nbg1)" />
                </div>
                {eligibleProvisionReleases.length === 0 ? (
                  <p className="mutedLine">No deployable release. Approve a release with images for the selected services.</p>
                ) : null}
                <TextField label="Optional account id" value={provisionAccountId} onChange={setProvisionAccountId} />
                <BrandThemeEditor value={provisionBrandTheme} onChange={setProvisionBrandTheme} />
                <button
                  className="primaryButton"
                  disabled={!provisionName.trim() || !provisionOwnerEmail.trim() || !moduleCatalog
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
        <Panel eyebrow="Rollouts" title="Customer rollouts" count={rolloutCount}>
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

function ModuleSelection({
  catalog,
  onChange,
  selectedIds,
}: {
  catalog: ProvisioningModuleCatalog | null;
  onChange: (moduleIds: string[]) => void;
  selectedIds: string[];
}) {
  if (!catalog) {
    return <p className="mutedLine">Loading the available modules…</p>;
  }

  function toggle(moduleId: string, selected: boolean) {
    onChange(selected
      ? [...selectedIds, moduleId]
      : selectedIds.filter((current) => current !== moduleId));
  }

  return (
    <fieldset className="moduleSelection">
      <legend>Modules</legend>
      <label className="moduleChoice coreModuleChoice">
        <input checked disabled type="checkbox" />
        <span>
          <strong>{catalog.core.label}</strong>
          <small>{catalog.core.description}</small>
        </span>
        <em>Required</em>
      </label>
      {catalog.optional_modules.map((module) => (
        <label className="moduleChoice" key={module.id}>
          <input
            checked={selectedIds.includes(module.id)}
            type="checkbox"
            onChange={(event) => toggle(module.id, event.target.checked)}
          />
          <span>
            <strong>{module.label}</strong>
            <small>{module.description}</small>
          </span>
          {module.modules.length ? <em>{module.modules.length} service{module.modules.length === 1 ? "" : "s"}</em> : <em>Core service</em>}
        </label>
      ))}
    </fieldset>
  );
}

function ReleaseRingField({ onChange, value }: { onChange: (value: string) => void; value: string }) {
  return (
    <label className="field releaseRingField">
      <span className="fieldLabel">Release ring <button aria-label={RELEASE_RING_HELP[value] || "Release ring guidance"} className="infoHint" title={RELEASE_RING_HELP[value]} type="button">i</button></span>
      <select className="select" value={value} onChange={(event) => onChange(event.target.value)}>
        {RELEASE_RINGS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select>
      <small>{RELEASE_RING_HELP[value]}</small>
    </label>
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
            <ExpandableCard
              className="provisioningRunCard"
              key={run.id}
              summary={(
                <StatusSummary
                  status={describeOperationalStatus(run.status)}
                  updatedAt={run.updated_at || run.created_at}
                  updatedLabel="Last updated"
                >
                  <span>{run.module_ids.length ? `${run.module_ids.length} optional module${run.module_ids.length === 1 ? "" : "s"}` : "Core only"}</span>
                </StatusSummary>
              )}
              title={(
                <div>
                  <strong>{run.account_id}</strong>
                  <span>{run.deployment_id}</span>
                </div>
              )}
            >
              <div className="operatorMeta">
                <span>Selected: {run.module_ids.length ? run.module_ids.map(labelFor).join(", ") : "OneBrain Core only"}</span>
                {run.target_id ? <span>{[run.target_id, run.target_environment].filter(Boolean).join(" / ")}</span> : null}
                {run.external_run_url ? <a href={run.external_run_url} rel="noreferrer" target="_blank">Open deployment</a> : null}
                {run.smoke_status ? <span>Smoke check: {labelFor(run.smoke_status)}</span> : null}
              </div>
              <Timestamp label="Created" value={run.created_at} />
              {Object.entries(run.service_urls || {}).length ? (
                <div className="operatorMeta">
                  {Object.entries(run.service_urls).map(([label, url]) => (
                    url.startsWith("http") ? <a href={url} key={label} rel="noreferrer" target="_blank">{labelFor(label)}</a> : <span key={label}>{labelFor(label)}</span>
                  ))}
                </div>
              ) : null}
              {pendingModules.length ? <p className="operatorMuted">Waiting for images: {pendingModules.map(labelFor).join(", ")}.</p> : null}
              {run.failure_reason ? <p className="promotionFailure">{run.failure_reason}</p> : null}
              {bootstrapSecrets[run.id] ? <code className="credentialSecret">{bootstrapSecrets[run.id]}</code> : null}
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
                  {busyAction === "secret" && busyId === run.id ? "Reading" : "Show one-time secret"}
                </button>
              </div>
            </ExpandableCard>
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
  const updatedAt = latestRecordedAt(
    customer.latest_rollout?.completed_at,
    customer.latest_rollout?.dispatched_at,
    customer.latest_rollout?.created_at,
    customer.health?.created_at,
    customer.backup?.created_at,
    deployment?.last_heartbeat_at,
  );
  return (
    <ExpandableCard
      className="customerCard"
      summary={(
        <StatusSummary status={describeOperationalStatus(customer.readiness)} updatedAt={updatedAt} updatedLabel="Latest signal">
          <span>{deployment?.current_version ? `Version ${deployment.current_version}` : "No version installed"}</span>
          <span>{activeCount(customer.service_keys)} active key{activeCount(customer.service_keys) === 1 ? "" : "s"}</span>
        </StatusSummary>
      )}
      title={(
        <div className="operatorRowTitle">
          <strong>{customer.account.name}</strong>
          <code>{customer.account.id}</code>
        </div>
      )}
    >
      <div className="operatorRowMain">
        <p className="operatorMuted">
          {deployment ? `${labelFor(deployment.release_ring)} ring on ${labelFor(deployment.deployment_type)}` : `${labelFor(customer.account.kind)} account — not deployed yet`}
        </p>
        <div className="timestampRail">
          <Timestamp label="Version active since" value={deployment?.current_version_deployed_at} />
          <Timestamp label="Last backup" value={customer.backup?.created_at} />
          <Timestamp label="Last health check" value={customer.health?.created_at} />
          <Timestamp label="Last rollout" value={customer.latest_rollout?.completed_at || customer.latest_rollout?.created_at} />
        </div>
        <div className="pillRail">
          <span>{customer.spaces.length} spaces</span>
          <span>{customer.apps.length} apps</span>
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
    </ExpandableCard>
  );
}

function latestRecordedAt(...values: Array<string | null | undefined>): string {
  return values
    .filter((value): value is string => typeof value === "string" && !Number.isNaN(Date.parse(value)))
    .sort((left, right) => Date.parse(right) - Date.parse(left))[0] || "";
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
          <StatusSummary
            status={describeOperationalStatus(gate?.ready ? "healthy" : "blocked")}
            updatedAt={deployment.last_heartbeat_at}
            updatedLabel="Last development report"
          />
          <dl className="gateFacts">
            <div><dt>Installed version</dt><dd>{deployment.current_version || "not installed"}</dd></div>
            <div><dt>Health</dt><dd>{deployment.last_heartbeat_healthy ? "healthy" : "not healthy"}</dd></div>
          </dl>
          <div className="timestampRail">
            <Timestamp label="Version active since" value={deployment.current_version_deployed_at} />
            <Timestamp label="Last seen" value={deployment.last_heartbeat_at} />
          </div>
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
                <h3>{release.version}</h3>
                <Timestamp label="Released" value={release.created_at} />
              </div>
              <span className={`statusPill ${state === "customer_approved" ? "success" : state.includes("failed") || state === "yanked" ? "failed" : "running"}`}>
                {labelFor(state)}
              </span>
            </div>
            <StatusSummary
              status={promotionStatus(state)}
              updatedAt={promotion?.customer_approved_at || promotion?.dev_verified_at || promotion?.dev_completed_at || release.created_at}
              updatedLabel="Latest release change"
            />
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
                  <p key={event.id}><span>{labelFor(event.action)}</span><Timestamp label="Recorded" value={event.created_at} /></p>
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
  const latestRollouts = [...row.rollouts]
    .sort((left, right) => Date.parse(right.created_at || "") - Date.parse(left.created_at || ""))
    .slice(0, 3);
  const latestRollout = latestRollouts[0];
  const canStart = Boolean(plan?.allowed && Object.keys(plan.modules_to_update || {}).length);
  return (
    <ExpandableCard
      className="deploymentCard"
      summary={(
        <StatusSummary
          status={describeOperationalStatus(latestRollout?.status || (row.deployment.last_heartbeat_healthy ? "healthy" : "pending"))}
          updatedAt={latestRecordedAt(
            latestRollout?.completed_at,
            latestRollout?.dispatched_at,
            latestRollout?.created_at,
            row.health?.created_at,
            row.deployment.last_heartbeat_at,
          )}
          updatedLabel="Latest rollout signal"
        >
          <span>{row.deployment.current_version ? `Current version ${row.deployment.current_version}` : "No version installed"}</span>
          <span>{row.rollouts.length} rollout{row.rollouts.length === 1 ? "" : "s"} recorded</span>
        </StatusSummary>
      )}
      title={(
        <div className="operatorRowTitle">
          <strong>{row.deployment.customer_name}</strong>
          <code>{row.deployment.id}</code>
        </div>
      )}
    >
      <p className="operatorMuted">{labelFor(row.deployment.release_ring)} ring · {labelFor(row.deployment.deployment_type)}</p>
      <div className="timestampRail">
        <Timestamp label="Server created" value={row.deployment.created_at} />
        <Timestamp label="Version active since" value={row.deployment.current_version_deployed_at} />
        <Timestamp label="Last seen" value={row.deployment.last_heartbeat_at} />
        <Timestamp label="Last backup" value={row.backup?.created_at} />
      </div>
      {row.deployment.last_reported_version && row.deployment.last_reported_version !== row.deployment.current_version ? (
        <p className="promotionFailure">Version drift: the server reports {row.deployment.last_reported_version}; the approved version is {row.deployment.current_version}.</p>
      ) : null}
      <div className="pillRail">
        {row.modules.map((module) => <span key={module.module_id}>{module.module_id} {module.version}</span>)}
      </div>
      <div className="deploymentControls">
        <SelectField
          label="Target release"
          options={releases.map((release) => ({ label: releaseOptionLabel(release), value: release.version }))}
          value={targetRelease}
          onChange={(value) => onTargetReleaseChange(row.deployment.id, value)}
        />
        <div className="operatorButtonGrid">
          <button className="secondaryButton" disabled={!targetRelease || activeBusy} type="button" onClick={() => void onPlan(row)}>
            {busyAction === "plan" && busyId === row.deployment.id ? "Checking plan" : "Check plan"}
          </button>
          <button className="primaryButton" disabled={!canStart || activeBusy} type="button" onClick={() => void onStartRollout(row)}>
            {busyAction === "rollout" && busyId === row.deployment.id ? "Starting" : "Start rollout"}
          </button>
        </div>
      </div>
      {plan ? <PlanResult plan={plan} /> : null}
      <div className="rolloutList">
        {latestRollouts.length === 0 ? <p className="operatorMuted">No rollout has been requested for this customer.</p> : null}
        {latestRollouts.map((rollout) => (
          <article className="rolloutLine" key={rollout.id}>
            <div>
              <strong>{rollout.target_version}</strong>
              <p>{describeOperationalStatus(rollout.status).condition}</p>
              {rollout.failure_reason ? <p className="promotionFailure">{rollout.failure_reason}</p> : null}
            </div>
            <Timestamp label="Requested" value={rollout.created_at} />
            {rollout.external_run_url ? <a href={rollout.external_run_url} rel="noreferrer" target="_blank">Open run</a> : null}
          </article>
        ))}
      </div>
    </ExpandableCard>
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
