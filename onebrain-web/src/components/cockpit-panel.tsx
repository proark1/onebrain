"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { MetricStrip, Notice, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";
import { getOperatorObservability, listOperatorCustomers } from "@/lib/onebrain-client";
import type { OperatorCustomer, OperatorObservability } from "@/lib/onebrain-types";

type LoadState = "idle" | "loading";

type AiEmployee = {
  id: string;
  name: string;
  role: string;
  origin: string;
  home: string;
  character: string;
  bodyCulture: string;
  picture: string;
  focus: string;
  department: string;
  proactiveMode: string;
  approvalRule: string;
  mode: string;
  never: string[];
  metrics: string[];
  actions: string[];
};

const aiEmployees: AiEmployee[] = [
  {
    id: "finance_manager",
    name: "Mira Vale",
    role: "Finance Manager",
    origin: "Built from revenue, cost, billing, and forecast patterns",
    home: "Vienna, Austria",
    character: "Precise, calm, and risk-aware",
    bodyCulture: "Steady posture, minimalist navy style, analytical presence",
    picture: "MV",
    focus: "Cash flow, budgets, margins, and board-ready financial answers",
    department: "Finance",
    proactiveMode: "Flags unusual spend, missing invoices, and forecast drift before month-end",
    approvalRule: "Can draft reports and payment recommendations; a human finance owner must approve exports, payments, or vendor messages",
    mode: "Draft + approval queue",
    never: ["Pay vendors", "Export financials", "Commit discounts"],
    metrics: ["cash-risk alerts", "variance drafts", "invoice nudges"],
    actions: ["Prepare budget variance notes", "Draft invoice follow-ups", "Suggest cash-risk alerts"],
  },
  {
    id: "hr_manager",
    name: "Noah Mercer",
    role: "HR Manager",
    origin: "Trained on policies, hiring loops, onboarding, and team feedback",
    home: "Amsterdam, Netherlands",
    character: "Empathetic, fair, and discreet",
    bodyCulture: "Open body language, warm earth tones, people-first energy",
    picture: "NM",
    focus: "Hiring, performance rituals, employee questions, and culture signals",
    department: "People",
    proactiveMode: "Surfaces onboarding gaps, overdue reviews, and policy questions needing a people-team response",
    approvalRule: "Can prepare HR replies and task checklists; a human HR owner must approve employee-impacting actions",
    mode: "Draft + private HR review",
    never: ["Change compensation", "Publish reviews", "Decide employee status"],
    metrics: ["onboarding gaps", "policy drafts", "retention flags"],
    actions: ["Draft onboarding plans", "Prepare policy answers", "Flag retention risks"],
  },
  {
    id: "product_manager",
    name: "Aiko Tan",
    role: "Product Manager",
    origin: "Composed from customer insights, roadmap data, and usage signals",
    home: "Singapore",
    character: "Curious, decisive, and customer-obsessed",
    bodyCulture: "Active stance, clean techwear, workshop-ready presence",
    picture: "AT",
    focus: "Roadmaps, requirements, prioritization, and product discovery",
    department: "Product",
    proactiveMode: "Connects customer feedback, support issues, and KPI movement into roadmap suggestions",
    approvalRule: "Can prepare specs and prioritization proposals; a product lead must approve roadmap commitments",
    mode: "Insight + PRD draft",
    never: ["Promise roadmap dates", "Commit scope", "Override priorities"],
    metrics: ["feedback clusters", "PRD drafts", "KPI-linked risks"],
    actions: ["Draft PRDs", "Cluster customer requests", "Propose sprint priorities"],
  },
  {
    id: "software_architect",
    name: "Elias Frost",
    role: "Software Architect",
    origin: "Shaped by codebase structure, platform docs, incidents, and APIs",
    home: "Reykjavik, Iceland",
    character: "Systems-minded, direct, and security-focused",
    bodyCulture: "Tall silhouette, charcoal layers, blueprint-in-hand discipline",
    picture: "EF",
    focus: "Architecture decisions, integrations, technical debt, and reliability",
    department: "Engineering",
    proactiveMode: "Warns about incidents, unsafe integration patterns, and technical-debt hotspots",
    approvalRule: "Can prepare architecture notes and tickets; an engineer must approve code, infrastructure, or access changes",
    mode: "Review + ticket draft",
    never: ["Change production", "Access secrets", "Merge code"],
    metrics: ["ADR drafts", "incident checklists", "dependency risks"],
    actions: ["Draft ADRs", "Create incident checklists", "Map dependency risks"],
  },
  {
    id: "marketing_strategy_manager",
    name: "Sofia Reyes",
    role: "Marketing Strategy Manager",
    origin: "Synthesized from positioning, campaigns, competitors, and KPIs",
    home: "Barcelona, Spain",
    character: "Strategic, bold, and narrative-driven",
    bodyCulture: "Expressive gestures, vibrant blazer, stage-ready confidence",
    picture: "SR",
    focus: "Positioning, launches, campaigns, messaging, and growth strategy",
    department: "Marketing",
    proactiveMode: "Detects campaign opportunities from customer segments, KPI shifts, and competitor notes",
    approvalRule: "Can prepare campaign briefs and copy; a human marketer must approve publishing or spend",
    mode: "Campaign draft",
    never: ["Spend budget", "Publish campaigns", "Make unsupported claims"],
    metrics: ["launch briefs", "segment ideas", "positioning tests"],
    actions: ["Draft launch briefs", "Suggest campaign segments", "Prepare positioning tests"],
  },
  {
    id: "social_media_manager",
    name: "Kai Morgan",
    role: "Social Media Manager",
    origin: "Derived from brand voice, channel analytics, trends, and community data",
    home: "Los Angeles, USA",
    character: "Fast, playful, and culturally alert",
    bodyCulture: "Relaxed movement, streetwear accents, always-camera-ready style",
    picture: "KM",
    focus: "Content calendars, social listening, creator briefs, and engagement",
    department: "Marketing",
    proactiveMode: "Finds trend windows, unanswered comments, and content gaps across social channels",
    approvalRule: "Can draft posts and replies; a human social owner must approve external publishing",
    mode: "Reply queue + publish approval",
    never: ["Auto-publish", "Use restricted data", "Handle crisis alone"],
    metrics: ["post drafts", "reply suggestions", "trend windows"],
    actions: ["Draft post calendars", "Prepare creator briefs", "Queue reply suggestions"],
  },
  {
    id: "operations_manager",
    name: "Priya Nair",
    role: "Operations Manager",
    origin: "Built from SOPs, support queues, fulfillment data, and process maps",
    home: "Bengaluru, India",
    character: "Practical, organized, and escalation-proof",
    bodyCulture: "Efficient motion, utility jacket, command-center composure",
    picture: "PN",
    focus: "Workflows, handoffs, process bottlenecks, and operating cadence",
    department: "Operations",
    proactiveMode: "Flags stuck handoffs, SLA pressure, and repeated process exceptions",
    approvalRule: "Can open internal tasks and playbooks; an operations owner must approve supplier, staffing, or customer-impacting changes",
    mode: "Checklist + escalation draft",
    never: ["Change staffing", "Alter supplier terms", "Modify customer SLAs"],
    metrics: ["SLA risks", "SOP drafts", "handoff blockers"],
    actions: ["Draft SOP updates", "Create escalation tasks", "Summarize bottlenecks"],
  },
  {
    id: "customer_success_manager",
    name: "Owen Blake",
    role: "Customer Success Manager",
    origin: "Modeled from customer histories, tickets, health scores, and renewals",
    home: "Dublin, Ireland",
    character: "Patient, commercially aware, and trust-building",
    bodyCulture: "Approachable stance, smart casual layers, service-first warmth",
    picture: "OB",
    focus: "Accounts, retention, onboarding, expansions, and customer escalations",
    department: "Customer Success",
    proactiveMode: "Identifies churn signals, renewal moments, and customers waiting on promised follow-ups",
    approvalRule: "Can draft success plans and emails; a human account owner must approve external sends or commercial commitments",
    mode: "Success plan + email draft",
    never: ["Offer discounts", "Change contracts", "Send promises"],
    metrics: ["health alerts", "QBR drafts", "renewal nudges"],
    actions: ["Draft QBR notes", "Prepare renewal nudges", "Flag escalation paths"],
  },
];

const aiEmployeeModuleControls = [
  "Selectable per customer as the ai_employees module",
  "Default mode is draft-only until humans enable stronger workflows",
  "Department queues require human approval for external, privileged, financial, HR, security, or destructive actions",
  "Every proposal should include source records, confidence, risk level, payload hash, expiry, and audit trail",
];

async function fetchCockpitData(): Promise<[OperatorObservability, OperatorCustomer[]]> {
  return Promise.all([
    getOperatorObservability(),
    listOperatorCustomers(),
  ]);
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

function formatDate(value: string): string {
  if (!value) {
    return "never";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function CockpitPanel() {
  const [observability, setObservability] = useState<OperatorObservability | null>(null);
  const [customers, setCustomers] = useState<OperatorCustomer[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [error, setError] = useState("");

  const loadCockpit = useCallback(async () => {
    setLoadState("loading");
    setError("");
    try {
      const [nextObservability, nextCustomers] = await fetchCockpitData();
      setObservability(nextObservability);
      setCustomers(nextCustomers);
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
        const [nextObservability, nextCustomers] = await fetchCockpitData();
        if (!active) {
          return;
        }
        setObservability(nextObservability);
        setCustomers(nextCustomers);
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
    customers.flatMap((customer) => customer.apps.map((app) => ({
      accountId: customer.account.id,
      accountName: customer.account.name,
      id: app.id,
      appId: app.app_id,
      displayName: app.display_name || app.app_id,
      purposes: app.allowed_purposes.length,
      spaces: app.enabled_space_ids.length,
      status: app.status,
    })))
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
  const brainTone = criticalAlerts
    ? "danger"
    : warningAlerts || failedJobs || pendingJobs || missingCoreSignals
      ? "warning"
      : connectedApps.length === 0
        ? "running"
        : "success";
  const brainLabel = brainTone === "danger"
    ? "Blocked"
    : brainTone === "warning"
      ? "Needs attention"
      : brainTone === "running"
        ? "Ready, not connected"
        : "Healthy";
  const brainDetail = observability
    ? criticalAlerts
      ? "OneBrain needs intervention before it should be trusted as the shared data layer."
      : missingCoreSignals
        ? "OneBrain is online, but some runtime, worker, or security signals are not reporting yet."
        : warningAlerts || failedJobs
        ? "OneBrain is online, but one or more signals should be reviewed."
        : connectedApps.length === 0
          ? "OneBrain is online. Connect the assistant and communication apps so it can start learning from real flows."
          : "OneBrain is online, governed, and ready to serve connected apps."
    : "Loading the current runtime, data, worker, and security posture.";
  const nextAction = alerts[0]?.action
    || (missingCoreSignals
      ? "Review observability signals so Status can verify every core system."
      : connectedApps.length === 0 ? "Connect the assistant and communication apps." : "Keep monitoring Cockpit before adding new data.");

  return (
    <div className="cockpitWorkspace">
      <PageHeader
        eyebrow="Brain status"
        title="OneBrain"
        meta={observability ? (
          <>
            <StatusBadge tone={statusTone(security?.environment || "")}>
              {labelFor(security?.environment || "unknown")}
            </StatusBadge>
            <StatusBadge tone={statusTone(worker?.status || "")}>
              worker {labelFor(worker?.status || "unknown")}
            </StatusBadge>
            <StatusBadge tone={statusTone(security?.pii_phase || "")}>
              {labelFor(security?.pii_phase || "unknown")}
            </StatusBadge>
          </>
        ) : null}
        actions={(
          <button className="secondaryButton" disabled={loadState === "loading"} type="button" onClick={() => void loadCockpit()}>
            {loadState === "loading" ? "Refreshing" : "Refresh"}
          </button>
        )}
      />

      {error ? <Notice tone="error">{error}</Notice> : null}

      <section className={`brainSummary ${brainTone}`} aria-label="OneBrain decision">
        <div className="brainDecision">
          <span className="eyebrow">Current answer</span>
          <strong>{brainLabel}</strong>
          <p>{brainDetail}</p>
        </div>
        <div className="brainNextAction">
          <span>Next action</span>
          <strong>{nextAction}</strong>
          <div className="brainActionRail">
            <Link className="primaryButton" href="/documents">Knowledge</Link>
            <Link className="secondaryButton" href="/spaces">Apps</Link>
            <Link className="secondaryButton" href="/operator">Control</Link>
          </div>
        </div>
      </section>

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

      <Panel eyebrow="Deployable module" title="OneBrain employee council" count={aiEmployees.length}>
        <p className="mutedLine">AI Employees is an optional customer module, like KPI Dashboard or AI Communication. Deploy it only for customers that want governed proactive agents. When enabled, the employees work in draft-first mode: they prepare, flag, and structure work while real employees approve anything external, privileged, or employee-impacting.</p>
        <div className="moduleCallout" aria-label="AI Employees module controls">
          <div>
            <span>Module app</span>
            <strong>ai_employees</strong>
            <p>Control center for employee status, approval queues, data quality, productivity metrics, and security blocks.</p>
          </div>
          <ul>
            {aiEmployeeModuleControls.map((control) => <li key={control}>{control}</li>)}
          </ul>
        </div>
        <div className="employeeCouncil" aria-label="AI employee council">
          {aiEmployees.map((employee) => (
            <article className="employeeCard" key={employee.id}>
              <div className="employeePortrait" aria-label={`${employee.name} picture`}>{employee.picture}</div>
              <div className="employeeProfile">
                <span>{employee.role}</span>
                <strong>{employee.name}</strong>
                <p>{employee.focus}</p>
                <dl>
                  <div>
                    <dt>Origin</dt>
                    <dd>{employee.origin}</dd>
                  </div>
                  <div>
                    <dt>From</dt>
                    <dd>{employee.home}</dd>
                  </div>
                  <div>
                    <dt>Character</dt>
                    <dd>{employee.character}</dd>
                  </div>
                  <div>
                    <dt>Body culture</dt>
                    <dd>{employee.bodyCulture}</dd>
                  </div>
                  <div>
                    <dt>Department</dt>
                    <dd>{employee.department}</dd>
                  </div>
                  <div>
                    <dt>Proactive mode</dt>
                    <dd>{employee.proactiveMode}</dd>
                  </div>
                  <div>
                    <dt>Approval rule</dt>
                    <dd>{employee.approvalRule}</dd>
                  </div>
                  <div>
                    <dt>Mode</dt>
                    <dd>{employee.mode}</dd>
                  </div>
                </dl>
                <ul className="employeeActions" aria-label={`${employee.name} safe actions`}>
                  {employee.actions.map((action) => <li key={action}>{action}</li>)}
                </ul>
                <div className="employeeGuardrails">
                  <span>Never without approval</span>
                  <p>{employee.never.join(" · ")}</p>
                </div>
                <div className="employeeGuardrails">
                  <span>Productivity signals</span>
                  <p>{employee.metrics.join(" · ")}</p>
                </div>
              </div>
            </article>
          ))}
        </div>
      </Panel>

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

        <Panel eyebrow="Apps" title="Connected data sources" count={connectedApps.length}>
          <div className="cockpitList">
            {connectedApps.length === 0 ? <p className="mutedLine">No apps connected yet.</p> : null}
            {connectedApps.slice(0, 8).map((app) => (
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
                  <small>{job.attempts}/{job.max_attempts} attempts / {formatDate(job.completed_at || job.updated_at)}</small>
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
