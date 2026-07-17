import Link from "next/link";
import type { ReactNode } from "react";
import { redirect } from "next/navigation";
import { WorkspaceProvider } from "@/components/workspace-provider";
import { WorkspaceSelector } from "@/components/workspace-selector";
import type { SessionInfo } from "@/lib/onebrain-types";

type ConsoleSection = "ai-employees" | "chat" | "cockpit" | "documents" | "kpis" | "spaces" | "privacy" | "settings" | "operator" | "fleet";

type ConsoleShellProps = {
  active: ConsoleSection;
  children: ReactNode;
  session: SessionInfo;
};

type NavItem = { id: ConsoleSection; href: string; label: string };

const STATUS_NAV: NavItem = { id: "cockpit", href: "/cockpit", label: "Status" };
// Customer surface — hidden on Mission Control (operator_mode), where there is no
// customer content to ask about, manage, or govern.
const CUSTOMER_NAV: NavItem[] = [
  { id: "chat", href: "/chat", label: "Ask" },
  { id: "documents", href: "/documents", label: "Knowledge" },
  { id: "kpis", href: "/kpis", label: "KPIs" },
  { id: "ai-employees", href: "/ai-employees", label: "AI Employees" },
  { id: "spaces", href: "/spaces", label: "Apps" },
  { id: "privacy", href: "/privacy", label: "Privacy" },
  { id: "settings", href: "/settings", label: "Settings" },
];
const ADMIN_NAV: NavItem[] = [
  { id: "operator", href: "/operator", label: "Control" },
  { id: "fleet", href: "/fleet", label: "Fleet" },
];
// Canonical full order (customer boxes) + label lookup for the command bar.
const ALL_NAV: NavItem[] = [STATUS_NAV, ...CUSTOMER_NAV, ...ADMIN_NAV];

export function ConsoleShell({ active, children, session }: ConsoleShellProps) {
  if (session.must_change_password) {
    redirect("/settings/password");
  }
  const identity = session.display_name || session.email;
  // Mission Control: admin-only layout (Status / Control / Fleet). Customer boxes
  // keep the full nav in its canonical order.
  const nav = session.operator_mode ? [STATUS_NAV, ...ADMIN_NAV] : ALL_NAV;
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
            <p>{identity}</p>
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
            {active === "kpis" ? <span /> : <WorkspaceSelector />}
          <div className="commandIdentity">
            <span>{session.role_label}</span>
            <Link href="/settings">{identity}</Link>
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
