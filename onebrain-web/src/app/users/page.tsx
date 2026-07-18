import { notFound } from "next/navigation";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { UsersPanel } from "@/components/users-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function UsersPage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  if (!sessionResult.session) return <SignedOutState loginHref={loginHref("/users")} />;
  if (!sessionResult.session.is_operator_surface) notFound();

  if (sessionResult.session.role_id !== "admin") {
    return (
      <ConsoleShell active="users" session={sessionResult.session}>
        <section className="blockedPanel" aria-labelledby="usersBlockedTitle">
          <p className="eyebrow">Mission Control</p>
          <h1 id="usersBlockedTitle">Admin access required</h1>
          <p>User directories and account recovery are limited to Mission Control administrators.</p>
        </section>
      </ConsoleShell>
    );
  }

  return (
    <ConsoleShell active="users" session={sessionResult.session}>
      <UsersPanel />
    </ConsoleShell>
  );
}
