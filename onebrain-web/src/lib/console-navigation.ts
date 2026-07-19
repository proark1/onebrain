export type ConsoleSection =
  | "ai-employees"
  | "chat"
  | "cockpit"
  | "drive"
  | "kpis"
  | "spaces"
  | "privacy"
  | "settings"
  | "operator"
  | "fleet"
  | "users";

export type ConsoleNavItem = { id: ConsoleSection; href: string; label: string };
export type ConsoleNavGroup = { id: string; label: string; items: ConsoleNavItem[] };

export const STATUS_NAV: ConsoleNavItem = { id: "cockpit", href: "/cockpit", label: "Status" };
export const CUSTOMER_NAV: ConsoleNavItem[] = [
  { id: "chat", href: "/chat", label: "Ask" },
  { id: "drive", href: "/drive", label: "Drive" },
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

const CUSTOMER_GROUPS: Array<{ id: string; label: string; sections: ConsoleSection[] }> = [
  { id: "monitor", label: "Monitor", sections: ["cockpit"] },
  { id: "work", label: "Work", sections: ["chat", "drive", "kpis", "ai-employees"] },
  { id: "manage", label: "Manage", sections: ["spaces", "privacy"] },
  { id: "account", label: "Account", sections: ["settings"] },
];

const OPERATOR_GROUPS: Array<{ id: string; label: string; sections: ConsoleSection[] }> = [
  { id: "monitor", label: "Monitor", sections: ["cockpit"] },
  { id: "manage", label: "Manage", sections: ["operator", "fleet", "users"] },
  { id: "account", label: "Account", sections: ["settings"] },
];

export function consoleNavigationGroups(operatorMode: boolean): ConsoleNavGroup[] {
  const items = consoleNavigation(operatorMode);
  const itemById = new Map(items.map((item) => [item.id, item]));
  return (operatorMode ? OPERATOR_GROUPS : CUSTOMER_GROUPS)
    .map((group) => ({
      id: group.id,
      label: group.label,
      items: group.sections.flatMap((section) => {
        const item = itemById.get(section);
        return item ? [item] : [];
      }),
    }))
    .filter((group) => group.items.length > 0);
}
