import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { PasswordChangePanel } from "@/components/password-change-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function PasswordPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));
  if (sessionResult.apiUnavailable) return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  if (!sessionResult.session) return <SignedOutState loginHref={loginHref("/settings/password")} />;
  if (sessionResult.session.must_change_password) return <PasswordChangePanel standalone />;
  return (
    <ConsoleShell active="settings" session={sessionResult.session}>
      <PasswordChangePanel />
    </ConsoleShell>
  );
}
