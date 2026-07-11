import Link from "next/link";
import type { ReactNode } from "react";
import { WorkspaceProvider } from "@/components/workspace-provider";
import { WorkspaceSelector } from "@/components/workspace-selector";
import type { SessionInfo } from "@/lib/onebrain-types";

type ConsoleSection = "chat" | "cockpit" | "documents" | "spaces" | "privacy" | "operator" | "fleet";

type ConsoleShellProps = {
  active: ConsoleSection;
  children: ReactNode;
  session: SessionInfo;
};

const PRIMARY_NAV: Array<{ id: ConsoleSection; href: string; label: string }> = [
  { id: "cockpit", href: "/cockpit", label: "Status" },
  { id: "chat", href: "/chat", label: "Ask" },
  { id: "documents", href: "/documents", label: "Knowledge" },
  { id: "spaces", href: "/spaces", label: "Apps" },
  { id: "privacy", href: "/privacy", label: "Privacy" },
  { id: "operator", href: "/operator", label: "Control" },
  { id: "fleet", href: "/fleet", label: "Fleet" },
];

export function ConsoleShell({ active, children, session }: ConsoleShellProps) {
  const identity = session.display_name || session.email;
  const activeLabel = PRIMARY_NAV.find((item) => item.id === active)?.label || "Console";

  return (
    <WorkspaceProvider session={session}>
      <main className="consoleShell">
        <aside className="consoleSidebar" aria-label="OneBrain console">
          <div className="brandBlock">
            <Link className="brand" href="/chat">
              <span className="brandMark">AD</span>
              <span>OneBrain</span>
            </Link>
            <p>{identity}</p>
          </div>

          <nav className="consoleNav" aria-label="Primary sections">
            {PRIMARY_NAV.map((item) => (
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

          <div className="consoleIdentity">
            <span>{session.role_label}</span>
            <small>{session.location_label}</small>
          </div>
        </aside>

        <div className="consoleFrame">
          <header className="commandBar">
            <div className="commandContext">
              <span>Assad Dar</span>
              <strong>{activeLabel}</strong>
            </div>
            <WorkspaceSelector />
            <div className="commandIdentity">
              <span>{session.role_label}</span>
              <small>{identity}</small>
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
