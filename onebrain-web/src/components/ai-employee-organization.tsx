"use client";

import { AiEmployeeDirectory } from "@/components/ai-employee-directory";
import type { AiEmployeeTeam } from "@/lib/onebrain-types";

export function AiEmployeeOrganization({ team }: { team: AiEmployeeTeam }) {
  return (
    <section className="aiOrganization" aria-label="AI employee organization">
      <header className="aiSectionLead">
        <div>
          <span className="eyebrow">Employee directory</span>
          <h2>Everyone is visible. Details stay out of the way.</h2>
        </div>
        <p>Expand an employee only when you need their working rules, character notes, or technical details.</p>
      </header>
      <AiEmployeeDirectory employees={team.agents} />
    </section>
  );
}
