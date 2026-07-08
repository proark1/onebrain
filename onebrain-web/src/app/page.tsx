import { ChatShell } from "@/components/chat-shell";
import { getSession, listServerConversations, onebrainApiBaseUrl } from "@/lib/onebrain-api";

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

  const conversations = await listServerConversations().catch(() => []);

  return <ChatShell initialConversations={conversations} session={sessionResult.session} />;
}

function SignedOutState({ apiBaseUrl }: { apiBaseUrl: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <h1>Sign in to chat</h1>
        <p>Use the existing OneBrain login, then return here to continue in the new web console.</p>
        <a className="stateAction" href={apiBaseUrl}>Open login</a>
      </section>
    </main>
  );
}

function ApiUnavailableState({ apiBaseUrl }: { apiBaseUrl: string }) {
  return (
    <main className="stateScreen">
      <section className="statePanel">
        <div className="brand">
          <span className="brandMark">one</span>
          <span>brain</span>
        </div>
        <h1>Core API unavailable</h1>
        <p>Start the FastAPI service and refresh this page. The web console is configured for {apiBaseUrl}.</p>
      </section>
    </main>
  );
}
