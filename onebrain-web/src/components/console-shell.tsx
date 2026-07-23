import Link from "next/link";
import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { ConsoleNavigation } from "@/components/console-navigation";
import { WorkspaceProvider } from "@/components/workspace-provider";
import { WorkspaceSelector } from "@/components/workspace-selector";
import { ALL_NAV, consoleNavigationGroups, type ConsoleSection } from "@/lib/console-navigation";
import type { SessionInfo } from "@/lib/onebrain-types";

type ConsoleShellProps = {
  active: ConsoleSection;
  children: ReactNode;
  session: SessionInfo;
  workspaceMode?: "console" | "feature";
};

export function ConsoleShell({ active, children, session, workspaceMode = "console" }: ConsoleShellProps) {
  if (session.must_change_password) {
    redirect("/settings/password");
  }
  const identity = session.display_name || session.email;
  // Mission Control is admin-only. A customer box can never expose Control/Fleet
  // merely because its user is an administrator; that requires the server-issued
  // operator-surface capability.
  const navGroups = consoleNavigationGroups(session.operator_mode);
  const homeHref = session.operator_mode ? "/fleet" : "/chat";
  const activeLabel = ALL_NAV.find((item) => item.id === active)?.label || "Console";
  const surfaceLabel = session.operator_mode ? "Mission Control" : "Workspace";
  const initials = identity
    .split(/\s+|@/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("") || "OB";
  const shell = (
    <main className="consoleShell">
      <ConsoleNavigation active={active} groups={navGroups} homeHref={homeHref} />

      <div className="consoleFrame">
        <header className={workspaceMode === "feature" ? "commandBar featureScoped" : "commandBar"}>
          <div className="commandContext">
            <span>{surfaceLabel}</span>
            <i aria-hidden="true">/</i>
            <strong>{activeLabel}</strong>
          </div>
          {workspaceMode === "feature" ? null : active === "kpis" || active === "buchhaltung" ? <span /> : <WorkspaceSelector />}
          <div className="commandIdentity">
            <Link aria-label={`Account settings for ${identity}`} href="/settings">
              <span>{identity}</span>
              <strong className="commandAvatar" aria-hidden="true">{initials}</strong>
            </Link>
          </div>
        </header>

        <section className="consoleContent">
          {children}
        </section>
      </div>
    </main>
  );
  return workspaceMode === "feature" ? shell : <WorkspaceProvider session={session}>{shell}</WorkspaceProvider>;
}
