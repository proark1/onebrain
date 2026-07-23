import type { MessageKey } from "@/lib/i18n";

export type ConsoleSection =
  | "ai-employees"
  | "buchhaltung"
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

// Labels are message keys, not text: the sidebar and command bar resolve them
// through the active locale at render time (see console-navigation.tsx /
// console-command-bar.tsx). `id` stays the stable, locale-independent identity.
export type ConsoleNavItem = { id: ConsoleSection; href: string; labelKey: MessageKey };
export type ConsoleNavGroup = { id: string; labelKey: MessageKey; items: ConsoleNavItem[] };

export const STATUS_NAV: ConsoleNavItem = { id: "cockpit", href: "/cockpit", labelKey: "nav.status" };
export const CUSTOMER_NAV: ConsoleNavItem[] = [
  { id: "chat", href: "/chat", labelKey: "nav.ask" },
  { id: "drive", href: "/drive", labelKey: "nav.drive" },
  { id: "kpis", href: "/kpis", labelKey: "nav.kpis" },
  { id: "ai-employees", href: "/ai-employees", labelKey: "nav.aiEmployees" },
  { id: "buchhaltung", href: "/buchhaltung", labelKey: "nav.accounting" },
  { id: "spaces", href: "/spaces", labelKey: "nav.apps" },
  { id: "privacy", href: "/privacy", labelKey: "nav.privacy" },
  { id: "settings", href: "/settings", labelKey: "nav.settings" },
];
export const MISSION_CONTROL_NAV: ConsoleNavItem[] = [
  { id: "operator", href: "/operator", labelKey: "nav.control" },
  { id: "fleet", href: "/fleet", labelKey: "nav.fleet" },
  { id: "users", href: "/users", labelKey: "nav.users" },
  { id: "settings", href: "/settings", labelKey: "nav.settings" },
];
export const ALL_NAV: ConsoleNavItem[] = [STATUS_NAV, ...CUSTOMER_NAV, ...MISSION_CONTROL_NAV];

export function consoleNavigation(operatorMode: boolean): ConsoleNavItem[] {
  return operatorMode
    ? [STATUS_NAV, ...MISSION_CONTROL_NAV]
    : [STATUS_NAV, ...CUSTOMER_NAV];
}

const CUSTOMER_GROUPS: Array<{ id: string; labelKey: MessageKey; sections: ConsoleSection[] }> = [
  { id: "monitor", labelKey: "nav.group.monitor", sections: ["cockpit"] },
  { id: "work", labelKey: "nav.group.work", sections: ["chat", "drive", "kpis", "ai-employees", "buchhaltung"] },
  { id: "manage", labelKey: "nav.group.manage", sections: ["spaces", "privacy"] },
  { id: "account", labelKey: "nav.group.account", sections: ["settings"] },
];

const OPERATOR_GROUPS: Array<{ id: string; labelKey: MessageKey; sections: ConsoleSection[] }> = [
  { id: "monitor", labelKey: "nav.group.monitor", sections: ["cockpit"] },
  { id: "manage", labelKey: "nav.group.manage", sections: ["operator", "fleet", "users"] },
  { id: "account", labelKey: "nav.group.account", sections: ["settings"] },
];

export function consoleNavigationGroups(operatorMode: boolean): ConsoleNavGroup[] {
  const items = consoleNavigation(operatorMode);
  const itemById = new Map(items.map((item) => [item.id, item]));
  return (operatorMode ? OPERATOR_GROUPS : CUSTOMER_GROUPS)
    .map((group) => ({
      id: group.id,
      labelKey: group.labelKey,
      items: group.sections.flatMap((section) => {
        const item = itemById.get(section);
        return item ? [item] : [];
      }),
    }))
    .filter((group) => group.items.length > 0);
}
