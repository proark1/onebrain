import Link from "next/link";
import type { ReactNode } from "react";
import type { SessionInfo } from "@/lib/onebrain-types";

type ConsoleSection = "chat" | "documents";

type ConsoleShellProps = {
  active: ConsoleSection;
  children: ReactNode;
  session: SessionInfo;
};

const PRIMARY_NAV: Array<{ id: ConsoleSection; href: string; label: string }> = [
  { id: "chat", href: "/chat", label: "Chat" },
  { id: "documents", href: "/documents", label: "Documents" },
];

const FUTURE_NAV = ["Spaces", "Privacy", "Operator"];

export function ConsoleShell({ active, children, session }: ConsoleShellProps) {
  const identity = session.display_name || session.email;

  return (
    <main className="consoleShell">
      <aside className="consoleSidebar" aria-label="OneBrain console">
        <div className="brandBlock">
          <Link className="brand" href="/chat">
            <span className="brandMark">one</span>
            <span>brain</span>
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

        <nav className="consoleNav mutedNav" aria-label="Future sections">
          {FUTURE_NAV.map((item) => (
            <span aria-disabled="true" key={item}>{item}</span>
          ))}
        </nav>

        <div className="consoleIdentity">
          <span>{session.role_label}</span>
          <small>{session.location_label}</small>
        </div>
      </aside>

      <section className="consoleContent">
        {children}
      </section>
    </main>
  );
}
