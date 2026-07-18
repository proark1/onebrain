export type ConsoleSection =
  | "ai-employees"
  | "chat"
  | "cockpit"
  | "documents"
  | "kpis"
  | "spaces"
  | "privacy"
  | "settings"
  | "operator"
  | "fleet"
  | "users";

export type ConsoleNavItem = { id: ConsoleSection; href: string; label: string };

export const STATUS_NAV: ConsoleNavItem = { id: "cockpit", href: "/cockpit", label: "Status" };
export const CUSTOMER_NAV: ConsoleNavItem[] = [
  { id: "chat", href: "/chat", label: "Ask" },
  { id: "documents", href: "/documents", label: "Knowledge" },
  { id: "kpis", href: "/kpis", label: "KPIs" },
  { id: "ai-employees", href: "/ai-employees", label: "AI Employees" },
  { id: "spaces", href: "/spaces", label: "Apps" },
  { id: "privacy", href: "/privacy", label: "Privacy" },
  { id: "settings", href: "/settings", label: "Settings" },
];
export const MISSION_CONTROL_NAV: ConsoleNavItem[] = [
  { id: "operator", href: "/operator", label: "Control" },
  { id: "fleet", href: "/fleet", label: "Fleet" },
  { id: "users", href: "/users", label: "Users" },
  { id: "settings", href: "/settings", label: "Settings" },
];
export const ALL_NAV: ConsoleNavItem[] = [STATUS_NAV, ...CUSTOMER_NAV, ...MISSION_CONTROL_NAV];

export function consoleNavigation(operatorMode: boolean): ConsoleNavItem[] {
  return operatorMode
    ? [STATUS_NAV, ...MISSION_CONTROL_NAV]
    : [STATUS_NAV, ...CUSTOMER_NAV];
}
