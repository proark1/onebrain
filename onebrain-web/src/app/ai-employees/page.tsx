import { AiEmployeesPanel } from "@/components/ai-employees-panel";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function AiEmployeesPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/ai-employees")} />;
  }

  return (
    <ConsoleShell active="ai-employees" session={sessionResult.session}>
      <AiEmployeesPanel />
    </ConsoleShell>
  );
}
