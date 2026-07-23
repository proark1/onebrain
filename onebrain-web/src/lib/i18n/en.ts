// English catalog. Typed as `Messages`, so it must carry exactly the keys defined
// in de.ts — TypeScript flags any missing or extra key at compile time.

import type { Messages } from "./de";

export const en: Messages = {
  // Navigation — destinations
  "nav.status": "Status",
  "nav.ask": "Ask",
  "nav.drive": "Drive",
  "nav.kpis": "KPIs",
  "nav.aiEmployees": "AI Employees",
  "nav.accounting": "Accounting",
  "nav.apps": "Apps",
  "nav.privacy": "Privacy",
  "nav.settings": "Settings",
  "nav.control": "Control",
  "nav.fleet": "Fleet",
  "nav.users": "Users",
  // Navigation — groups
  "nav.group.monitor": "Monitor",
  "nav.group.work": "Work",
  "nav.group.manage": "Manage",
  "nav.group.account": "Account",
  // Navigation — chrome
  "nav.open": "Open navigation",
  "nav.close": "Close navigation",
  "nav.consoleLabel": "OneBrain console",
  "nav.groupSections": "{group} sections",
  // Command bar / shell
  "shell.missionControl": "Mission Control",
  "shell.workspace": "Workspace",
  "shell.console": "Console",
  "shell.accountSettingsFor": "Account settings for {name}",
  // Language switcher
  "locale.label": "Language",
};
