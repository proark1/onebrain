import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { PrivacyPanel } from "@/components/privacy-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function PrivacyPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/privacy")} />;
  }

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="privacy" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="privacyBlockedTitle">
          <p className="eyebrow">Privacy center</p>
          <h1 id="privacyBlockedTitle">Admin access required</h1>
          <p>
            Privacy exports and erasure are restricted to administrators because they can expose or remove account
            data across documents, conversations, intake records, and audit logs.
          </p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="privacy" session={sessionResult.session}>
      <PrivacyPanel />
    </ConsoleShell>
  );
}
