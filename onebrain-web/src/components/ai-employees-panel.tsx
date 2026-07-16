"use client";

import { MetricStrip, PageHeader, Panel, StatusBadge } from "@/components/admin-ui";

const employees = [
  { department: "Finance", mode: "Draft", name: "Mira Vale", owner: "Finance owner", role: "Finance Manager", status: "ready" },
  { department: "People", mode: "Draft", name: "Noah Mercer", owner: "HR owner", role: "HR Manager", status: "ready" },
  { department: "Product", mode: "Suggest", name: "Aiko Tan", owner: "Product lead", role: "Product Manager", status: "ready" },
  { department: "Engineering", mode: "Suggest", name: "Elias Frost", owner: "Engineering owner", role: "Software Architect", status: "ready" },
  { department: "Marketing", mode: "Draft", name: "Sofia Reyes", owner: "Marketing owner", role: "Marketing Strategy Manager", status: "ready" },
  { department: "Marketing", mode: "Approval queue", name: "Kai Morgan", owner: "Social owner", role: "Social Media Manager", status: "guarded" },
  { department: "Operations", mode: "Draft", name: "Priya Nair", owner: "Operations owner", role: "Operations Manager", status: "ready" },
  { department: "Customer Success", mode: "Draft", name: "Owen Blake", owner: "Account owner", role: "Customer Success Manager", status: "ready" },
];

const approvalQueue = [
  "External sends, publishing, payments, HR decisions, access changes, exports, deletes, and infrastructure changes always require human approval.",
  "Approval cards must show source records, confidence, risk, exact payload preview, payload hash, expiry, and required approver role.",
  "Payload changes invalidate old approvals and create a new proposal.",
];

const dataQuality = [
  "Unknown departments",
  "Missing actionability",
  "Low confidence extraction",
  "Duplicate or stale facts",
  "Restricted data in the wrong workflow",
];

const securitySignals = [
  "Blocked proposals",
  "High-risk proposals",
  "Approval bypass attempts",
  "Payload hash mismatches",
  "Expired approvals",
];

const productivitySignals = [
  "Drafts prepared",
  "Risks flagged",
  "Accepted suggestions",
  "Time-to-approval",
  "Data-quality fixes",
];

function toneForStatus(status: string): "running" | "success" {
  return status === "guarded" ? "running" : "success";
}

export function AiEmployeesPanel() {
  return (
    <div className="cockpitWorkspace">
      <PageHeader
        eyebrow="Deployable module"
        title="AI Employees"
        meta={(
          <>
            <StatusBadge tone="success">ai_employees</StatusBadge>
            <StatusBadge tone="running">draft-first</StatusBadge>
            <StatusBadge tone="success">human approved</StatusBadge>
          </>
        )}
      />

      <section className="moduleHero" aria-label="AI Employees control center summary">
        <div>
          <span className="eyebrow">Control center</span>
          <strong>Governed proactive assistants, not autonomous actors.</strong>
          <p>Use this module to enable, pause, review, and measure the eight company AI employees. They can prepare drafts, flag risks, and propose work while humans approve sensitive execution.</p>
        </div>
        <ul>
          <li>Selectable per customer deployment</li>
          <li>Default mode is draft-only or suggest-only</li>
          <li>No autonomous external credentials yet</li>
          <li>Proposal execution must be payload-bound and audited</li>
        </ul>
      </section>

      <MetricStrip
        metrics={[
          { label: "employees", value: employees.length },
          { label: "default", value: "draft" },
          { label: "approval", value: "human" },
          { label: "external execution", tone: "warning", value: "blocked" },
          { label: "data quality", value: dataQuality.length },
          { label: "security signals", value: securitySignals.length },
        ]}
      />

      <section className="cockpitGrid" aria-label="AI Employees operations">
        <Panel eyebrow="Roster" title="Employees" count={employees.length}>
          <div className="aiEmployeeTable">
            {employees.map((employee) => (
              <article key={employee.name}>
                <div>
                  <strong>{employee.name}</strong>
                  <span>{employee.role}</span>
                </div>
                <span>{employee.department}</span>
                <span>{employee.mode}</span>
                <span>{employee.owner}</span>
                <StatusBadge tone={toneForStatus(employee.status)}>{employee.status}</StatusBadge>
              </article>
            ))}
          </div>
        </Panel>

        <Panel eyebrow="Approvals" title="Human review queue">
          <ul className="moduleChecklist">
            {approvalQueue.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </Panel>

        <Panel eyebrow="Data quality" title="Agent-ready structure" count={dataQuality.length}>
          <ul className="moduleChecklist">
            {dataQuality.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </Panel>

        <Panel eyebrow="Security" title="Signals to monitor" count={securitySignals.length}>
          <ul className="moduleChecklist">
            {securitySignals.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </Panel>

        <Panel eyebrow="Productivity" title="Signals to prove value" count={productivitySignals.length}>
          <ul className="moduleChecklist">
            {productivitySignals.map((item) => <li key={item}>{item}</li>)}
          </ul>
        </Panel>
      </section>
    </div>
  );
}
