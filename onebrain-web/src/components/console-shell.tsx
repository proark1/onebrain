import Link from "next/link";
import type { ReactNode } from "react";
import { redirect } from "next/navigation";
import { WorkspaceProvider } from "@/components/workspace-provider";
import { WorkspaceSelector } from "@/components/workspace-selector";
import { ALL_NAV, consoleNavigation, type ConsoleSection } from "@/lib/console-navigation";
import type { SessionInfo } from "@/lib/onebrain-types";

type ConsoleShellProps = {
  active: ConsoleSection;
  children: ReactNode;
  session: SessionInfo;
};

export function ConsoleShell({ active, children, session }: ConsoleShellProps) {
  if (session.must_change_password) {
    redirect("/settings/password");
  }
  const identity = session.display_name || session.email;
  // Mission Control is admin-only. A customer box can never expose Control/Fleet
  // merely because its user is an administrator; that requires the server-issued
  // operator-surface capability.
  const nav = consoleNavigation(session.operator_mode);
  const homeHref = session.operator_mode ? "/fleet" : "/chat";
  const activeLabel = ALL_NAV.find((item) => item.id === active)?.label || "Console";

  return (
    <WorkspaceProvider session={session}>
      <main className="consoleShell">
        <aside className="consoleSidebar" aria-label="OneBrain console">
          <div className="brandBlock">
            <Link className="brand" href={homeHref}>
              <span className="brandMark">AD</span>
              <span>OneBrain</span>
            </Link>
          </div>

          <nav className="consoleNav" aria-label="Primary sections">
            {nav.map((item) => (
              <Link
                aria-current={active === item.id ? "page" : undefined}
                className={active === item.id ? "active" : ""}
                href={item.href}
                key={item.id}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          </aside>

        <div className="consoleFrame">
          <header className="commandBar">
            <div className="commandContext">
              <span>{identity}</span>
              <strong>{activeLabel}</strong>
            </div>
            {active === "kpis" ? <span /> : <WorkspaceSelector />}
            <div className="commandIdentity">
              <Link aria-label={`Account settings for ${identity}`} href="/settings">{identity}</Link>
            </div>
          </header>

          <section className="consoleContent">
            {children}
          </section>
        </div>
      </main>
    </WorkspaceProvider>
  );
}
