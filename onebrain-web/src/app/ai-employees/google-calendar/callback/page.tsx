import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { GoogleCalendarOAuthCallback } from "@/components/google-calendar-oauth-callback";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function GoogleCalendarCallbackPage({ searchParams }: {
  searchParams: Promise<{ code?: string; error?: string; state?: string }>;
}) {
  const [params, sessionResult] = await Promise.all([
    searchParams,
    getSession().then((session) => ({ apiUnavailable: false, session }))
      .catch(() => ({ apiUnavailable: true, session: null })),
  ]);
  if (sessionResult.apiUnavailable) return <ApiUnavailableState apiBaseUrl={onebrainApiBaseUrl()} />;
  if (!sessionResult.session) return <SignedOutState loginHref={loginHref("/ai-employees")} />;
  return (
    <ConsoleShell active="ai-employees" session={sessionResult.session}>
      <GoogleCalendarOAuthCallback
        code={params.code || ""}
        oauthError={params.error || ""}
        state={params.state || ""}
      />
    </ConsoleShell>
  );
}
