import { redirect } from "next/navigation";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function Home() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/chat")} />;
  }

  // Mission Control has no customer surface — land the operator on the fleet overview.
  if (sessionResult.session.operator_mode) {
    redirect("/fleet");
  }

  redirect(sessionResult.session.role_id === "admin" ? "/cockpit" : "/chat");
}
