import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
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
  return <PasswordChangePanel />;
}
