import { redirect } from "next/navigation";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { SettingsPanel } from "@/components/settings-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function SettingsPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));
  if (sessionResult.apiUnavailable) return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  if (!sessionResult.session) return <SignedOutState loginHref={loginHref("/settings")} />;
  if (sessionResult.session.must_change_password) redirect("/settings/password");
  return (
    <ConsoleShell active="settings" session={sessionResult.session}>
      <SettingsPanel />
    </ConsoleShell>
  );
}
