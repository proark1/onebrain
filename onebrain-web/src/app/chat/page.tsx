import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ChatPanel } from "@/components/chat-panel";
import { ConsoleShell } from "@/components/console-shell";
import { getSession, listServerConversations, onebrainApiBaseUrl } from "@/lib/onebrain-api";

export default async function ChatPage() {
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

  return (
    <ConsoleShell active="chat" session={sessionResult.session}>
      <ChatPanel initialConversations={conversations} session={sessionResult.session} />
    </ConsoleShell>
  );
}
