import { redirect } from "next/navigation";
import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { getSession, onebrainApiBaseUrl } from "@/lib/onebrain-api";

export default async function Home() {
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

  redirect("/chat");
}
