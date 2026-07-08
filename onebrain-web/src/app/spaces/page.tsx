import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { SpacesPanel } from "@/components/spaces-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";

export default async function SpacesPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState apiBaseUrl={apiBaseUrl} />;
  }

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="spaces" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="spacesBlockedTitle">
          <p className="eyebrow">Platform admin</p>
          <h1 id="spacesBlockedTitle">Admin access required</h1>
          <p>
            Account, space, app-installation, access-check, and audit workflows are limited to administrators.
          </p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="spaces" session={sessionResult.session}>
      <SpacesPanel />
    </ConsoleShell>
  );
}
