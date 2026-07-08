import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { OperatorPanel } from "@/components/operator-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function OperatorPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/operator")} />;
  }

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="operator" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="operatorBlockedTitle">
          <p className="eyebrow">Control plane</p>
          <h1 id="operatorBlockedTitle">Admin access required</h1>
          <p>
            Provisioning, release planning, service-key revocation, and rollout controls are limited to administrators.
          </p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="operator" session={sessionResult.session}>
      <OperatorPanel />
    </ConsoleShell>
  );
}
