import { ApiUnavailableState, SignedOutState } from "@/components/app-state";
import { ConsoleShell } from "@/components/console-shell";
import { DocumentsPanel } from "@/components/documents-panel";
import {
  getSession,
  listServerDocuments,
  listServerPendingDocuments,
  onebrainApiBaseUrl,
} from "@/lib/onebrain-api";
import type { DocumentSummary } from "@/lib/onebrain-types";

export default async function DocumentsPage() {
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

  const [documentsResult, pendingResult] = await Promise.all([
    listServerDocuments()
      .then((documents) => ({ documents, error: "" }))
      .catch(() => ({ documents: [] as DocumentSummary[], error: "Could not load documents." })),
    listServerPendingDocuments()
      .then((documents) => ({ available: true, documents }))
      .catch(() => ({ available: false, documents: [] })),
  ]);

  return (
    <ConsoleShell active="documents" session={sessionResult.session}>
      <DocumentsPanel
        initialDocuments={documentsResult.documents}
        initialError={documentsResult.error}
        initialPending={pendingResult.documents}
        pendingReviewAvailable={pendingResult.available}
      />
    </ConsoleShell>
  );
}
