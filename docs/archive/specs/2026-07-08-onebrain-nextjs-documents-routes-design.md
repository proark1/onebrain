# OneBrain Next.js Documents Routes Design

Date: 2026-07-08

## Decision

Use route-based Next.js screens for the product console, while keeping the backend in Python/FastAPI.

The Python service remains the system of record for authentication, authorization, ingestion, document extraction, PII checks, review approval, storage, retrieval, and privacy enforcement. The Next.js app becomes the richer browser UI that calls those existing APIs through the local proxy.

## Goals

- Move the document library, upload, and pending review workflows into the Next.js app.
- Use explicit routes instead of one single shell-only page:
  - `/chat`
  - `/documents`
  - `/` as a small routing/status entry point.
- Preserve the existing FastAPI document behavior and safety boundaries.
- Make file upload binary-safe through the Next.js proxy.
- Keep the existing static FastAPI UI available during the migration.

## Non-Goals

- Do not rewrite the Python backend in TypeScript.
- Do not move ingestion, access control, vector storage, review policy, or privacy logic into Next.js.
- Do not migrate operator, provisioning, privacy, or account/space management in this slice.
- Do not remove the existing static UI yet.
- Do not introduce a new database, queue, or document schema in this slice.
- Do not add an admin document delete workflow in this slice.

## Architecture

The web app should move from a chat-only shell to a routed console:

```text
onebrain-web
  app/
    page.tsx              entry point and signed-out/API-unavailable states
    chat/page.tsx         chat route
    documents/page.tsx    document route
    api/onebrain/[...path]/route.ts
  src/
    components/
      console-shell.tsx
      chat-panel.tsx
      documents-panel.tsx
    lib/
      onebrain-api.ts
      onebrain-client.ts
      onebrain-types.ts
```

`console-shell.tsx` owns the shared navigation, identity display, and responsive layout. Route pages provide the active section and the initial server-fetched data. This prevents the document UI from being hidden inside the chat component and gives future sections a stable route pattern.

## Components

### Console Shell

The shared shell renders the brand, user identity, route navigation, and active content region. It should use normal Next.js links for navigation so refresh, history, and direct URLs work.

The navigation should include active links for Chat and Documents. Spaces, Privacy, and Operator can remain visible as disabled future links only if they do not look like broken controls.

### Chat Route

The existing chat behavior should move into a chat-focused panel component. The conversation list remains in the chat route, not in the global shell, because it is specific to chat.

The chat route server-prefetches conversations through the Python API using forwarded cookies, then hydrates the existing streaming chat client.

### Documents Route

The document route server-prefetches:

- visible documents from `GET /api/documents`
- pending documents from `GET /api/documents/pending`

Pending fetch failures caused by permissions should not fail the page. They should render as an empty/non-reviewer state, matching the old static UI behavior.

The client document panel should support:

- visible document list with classification, category, location, status, chunk count, and PII finding count when present
- upload form with file picker/drop target, classification, location, and category
- pending review queue with approve action
- inline refresh after upload or approval
- role-appropriate empty and error states

## API and Data Flow

All browser calls continue to go through `/api/onebrain/...`, which proxies to the FastAPI service.

Needed typed client functions:

- `listDocuments(scope?)`
- `listPendingDocuments(scope?)`
- `uploadDocument(input, scope?)`
- `approveDocument(id, scope?)`

The upload path must use `FormData` in the browser and preserve the multipart payload through the proxy. The current proxy converts request bodies to text; this must be changed to binary-safe forwarding for non-GET/non-HEAD requests. JSON, SSE, approval, and multipart upload should all continue to work through the same route handler.

Server-side route prefetches should use `onebrain-api.ts` with forwarded cookies. Client-side refreshes and mutations should use `onebrain-client.ts` through the proxy.

## Error Handling

- If the Python API is unreachable, the app shows the existing API-unavailable state.
- If the user is signed out, the app shows the existing signed-out state with a link to the Python login.
- If document listing fails, show an inline error and keep the route usable.
- If pending review is forbidden, hide the review queue rather than showing a hard error.
- If upload fails, keep the selected file and labels so the user can adjust and retry.
- If approval fails because of four-eyes or clearance rules, show the backend message inline and keep the pending item visible.
- If the file type is unsupported client-side, reject it before upload with clear copy.
- The backend remains the final authority for size limits, supported extraction, PII policy, and approval rules.

## UI Direction

This is an operational console, not a marketing page. Keep the quiet, dense visual system already established in the chat migration:

- restrained green, ink, wine, amber, and red status colors
- compact route navigation
- document rows designed for scanning
- upload as a functional panel or dialog, not a hero section
- no decorative cards nested inside cards
- responsive layout that keeps document metadata readable on small screens

The signature UI detail should be a document "classification rail": a thin status color strip on document rows and pending items. It gives the knowledge base a recognizable safety-oriented visual language without making the UI loud.

## Testing

Run the existing backend and frontend checks:

- Python test suite
- Next.js typecheck
- Next.js lint
- npm audit at the existing threshold
- Next.js production build

Add focused coverage or smoke checks for:

- server document prefetch helpers
- binary-safe proxy forwarding for multipart upload where practical
- document route rendering without pending-review permission
- document upload and approve refresh behavior through runtime smoke testing

Runtime smoke should verify:

- `/chat` loads for a signed-in session
- `/documents` loads for a signed-in session
- proxied `GET /api/onebrain/documents` returns data
- proxied upload does not corrupt multipart data
- chat streaming still works after the proxy body-forwarding change

## Rollout

This is an additive migration. The existing FastAPI static UI stays available. Once the Next.js document route is verified, the next migration slice can be workspace/account selection or privacy/export controls, depending on which workflow blocks real use first.

## Spec Self-Review

- No placeholders remain.
- The backend ownership boundary is explicit: Python/FastAPI stays in charge of sensitive behavior.
- The route-based Option B decision is explicit.
- The multipart proxy risk is called out before implementation.
- Scope is limited to chat route refactor plus document library/upload/review in Next.js.
