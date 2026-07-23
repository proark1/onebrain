import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { ConsoleCommandBar } from "@/components/console-command-bar";
import { ConsoleNavigation } from "@/components/console-navigation";
import { LocaleProvider } from "@/components/locale-provider";
import { WorkspaceProvider } from "@/components/workspace-provider";
import { consoleNavigationGroups, type ConsoleSection } from "@/lib/console-navigation";
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
        <ConsoleCommandBar
          active={active}
          identity={identity}
          initials={initials}
          operatorMode={session.operator_mode}
          workspaceMode={workspaceMode}
        />

        <section className="consoleContent">
          {children}
        </section>
      </div>
    </main>
  );
  // LocaleProvider seeds the UI language from the account default and wraps every
  // console surface (both workspace and feature mode); WorkspaceProvider stays
  // scoped to console mode as before.
  const scoped =
    workspaceMode === "feature" ? shell : <WorkspaceProvider session={session}>{shell}</WorkspaceProvider>;
  return <LocaleProvider defaultLocale={session.default_locale}>{scoped}</LocaleProvider>;
}
