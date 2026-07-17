import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { FleetPanel } from "@/components/fleet-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";
import { notFound } from "next/navigation";

export default async function FleetPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/fleet")} />;
  }

  if (!sessionResult.session.is_operator_surface) {
    notFound();
  }

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="fleet" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="fleetBlockedTitle">
          <p className="eyebrow">Mission Control</p>
          <h1 id="fleetBlockedTitle">Admin access required</h1>
          <p>Fleet overview, rollouts, and enrollment keys are limited to administrators.</p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="fleet" session={sessionResult.session}>
      <FleetPanel />
    </ConsoleShell>
  );
}
