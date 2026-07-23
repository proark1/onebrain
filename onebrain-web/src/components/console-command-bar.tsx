"use client";

import Link from "next/link";
import { useTranslations } from "@/components/locale-provider";
import { LocaleSwitcher } from "@/components/locale-switcher";
import { WorkspaceSelector } from "@/components/workspace-selector";
import { ALL_NAV, type ConsoleSection } from "@/lib/console-navigation";

type ConsoleCommandBarProps = {
  active: ConsoleSection;
  identity: string;
  initials: string;
  operatorMode: boolean;
  workspaceMode: "console" | "feature";
};

// The command bar was inlined in ConsoleShell (a Server Component) with hardcoded
// English. It now lives here as a client component so its labels — and the active
// destination name — resolve through the active locale, and so the language
// switcher can sit beside the identity link.
export function ConsoleCommandBar({ active, identity, initials, operatorMode, workspaceMode }: ConsoleCommandBarProps) {
  const { t } = useTranslations();
  const surfaceLabel = operatorMode ? t("shell.missionControl") : t("shell.workspace");
  const activeItem = ALL_NAV.find((item) => item.id === active);
  const activeLabel = activeItem ? t(activeItem.labelKey) : t("shell.console");
  return (
    <header className={workspaceMode === "feature" ? "commandBar featureScoped" : "commandBar"}>
      <div className="commandContext">
        <span>{surfaceLabel}</span>
        <i aria-hidden="true">/</i>
        <strong>{activeLabel}</strong>
      </div>
      {workspaceMode === "feature" ? null : active === "kpis" || active === "buchhaltung" ? <span /> : <WorkspaceSelector />}
      <div className="commandIdentity">
        <LocaleSwitcher />
        <Link aria-label={t("shell.accountSettingsFor", { name: identity })} href="/settings">
          <span>{identity}</span>
          <strong className="commandAvatar" aria-hidden="true">{initials}</strong>
        </Link>
      </div>
    </header>
  );
}
