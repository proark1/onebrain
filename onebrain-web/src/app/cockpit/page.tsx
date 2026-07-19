import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { CockpitPanel } from "@/components/cockpit-panel";
import { ConsoleShell } from "@/components/console-shell";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function CockpitPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/cockpit")} />;
  }

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="cockpit" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="cockpitBlockedTitle">
          <p className="eyebrow">Cockpit</p>
          <h1 id="cockpitBlockedTitle">Admin access required</h1>
          <p>System health, privacy posture, service-key signals, and job monitoring are limited to administrators.</p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="cockpit" session={sessionResult.session}>
      <CockpitPanel isOperatorSurface={sessionResult.session.is_operator_surface} />
    </ConsoleShell>
  );
}
