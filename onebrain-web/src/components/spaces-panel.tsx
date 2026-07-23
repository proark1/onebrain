"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import {
  checkPlatformAccess,
  createPlatformAccount,
  createPlatformSpace,
  installPlatformApp,
  listPlatformAccounts,
  listPlatformApps,
  listPlatformAudit,
  listPlatformSpaces,
} from "@/lib/onebrain-client";
import { MetricStrip, Notice, PageHeader, Panel, Tabs } from "@/components/admin-ui";
import { summaryValue, type LoadState } from "@/lib/platform-summary";
import type {
  PlatformAccessCheckResult,
  PlatformAccount,
  PlatformAppInstallation,
  PlatformAuditEvent,
  PlatformSpace,
} from "@/lib/onebrain-types";

const ACCOUNT_KINDS = [
  { label: "Organization", value: "organization" },
  { label: "Person", value: "person" },
  { label: "Family", value: "family" },
  { label: "Project", value: "project" },
];

const SPACE_KINDS = [
  { label: "Business", value: "business" },
  { label: "Customer service", value: "customer_service" },
  { label: "Shared", value: "shared" },
  { label: "Personal", value: "personal" },
  { label: "Family", value: "family" },
  { label: "Project", value: "project" },
];

const APP_IDS = [
  "onebrain_core",
  "assistant",
  "ai_employees",
  "communication",
  "kpi_dashboard",
  "buchhaltung",
  "admin_console",
  "workers",
];

const PURPOSES = [
  "assistant_context",
  "assistant_action",
  "ai_employee_read",
  "ai_employee_configure",
  "ai_employee_mission_run",
  "ai_employee_action_propose",
  "ai_employee_action_approve",
  "ai_employee_connector_manage",
  "ai_employee_action_execute",
  "customer_service_answer",
  "customer_service_inbox",
  "knowledge_management",
  "admin_management",
  "gdpr_export",
  "gdpr_delete",
  "analytics",
  "billing",
  "kpi_read",
  "kpi_configure",
  "kpi_snapshot_write",
  "accounting_read",
  "accounting_ingest",
  "accounting_configure",
  "accounting_export",
];

const APP_DEFAULT_PURPOSES: Record<string, string[]> = {
  onebrain_core: ["knowledge_management", "admin_management", "gdpr_export", "gdpr_delete"],
  assistant: ["assistant_context", "assistant_action"],
  ai_employees: [
    "ai_employee_read",
    "ai_employee_configure",
    "ai_employee_mission_run",
    "ai_employee_action_propose",
    "ai_employee_action_approve",
    "ai_employee_connector_manage",
    "ai_employee_action_execute",
  ],
  communication: ["customer_service_answer", "customer_service_inbox"],
  kpi_dashboard: ["kpi_read", "kpi_configure", "kpi_snapshot_write"],
  buchhaltung: ["accounting_read", "accounting_ingest", "accounting_configure", "accounting_export"],
  admin_console: ["admin_management"],
  workers: ["analytics"],
};

type BusyAction = "accounts" | "details" | "createAccount" | "createSpace" | "installApp" | "accessCheck" | "";
type SpaceTab = "overview" | "spaces" | "apps" | "policy" | "audit";

function labelFor(value: string): string {
  return value.replace(/_/g, " ");
}

function toggleValue(values: string[], value: string): string[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

function metaSummary(meta: Record<string, unknown>): string {
  const entries = Object.entries(meta);
  if (!entries.length) {
    return "";
  }
  return entries
    .slice(0, 3)
    .map(([key, value]) => `${labelFor(key)}: ${Array.isArray(value) ? value.join(", ") : String(value)}`)
    .join(" / ");
}

export function SpacesPanel() {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [spaces, setSpaces] = useState<PlatformSpace[]>([]);
  const [apps, setApps] = useState<PlatformAppInstallation[]>([]);
  const [audit, setAudit] = useState<PlatformAuditEvent[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [accountKind, setAccountKind] = useState("organization");
  const [accountName, setAccountName] = useState("");
  const [accountId, setAccountId] = useState("");
  const [spaceKind, setSpaceKind] = useState("business");
  const [spaceName, setSpaceName] = useState("");
  const [spaceId, setSpaceId] = useState("");
  const [installAppId, setInstallAppId] = useState("assistant");
  const [installDisplayName, setInstallDisplayName] = useState("");
  const [installId, setInstallId] = useState("");
  const [installSpaceIds, setInstallSpaceIds] = useState<string[]>([]);
  const [installPurposes, setInstallPurposes] = useState<string[]>(["assistant_context"]);
  const [checkAppId, setCheckAppId] = useState("assistant");
  const [checkSpaceId, setCheckSpaceId] = useState("");
  const [checkPurpose, setCheckPurpose] = useState("assistant_context");
  const [accessResult, setAccessResult] = useState<PlatformAccessCheckResult | null>(null);
  const [activeTab, setActiveTab] = useState<SpaceTab>("overview");
  const [showCreateAccount, setShowCreateAccount] = useState(false);
  const [showCreateSpace, setShowCreateSpace] = useState(false);
  const [showInstallApp, setShowInstallApp] = useState(false);
  const [busyAction, setBusyAction] = useState<BusyAction>("");
  const [accountsState, setAccountsState] = useState<LoadState>("loading");
  const [detailsState, setDetailsState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selectedAccount = useMemo(
    () => accounts.find((account) => account.id === selectedAccountId) ?? null,
    [accounts, selectedAccountId],
  );
  const spaceNameById = useMemo(() => new Map(spaces.map((space) => [space.id, space.name])), [spaces]);
  const loadingAccounts = busyAction === "accounts";
  const loadingDetails = busyAction === "details";

  const chooseAccount = useCallback((accountIdValue: string) => {
    setSelectedAccountId(accountIdValue);
    setSpaces([]);
    setApps([]);
    setAudit([]);
    setInstallSpaceIds([]);
    setCheckSpaceId("");
    setAccessResult(null);
    setNotice("");
    setError("");
    setDetailsState("loading");
  }, []);

  const loadAccounts = useCallback(async (preferredAccountId = selectedAccountId) => {
    setBusyAction("accounts");
    setAccountsState("loading");
    setError("");
    try {
      const nextAccounts = await listPlatformAccounts();
      setAccounts(nextAccounts);
      setAccountsState("ready");
      const nextSelectedAccountId = preferredAccountId && nextAccounts.some((account) => account.id === preferredAccountId)
        ? preferredAccountId
        : nextAccounts[0]?.id ?? "";
      chooseAccount(nextSelectedAccountId);
    } catch (err) {
      setAccounts([]);
      setAccountsState("error");
      chooseAccount("");
      setError(err instanceof Error ? err.message : "Could not load accounts.");
    } finally {
      setBusyAction("");
    }
  }, [chooseAccount, selectedAccountId]);

  const loadDetails = useCallback(async () => {
    if (!selectedAccountId) {
      return;
    }
    setBusyAction("details");
    setDetailsState("loading");
    setError("");
    try {
      const [nextSpaces, nextApps, nextAudit] = await Promise.all([
        listPlatformSpaces(selectedAccountId),
        listPlatformApps(selectedAccountId),
        listPlatformAudit(selectedAccountId),
      ]);
      setSpaces(nextSpaces);
      setApps(nextApps);
      setAudit(nextAudit);
      setDetailsState("ready");
      setInstallSpaceIds((current) => current.filter((space) => nextSpaces.some((item) => item.id === space)));
      setCheckSpaceId((current) => current && nextSpaces.some((space) => space.id === current)
        ? current
        : nextSpaces[0]?.id ?? "");
    } catch (err) {
      setDetailsState("error");
      setError(err instanceof Error ? err.message : "Could not load account details.");
    } finally {
      setBusyAction("");
    }
  }, [selectedAccountId]);

  useEffect(() => {
    let active = true;
    async function loadInitialAccounts() {
      setBusyAction("accounts");
      setAccountsState("loading");
      setError("");
      try {
        const nextAccounts = await listPlatformAccounts();
        if (!active) {
          return;
        }
        setAccounts(nextAccounts);
        setAccountsState("ready");
        chooseAccount(nextAccounts[0]?.id ?? "");
      } catch (err) {
        if (!active) {
          return;
        }
        setAccounts([]);
        setAccountsState("error");
        chooseAccount("");
        setError(err instanceof Error ? err.message : "Could not load accounts.");
      } finally {
        if (active) {
          setBusyAction("");
        }
      }
    }
    void loadInitialAccounts();
    return () => {
      active = false;
    };
  }, [chooseAccount]);

  useEffect(() => {
    let active = true;
    async function loadSelectedAccountDetails() {
      if (!selectedAccountId) {
        return;
      }
      setBusyAction("details");
      setDetailsState("loading");
      setError("");
      try {
        const [nextSpaces, nextApps, nextAudit] = await Promise.all([
          listPlatformSpaces(selectedAccountId),
          listPlatformApps(selectedAccountId),
          listPlatformAudit(selectedAccountId),
        ]);
        if (!active) {
          return;
        }
        setSpaces(nextSpaces);
        setApps(nextApps);
        setAudit(nextAudit);
        setDetailsState("ready");
        setInstallSpaceIds((current) => current.filter((space) => nextSpaces.some((item) => item.id === space)));
        setCheckSpaceId((current) => current && nextSpaces.some((space) => space.id === current)
          ? current
          : nextSpaces[0]?.id ?? "");
      } catch (err) {
        if (active) {
          setDetailsState("error");
          setError(err instanceof Error ? err.message : "Could not load account details.");
        }
      } finally {
        if (active) {
          setBusyAction("");
        }
      }
    }
    void loadSelectedAccountDetails();
    return () => {
      active = false;
    };
  }, [selectedAccountId]);

  async function onCreateAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!accountName.trim() || busyAction) {
      return;
    }
    setBusyAction("createAccount");
    setError("");
    setNotice("");
    try {
      const account = await createPlatformAccount({
        id: accountId,
        kind: accountKind,
        name: accountName,
      });
      setAccountId("");
      setAccountName("");
      setShowCreateAccount(false);
      setNotice(`${account.name} created.`);
      await loadAccounts(account.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create account.");
    } finally {
      setBusyAction("");
    }
  }

  async function onCreateSpace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedAccountId || !spaceName.trim() || busyAction) {
      return;
    }
    setBusyAction("createSpace");
    setError("");
    setNotice("");
    try {
      const space = await createPlatformSpace(selectedAccountId, {
        id: spaceId,
        kind: spaceKind,
        name: spaceName,
      });
      setSpaceId("");
      setSpaceName("");
      setShowCreateSpace(false);
      setNotice(`${space.name} created.`);
      await loadDetails();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create space.");
    } finally {
      setBusyAction("");
    }
  }

  async function onInstallApp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedAccountId || !installSpaceIds.length || !installPurposes.length || busyAction) {
      return;
    }
    setBusyAction("installApp");
    setError("");
    setNotice("");
    try {
      const app = await installPlatformApp(selectedAccountId, {
        id: installId,
        app_id: installAppId,
        display_name: installDisplayName,
        enabled_space_ids: installSpaceIds,
        allowed_purposes: installPurposes,
      });
      setInstallId("");
      setInstallDisplayName("");
      setShowInstallApp(false);
      setNotice(`${app.display_name || labelFor(app.app_id)} installed.`);
      await loadDetails();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not install app.");
    } finally {
      setBusyAction("");
    }
  }

  async function onAccessCheck(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedAccountId || !checkSpaceId || !checkAppId || !checkPurpose || busyAction) {
      return;
    }
    setBusyAction("accessCheck");
    setError("");
    setNotice("");
    try {
      const result = await checkPlatformAccess({
        account_id: selectedAccountId,
        app_id: checkAppId,
        space_id: checkSpaceId,
        purpose: checkPurpose,
      });
      setAccessResult(result);
      setNotice(result.allowed ? "Access allowed." : "Access denied.");
      await loadDetails();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not check access.");
    } finally {
      setBusyAction("");
    }
  }

  return (
    <div className="spacesWorkspace">
      <PageHeader
        actions={(
          <>
            <button className="secondaryButton" disabled={Boolean(busyAction)} type="button" onClick={() => void loadAccounts()}>
              {loadingAccounts || loadingDetails ? "Refreshing" : "Refresh"}
            </button>
            <button className="primaryButton" type="button" onClick={() => setShowCreateAccount(true)}>
              Create account
            </button>
          </>
        )}
        description="Connect accounts, spaces, and apps to the work they support."
        eyebrow="Connected apps"
        meta={selectedAccount ? <span className="scopePill"><span className="statusDot" />{selectedAccount.name}</span> : null}
        title="Accounts and app access"
      />

      {error ? <Notice tone="error">{error}</Notice> : null}
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      <MetricStrip
        metrics={[
          { label: "accounts", value: summaryValue(accountsState, accounts.length) },
          { label: "spaces", value: summaryValue(selectedAccountId ? detailsState : "loading", spaces.length) },
          { label: "apps", value: summaryValue(selectedAccountId ? detailsState : "loading", apps.length) },
          { label: "audit", value: summaryValue(selectedAccountId ? detailsState : "loading", audit.length) },
        ]}
      />

      <div className="spacesGrid">
        <Panel
          actions={<button className="secondaryButton" type="button" onClick={() => setShowCreateAccount((current) => !current)}>{showCreateAccount ? "Close" : "Create"}</button>}
          count={accounts.length}
          eyebrow="Accounts"
          title="Tenants"
        >
          <div className="accountList">
            {accountsState === "ready" && accounts.length === 0 ? (
              <div className="emptyPanel">
                <h3>No accounts</h3>
                <p>Create the first account to organize spaces and app access.</p>
              </div>
            ) : null}
            {accounts.map((account) => (
              <button
                aria-current={selectedAccountId === account.id ? "true" : undefined}
                className={selectedAccountId === account.id ? "accountItem active" : "accountItem"}
                disabled={Boolean(busyAction)}
                key={account.id}
                type="button"
                onClick={() => chooseAccount(account.id)}
              >
                <strong>{account.name}</strong>
                <span>{labelFor(account.kind)} / {account.status}</span>
                <small>{account.id}</small>
              </button>
            ))}
          </div>

          {showCreateAccount ? (
            <form className="spacesForm compactWorkflow" onSubmit={(event) => void onCreateAccount(event)}>
              <SelectField label="Kind" options={ACCOUNT_KINDS} value={accountKind} onChange={setAccountKind} />
              <TextField label="Name" value={accountName} onChange={setAccountName} />
              <TextField label="Optional id" value={accountId} onChange={setAccountId} />
              <button className="primaryButton" disabled={!accountName.trim() || Boolean(busyAction)} type="submit">
                {busyAction === "createAccount" ? "Creating" : "Create account"}
              </button>
            </form>
          ) : null}
        </Panel>

        <section className="spacesDetail" aria-label="Selected account details">
          <Tabs
            active={activeTab}
            items={[
              { id: "overview", label: "Overview" },
              { id: "spaces", label: "Spaces", meta: spaces.length },
              { id: "apps", label: "Apps", meta: apps.length },
              { id: "policy", label: "Policy" },
              { id: "audit", label: "Audit", meta: audit.length },
            ]}
            label="Account sections"
            onChange={setActiveTab}
          />

          {activeTab === "overview" ? (
            <Panel count={selectedAccount?.status || "none"} eyebrow="Selected account" title={selectedAccount?.name || "No account selected"}>
              <div className="overviewGrid">
                <div>
                  <strong>{spaces.length}</strong>
                  <span>Spaces</span>
                </div>
                <div>
                  <strong>{apps.length}</strong>
                  <span>Apps</span>
                </div>
                <div>
                  <strong>{audit.length}</strong>
                  <span>Audit events</span>
                </div>
              </div>
              <p className="operatorMuted">
                Use the tabs to manage spaces, app access, policy checks, and account audit events.
              </p>
            </Panel>
          ) : null}

          {activeTab === "spaces" ? (
            <Panel
              actions={<button className="secondaryButton" disabled={!selectedAccountId} type="button" onClick={() => setShowCreateSpace((current) => !current)}>{showCreateSpace ? "Close" : "Create space"}</button>}
              count={spaces.length}
              eyebrow="Selected account"
              title={selectedAccount?.name || "No account selected"}
            >
              <div className="spaceList">
                {spaces.length === 0 ? (
                  <div className="emptyPanel">
                    <h3>No spaces yet</h3>
                    <p>Create a space before installing apps or checking access.</p>
                  </div>
                ) : null}
                {spaces.map((space) => (
                  <article className="spaceRow" key={space.id}>
                    <div>
                      <strong>{space.name}</strong>
                      <span>{labelFor(space.kind)} / {space.status}</span>
                    </div>
                    <small>{space.id}</small>
                  </article>
                ))}
              </div>

              {showCreateSpace ? (
                <form className="spacesForm inlineForm compactWorkflow" onSubmit={(event) => void onCreateSpace(event)}>
                  <SelectField label="Space kind" options={SPACE_KINDS} value={spaceKind} onChange={setSpaceKind} />
                  <TextField label="Name" value={spaceName} onChange={setSpaceName} />
                  <TextField label="Optional id" value={spaceId} onChange={setSpaceId} />
                  <button className="primaryButton" disabled={!selectedAccountId || !spaceName.trim() || Boolean(busyAction)} type="submit">
                    {busyAction === "createSpace" ? "Creating" : "Create space"}
                  </button>
                </form>
              ) : null}
            </Panel>
          ) : null}

          {activeTab === "apps" ? (
            <Panel
              actions={<button className="secondaryButton" disabled={!selectedAccountId} type="button" onClick={() => setShowInstallApp((current) => !current)}>{showInstallApp ? "Close" : "Install app"}</button>}
              count={apps.length}
              eyebrow="Apps"
              title="Installations"
            >
              <div className="appList">
                {apps.length === 0 ? <p className="mutedLine">No apps installed for this account.</p> : null}
                {apps.map((app) => (
                  <article className="appRow" key={app.id}>
                    <div>
                      <strong>{app.display_name || labelFor(app.app_id)}</strong>
                      <span>{app.app_id} / {app.status}</span>
                    </div>
                    <div className="pillRail">
                      {app.enabled_space_ids.map((id) => <span key={id}>{spaceNameById.get(id) || id}</span>)}
                      {app.allowed_purposes.map((purpose) => <span key={purpose}>{labelFor(purpose)}</span>)}
                    </div>
                  </article>
                ))}
              </div>

              {showInstallApp ? (
                <form className="spacesForm compactWorkflow" onSubmit={(event) => void onInstallApp(event)}>
                  <SelectField
                    label="App"
                    options={APP_IDS.map((app) => ({ label: labelFor(app), value: app }))}
                    value={installAppId}
                    onChange={(value) => {
                      setInstallAppId(value);
                      setInstallPurposes(APP_DEFAULT_PURPOSES[value] ?? []);
                    }}
                  />
                  <TextField label="Display name" value={installDisplayName} onChange={setInstallDisplayName} />
                  <TextField label="Optional id" value={installId} onChange={setInstallId} />
                  <CheckboxGroup
                    label="Enabled spaces"
                    options={spaces.map((space) => ({ label: space.name, value: space.id }))}
                    values={installSpaceIds}
                    onToggle={(value) => setInstallSpaceIds((current) => toggleValue(current, value))}
                  />
                  <CheckboxGroup
                    label="Allowed purposes"
                    options={PURPOSES.map((purpose) => ({ label: labelFor(purpose), value: purpose }))}
                    values={installPurposes}
                    onToggle={(value) => setInstallPurposes((current) => toggleValue(current, value))}
                  />
                  <button
                    className="primaryButton"
                    disabled={!selectedAccountId || !installSpaceIds.length || !installPurposes.length || Boolean(busyAction)}
                    type="submit"
                  >
                    {busyAction === "installApp" ? "Installing" : "Install app"}
                  </button>
                </form>
              ) : null}
            </Panel>
          ) : null}

          {activeTab === "policy" ? (
            <Panel eyebrow="Policy" title="Access check">
              <form className="spacesForm" onSubmit={(event) => void onAccessCheck(event)}>
                <SelectField
                  label="App"
                  options={APP_IDS.map((app) => ({ label: labelFor(app), value: app }))}
                  value={checkAppId}
                  onChange={setCheckAppId}
                />
                <SelectField
                  label="Space"
                  options={spaces.map((space) => ({ label: space.name, value: space.id }))}
                  value={checkSpaceId}
                  onChange={setCheckSpaceId}
                />
                <SelectField
                  label="Purpose"
                  options={PURPOSES.map((purpose) => ({ label: labelFor(purpose), value: purpose }))}
                  value={checkPurpose}
                  onChange={setCheckPurpose}
                />
                <button className="primaryButton" disabled={!selectedAccountId || !checkSpaceId || Boolean(busyAction)} type="submit">
                  {busyAction === "accessCheck" ? "Checking" : "Check access"}
                </button>
              </form>
              {accessResult ? (
                <div className={accessResult.allowed ? "accessResult allowed" : "accessResult denied"}>
                  <strong>{accessResult.allowed ? "Allowed" : "Denied"}</strong>
                  <span>{labelFor(accessResult.reason)}</span>
                </div>
              ) : null}
            </Panel>
          ) : null}

          {activeTab === "audit" ? (
            <Panel count={audit.length} eyebrow="Audit" title="Account events">
            <div className="auditList">
              {audit.length === 0 ? <p className="mutedLine">No audit events for this account.</p> : null}
              {audit.map((event) => (
                <article className="auditRow" key={event.id}>
                  <div>
                    <strong>{labelFor(event.action)}</strong>
                    <span>{event.target_type}: {event.target_id}</span>
                    {metaSummary(event.meta) ? <small>{metaSummary(event.meta)}</small> : null}
                  </div>
                  <div className="auditMeta">
                    {event.decision ? <span>{event.decision}</span> : null}
                    {event.purpose ? <span>{labelFor(event.purpose)}</span> : null}
                    {event.app_id ? <span>{event.app_id}</span> : null}
                  </div>
                </article>
              ))}
            </div>
            </Panel>
          ) : null}
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

function CheckboxGroup({
  label,
  onToggle,
  options,
  values,
}: {
  label: string;
  onToggle: (value: string) => void;
  options: Array<{ label: string; value: string }>;
  values: string[];
}) {
  return (
    <fieldset className="choiceField">
      <legend>{label}</legend>
      <div className="choiceGrid">
        {options.length === 0 ? <p className="mutedLine">No choices available.</p> : null}
        {options.map((option) => (
          <label className="choiceItem" key={option.value}>
            <input
              checked={values.includes(option.value)}
              onChange={() => onToggle(option.value)}
              type="checkbox"
            />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
