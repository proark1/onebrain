import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { AccountingPanel } from "@/components/accounting-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function BuchhaltungPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/buchhaltung")} />;
  }

  return (
    <ConsoleShell active="buchhaltung" session={sessionResult.session}>
      <AccountingPanel />
    </ConsoleShell>
  );
}
