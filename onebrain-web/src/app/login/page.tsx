import { redirect } from "next/navigation";
import { ApiUnavailableState } from "@/components/app-state";
import { LoginPanel } from "@/components/login-panel";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";
import { safeLoginRedirect } from "@/lib/login-redirect";

type LoginPageProps = {
  searchParams: Promise<{ next?: string | string[]; passwordChanged?: string | string[] }>;
};

export default async function LoginPage({ searchParams }: LoginPageProps) {
  const params = await searchParams;
  const nextPath = safeLoginRedirect(params.next);
  const apiBaseUrl = onebrainApiBaseUrl();
  const sessionResult = await getSession()
    .then((session) => ({ apiUnavailable: false, session }))
    .catch(() => ({ apiUnavailable: true, session: null }));

  if (sessionResult.apiUnavailable) {
    return <ApiUnavailableState apiBaseUrl={apiBaseUrl} />;
  }

  if (sessionResult.session) {
    redirect(nextPath);
  }

  return <LoginPanel nextPath={nextPath} passwordChanged={params.passwordChanged === "1"} />;
}
