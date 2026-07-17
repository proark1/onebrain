"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AiEmployee, AiEmployeeTeam } from "@/lib/onebrain-types";

const POD_LABELS: Record<string, { name: string; remit: string; mark: string }> = {
  operations_corporate: {
    name: "Operations & corporate",
    remit: "Operations, finance, legal, and people",
    mark: "OPS",
  },
  product_technology_security: {
    name: "Product, technology & security",
    remit: "Product direction, delivery, and controls",
    mark: "PT",
  },
  market_customer: {
    name: "Market & customer",
    remit: "Growth, revenue, and customer outcomes",
    mark: "GTM",
  },
};

const COUNTRY_CODE: Record<string, string> = { France: "FR", Germany: "DE", "United Kingdom": "UK" };

function initials(name: string): string {
  return name.split(/\s+/).map((part) => part[0]).join("").slice(0, 2).toUpperCase();
}

function EmployeeCard({ employee, onOpen }: { employee: AiEmployee; onOpen: (trigger: HTMLButtonElement) => void }) {
  return (
    <button
      className="aiOrgPerson"
      data-country={COUNTRY_CODE[employee.country] || "EU"}
      onClick={(event) => onOpen(event.currentTarget)}
      type="button"
    >
      <span className="aiPersonAvatar" aria-hidden="true">{initials(employee.name)}</span>
      <span className="aiPersonIdentity">
        <strong>{employee.name}</strong>
        <span>{employee.role}</span>
      </span>
      <span className={`aiPresence ${employee.status}`}>{employee.status}</span>
    </button>
  );
}

export function AiEmployeeOrganization({ team }: { team: AiEmployeeTeam }) {
  const [selectedId, setSelectedId] = useState("");
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLButtonElement | null>(null);
  const agentById = useMemo(
    () => new Map(team.agents.map((agent) => [agent.employee_id, agent])),
    [team.agents],
  );
  const selected = selectedId ? agentById.get(selectedId) ?? null : null;
  const office = (team.pods.chief_of_staff_office ?? [])
    .map((id) => agentById.get(id))
    .filter((employee): employee is AiEmployee => Boolean(employee));
  const chief = office.find((employee) => !employee.reports_to) ?? office[0] ?? null;
  const advisors = office.filter((employee) => employee.employee_id !== chief?.employee_id);
  const operatingPods = Object.entries(team.pods).filter(([pod]) => pod !== "chief_of_staff_office");

  function openProfile(employeeId: string, trigger: HTMLButtonElement) {
    returnFocusRef.current = trigger;
    setSelectedId(employeeId);
  }

  const closeProfile = useCallback(() => {
    setSelectedId("");
    window.requestAnimationFrame(() => returnFocusRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!selected) return;
    closeButtonRef.current?.focus();
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") closeProfile();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [closeProfile, selected]);

  return (
    <section className="aiOrganization" aria-label="AI employee organization">
      <header className="aiSectionLead">
        <div>
          <span className="eyebrow">Organization</span>
          <h2>One office. Three accountable pods.</h2>
        </div>
        <p>Select an employee for role, reporting line, character, and model details.</p>
      </header>

      <div className="aiOrganizationMap">
        <div className="aiOrgRoot">
          <div className="aiOrgRootLabel"><span>COS</span><strong>Chief of Staff office</strong></div>
          {chief ? (
            <EmployeeCard employee={chief} onOpen={(trigger) => openProfile(chief.employee_id, trigger)} />
          ) : null}
          {advisors.map((employee) => (
            <EmployeeCard
              employee={employee}
              key={employee.employee_id}
              onOpen={(trigger) => openProfile(employee.employee_id, trigger)}
            />
          ))}
        </div>

        <div className="aiPodGrid">
          {operatingPods.map(([pod, ids]) => {
            const label = POD_LABELS[pod] ?? { name: pod, remit: "Specialist pod", mark: "POD" };
            return (
              <article className="aiOrgPod" key={pod}>
                <header>
                  <span>{label.mark}</span>
                  <div><h3>{label.name}</h3><p>{label.remit}</p></div>
                  <small>{ids.length}/5</small>
                </header>
                <div>
                  {ids.map((id) => {
                    const employee = agentById.get(id);
                    return employee ? (
                      <EmployeeCard employee={employee} key={id} onOpen={(trigger) => openProfile(id, trigger)} />
                    ) : null;
                  })}
                </div>
              </article>
            );
          })}
        </div>
      </div>

      {selected ? (
        <div className="aiProfileScrim" onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeProfile();
        }} role="presentation">
          <aside aria-label={`${selected.name} profile`} aria-modal="true" className="aiProfileSheet" role="dialog">
            <button aria-label="Close profile" className="aiSheetClose" onClick={closeProfile} ref={closeButtonRef} type="button">×</button>
            <div className="aiProfileHeading">
              <span className="aiProfileMonogram">{initials(selected.name)}</span>
              <div>
                <span className="eyebrow">{selected.country} · age {selected.fictional_age}</span>
                <h2>{selected.name}</h2>
                <p>{selected.role}</p>
              </div>
            </div>
            <div className="aiProfileFacts">
              <div><span>Reports to</span><strong>{agentById.get(selected.reports_to)?.name || "Human project admin"}</strong></div>
              <div><span>Mode</span><strong>{selected.default_mode.replaceAll("_", " ")}</strong></div>
              <div><span>Model</span><strong>{selected.model_provider} · {selected.model.split("/").at(-1)}</strong></div>
              <div><span>Character</span><strong>Version {selected.character_version}</strong></div>
            </div>
            <p className="aiProfileBio">{selected.biography}</p>
            <dl className="aiCharacterNotes">
              <div><dt>Personality</dt><dd>{selected.personality.join(" · ")}</dd></div>
              <div><dt>Voice</dt><dd>{selected.tone}</dd></div>
              <div><dt>Strengths</dt><dd>{selected.strengths.join(" · ")}</dd></div>
              <div><dt>Watch-outs</dt><dd>{selected.watch_outs.join(" · ")}</dd></div>
              <div><dt>Working style</dt><dd>{selected.working_style}</dd></div>
            </dl>
          </aside>
        </div>
      ) : null}
    </section>
  );
}
