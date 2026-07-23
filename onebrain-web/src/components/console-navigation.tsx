"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { ConsoleNavGroup, ConsoleSection } from "@/lib/console-navigation";

type ConsoleNavigationProps = {
  active: ConsoleSection;
  groups: ConsoleNavGroup[];
  homeHref: string;
};

const ICON_PATHS: Record<ConsoleSection, ReactNode> = {
  "ai-employees": <><circle cx="12" cy="8" r="3" /><path d="M6.5 19c.8-3.2 2.6-5 5.5-5s4.7 1.8 5.5 5" /><path d="M18 5.5v5M15.5 8h5" /></>,
  buchhaltung: <><path d="M6.5 3.5h11v17l-1.8-1.2-1.8 1.2-1.9-1.2-1.9 1.2-1.8-1.2-1.9 1.2V3.5Z" /><path d="M9.5 8h5M9.5 11.5h5" /></>,
  chat: <><path d="M5 6.5h14v9H9l-4 3v-12Z" /><path d="M8.5 10h7" /></>,
  cockpit: <><path d="M4.5 17.5V11l3-3 3 2 4.5-5 4.5 3.5v9" /><path d="M4 19h16" /></>,
  drive: <><path d="M4 8h6l2-2h8v12H4V8Z" /><path d="M4 10h16" /></>,
  fleet: <><rect x="4" y="5" width="16" height="5" rx="1" /><rect x="4" y="14" width="16" height="5" rx="1" /><path d="M7 7.5h.01M7 16.5h.01" /></>,
  kpis: <><path d="M5 18V9M12 18V5M19 18v-6" /><path d="M3.5 19.5h17" /></>,
  operator: <><path d="M12 3.5 19 7v5c0 4.2-2.7 7-7 8.5C7.7 19 5 16.2 5 12V7l7-3.5Z" /><path d="m9 12 2 2 4-5" /></>,
  privacy: <><rect x="5" y="10" width="14" height="10" rx="2" /><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v2" /></>,
  settings: <><circle cx="12" cy="12" r="3" /><path d="M12 3.5v2M12 18.5v2M3.5 12h2M18.5 12h2M6 6l1.5 1.5M16.5 16.5 18 18M18 6l-1.5 1.5M7.5 16.5 6 18" /></>,
  spaces: <><rect x="4" y="4" width="7" height="7" rx="1" /><rect x="13" y="4" width="7" height="7" rx="1" /><rect x="4" y="13" width="7" height="7" rx="1" /><rect x="13" y="13" width="7" height="7" rx="1" /></>,
  users: <><circle cx="9" cy="8" r="3" /><path d="M3.5 19c.7-3.5 2.5-5.5 5.5-5.5s4.8 2 5.5 5.5" /><path d="M15 6.5a2.5 2.5 0 0 1 0 5M16 14c2.3.4 3.7 2 4.2 4.5" /></>,
};

function NavIcon({ section }: { section: ConsoleSection }) {
  return (
    <svg aria-hidden="true" className="consoleNavIcon" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7">
      {ICON_PATHS[section]}
    </svg>
  );
}

export function ConsoleNavigation({ active, groups, homeHref }: ConsoleNavigationProps) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const sidebarRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    sidebarRef.current?.querySelector<HTMLElement>("a[aria-current='page'], a")?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        requestAnimationFrame(() => triggerRef.current?.focus());
        return;
      }

      if (event.key !== "Tab") return;
      const focusable = Array.from(
        sidebarRef.current?.querySelectorAll<HTMLElement>("a[href], button:not([disabled])") ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  function closeNavigation({ restoreFocus = false } = {}) {
    setOpen(false);
    if (restoreFocus) requestAnimationFrame(() => triggerRef.current?.focus());
  }

  return (
    <>
      <button
        aria-controls="console-navigation"
        aria-expanded={open}
        aria-label={open ? "Close navigation" : "Open navigation"}
        className={`mobileNavButton ${open ? "open" : ""}`}
        onClick={() => setOpen((current) => !current)}
        ref={triggerRef}
        type="button"
      >
        <span /><span /><span />
      </button>
      <button
        aria-label="Close navigation"
        className={`consoleNavBackdrop ${open ? "open" : ""}`}
        onClick={() => closeNavigation({ restoreFocus: true })}
        tabIndex={open ? 0 : -1}
        type="button"
      />
      <aside
        aria-label="OneBrain console"
        className={`consoleSidebar ${open ? "open" : ""}`}
        id="console-navigation"
        ref={sidebarRef}
      >
        <div className="brandBlock">
          <Link className="brand" href={homeHref} onClick={() => closeNavigation()}>
            <span className="brandMark">AD</span>
            <span className="brandName">OneBrain</span>
          </Link>
        </div>
        <div className="consoleNavGroups">
          {groups.map((group) => (
            <div className="consoleNavGroup" key={group.id}>
              <p className="consoleNavLabel">{group.label}</p>
              <nav className="consoleNav" aria-label={`${group.label} sections`}>
                {group.items.map((item) => (
                  <Link
                    aria-current={active === item.id ? "page" : undefined}
                    aria-label={item.label}
                    className={active === item.id ? "active" : ""}
                    href={item.href}
                    key={item.id}
                    onClick={() => closeNavigation()}
                    title={item.label}
                  >
                    <NavIcon section={item.id} />
                    <span>{item.label}</span>
                  </Link>
                ))}
              </nav>
            </div>
          ))}
        </div>
      </aside>
    </>
  );
}
