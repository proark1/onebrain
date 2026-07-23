// German catalog — the platform's primary language and the source of truth for
// the message-key set. `en.ts` is typed against this, so a missing or misspelled
// key there is a compile error. Values are plain strings; {token} placeholders are
// filled by translate(). Add new keys here first.

export const de = {
  // Navigation — destinations
  "nav.status": "Status",
  "nav.ask": "Assistent",
  "nav.drive": "Drive",
  "nav.kpis": "Kennzahlen",
  "nav.aiEmployees": "KI-Mitarbeiter",
  "nav.accounting": "Buchhaltung",
  "nav.apps": "Apps",
  "nav.privacy": "Datenschutz",
  "nav.settings": "Einstellungen",
  "nav.control": "Steuerung",
  "nav.fleet": "Flotte",
  "nav.users": "Benutzer",
  // Navigation — groups
  "nav.group.monitor": "Überwachung",
  "nav.group.work": "Arbeit",
  "nav.group.manage": "Verwaltung",
  "nav.group.account": "Konto",
  // Navigation — chrome
  "nav.open": "Navigation öffnen",
  "nav.close": "Navigation schließen",
  "nav.consoleLabel": "OneBrain-Konsole",
  "nav.groupSections": "{group} – Bereiche",
  // Command bar / shell
  "shell.missionControl": "Mission Control",
  "shell.workspace": "Arbeitsbereich",
  "shell.console": "Konsole",
  "shell.accountSettingsFor": "Kontoeinstellungen für {name}",
  // Language switcher
  "locale.label": "Sprache",
};

export type Messages = typeof de;
