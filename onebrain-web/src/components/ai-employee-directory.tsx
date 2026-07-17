"use client";

import { ExpandableCard } from "@/components/operational/expandable-card";
import { StatusBadge } from "@/components/admin-ui";
import type { AiEmployee } from "@/lib/onebrain-types";

type AiEmployeeDirectoryProps = {
  emptyMessage?: string;
  employees: AiEmployee[];
};

function initials(name: string): string {
  return name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
}

function labelFor(value: string): string {
  return value.replaceAll("_", " ");
}

function statusTone(status: string): "danger" | "neutral" | "running" | "success" | "warning" {
  if (status === "active") return "success";
  if (["paused", "disabled"].includes(status)) return "warning";
  if (["failed", "blocked"].includes(status)) return "danger";
  return "neutral";
}

function DetailList({ items, label }: { items: string[] | undefined; label: string }) {
  if (!items?.length) return null;
  return (
    <section className="employeeDetailSection">
      <h3>{label}</h3>
      <ul className="employeeDetailList">
        {items.map((item) => <li key={item}>{item}</li>)}
      </ul>
    </section>
  );
}

function EmployeeDetails({ employee }: { employee: AiEmployee }) {
  const descriptiveDetails = [
    ["Personality", employee.personality.join(" · ")],
    ["Voice", employee.tone],
    ["Strengths", employee.strengths.join(" · ")],
    ["Watch-outs", employee.watch_outs.join(" · ")],
    ["Working style", employee.working_style],
  ].filter(([, value]) => Boolean(value));
  const technicalDetails = [
    ["Employee ID", employee.employee_id],
    ["Model", `${employee.model_provider} · ${employee.model.split("/").at(-1)}`],
    ["Character", `Version ${employee.character_version}`],
  ];

  return (
    <div className="employeeDetailGrid">
      <section className="employeeDetailSection employeeDetailDescription">
        <h3>What they do</h3>
        <p>{employee.biography}</p>
        {employee.approval_rule ? <div className="employeeApprovalRule"><span>Approval rule</span><strong>{employee.approval_rule}</strong></div> : null}
      </section>
      <div className="employeeDetailStack">
        <DetailList items={employee.safe_actions} label="Safe actions" />
        <DetailList items={employee.never_without_approval} label="Never without approval" />
        <DetailList items={employee.productivity_metrics} label="Productivity signals" />
      </div>
      <section className="employeeDetailSection">
        <h3>Character</h3>
        <dl className="employeeDetailFacts">
          {descriptiveDetails.map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      </section>
      <section className="employeeDetailSection employeeTechnicalDetails">
        <h3>Technical details</h3>
        <dl className="employeeDetailFacts">
          {technicalDetails.map(([label, value]) => (
            <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
          ))}
        </dl>
      </section>
    </div>
  );
}

/** Canonical employee directory. Details stay opt-in so the whole team remains scannable. */
export function AiEmployeeDirectory({ emptyMessage = "No AI employees are available in this workspace.", employees }: AiEmployeeDirectoryProps) {
  if (!employees.length) return <p className="aiDirectoryEmpty">{emptyMessage}</p>;

  return (
    <div className="aiEmployeeDirectory" aria-label="AI employee directory">
      {employees.map((rawEmployee) => {
        const employee = rawEmployee;
        const mode = labelFor(employee.default_mode || "not configured");
        const department = employee.department || "Unassigned";

        return (
          <ExpandableCard
            className="employeeDirectoryCard"
            key={employee.employee_id}
            summary={(
              <div className="employeeDirectorySummary">
                <span>{department}</span>
                <span>{mode}</span>
                <StatusBadge tone={statusTone(employee.status)}>{labelFor(employee.status)}</StatusBadge>
              </div>
            )}
            title={(
              <div className="employeeDirectoryIdentity">
                <span className="employeeDirectoryAvatar" aria-hidden="true">{initials(employee.name)}</span>
                <div>
                  <strong>{employee.name}</strong>
                  <span>{employee.role}</span>
                </div>
              </div>
            )}
          >
            <EmployeeDetails employee={employee} />
          </ExpandableCard>
        );
      })}
    </div>
  );
}
