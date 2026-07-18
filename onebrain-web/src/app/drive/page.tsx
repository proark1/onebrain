import { redirect } from "next/navigation";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { DriveApp } from "@/features/drive/drive-app";
import { getDriveBootstrap } from "@/features/drive/drive-server";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { loginHref } from "@/lib/login-redirect";

export default async function DrivePage() {
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }
  if (!sessionResult.session) {
    return <SignedOutState loginHref={loginHref("/drive")} />;
  }
  if (sessionResult.session.must_change_password) {
    redirect("/settings/password");
  }
  if (sessionResult.session.operator_mode) {
    redirect("/fleet");
  }

  const bootstrapResult = await getDriveBootstrap()
    .then((bootstrap) => ({ bootstrap, error: "" }))
    .catch((error: unknown) => ({
      bootstrap: undefined,
      error: error instanceof Error ? error.message : "Drive could not load.",
    }));

  return (
    <ConsoleShell active="drive" session={sessionResult.session} workspaceMode="feature">
      <DriveApp initialBootstrap={bootstrapResult.bootstrap} initialError={bootstrapResult.error} />
    </ConsoleShell>
  );
}
