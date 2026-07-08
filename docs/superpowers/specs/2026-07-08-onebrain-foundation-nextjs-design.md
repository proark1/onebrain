# OneBrain Foundation Hardening And Next.js Entry Point Design

Date: 2026-07-08

## Summary

OneBrain should keep the existing Python/FastAPI backend as the core brain and add a separate TypeScript/React/Next.js frontend as the product shell. The first milestone hardens the backend paths that protect customer data and opens a clean migration path for the UI without rewriting the retrieval, ingestion, privacy, or access-control core.

This is an incremental architecture migration, not a full-stack rewrite. The backend remains the source of truth for identity, service keys, tenant/account/space scoping, document ingestion, retrieval, privacy operations, and AI provider routing. The new frontend consumes the existing API through a typed client generated from FastAPI's OpenAPI schema.

## Goals

- Preserve the existing Python security and retrieval core.
- Reduce production risk in upload, vector-store, and test automation paths.
- Create a Next.js app that can gradually replace the vanilla static UI.
- Establish a typed API boundary between FastAPI and the frontend.
- Keep the first milestone small enough to verify with tests and review.

## Non-Goals

- Do not rewrite FastAPI routes in Next.js route handlers.
- Do not move document extraction, OCR, vector search, access policy, service-key checks, or GDPR operations into Node.
- Do not migrate every UI screen in the first milestone.
- Do not introduce billing, full onboarding, channel connectors, or assistant memory in this milestone.
- Do not change the public product model beyond what is needed to support the migration path.

## Architecture Decision

FastAPI remains the OneBrain core API. Next.js becomes a separate web app:

```text
onebrain-api   Python/FastAPI
  - auth/session
  - documents and ingestion
  - retrieval/RAG
  - platform accounts/spaces/apps
  - service keys
  - privacy/export/delete
  - operator/provisioning/control plane

onebrain-web   Next.js/React/TypeScript
  - app shell
  - chat UI
  - upload/document UI
  - workspace selector
  - later: admin, privacy center, operator dashboard

shared boundary
  - FastAPI OpenAPI schema
  - generated TypeScript client
  - same-origin or configured API base URL
```

The existing static UI can continue to be served by FastAPI while the Next.js app is introduced. During the transition, both UIs may exist side by side. New complex screens should be built in Next.js once the shell and typed API client are in place.

## Milestone Scope

### Backend Foundation

1. Add CI for Python tests.
   - Run `python -m pytest -q`.
   - Use a Python version supported by the project.
   - Install `requirements-dev.txt`.

2. Make upload handling safer.
   - Enforce upload size while reading bytes, not only through `Content-Length`.
   - Reject oversized bodies deterministically.
   - Offload synchronous ingestion work from the event loop using a threadpool boundary.

3. Stop destructive vector-store startup behavior.
   - The pgvector store must not drop the `chunks` table when embedding dimensions change.
   - Startup should fail with a clear error explaining that a re-embed/migration is required.
   - A later milestone can add versioned embeddings and automated re-embedding.

4. Prepare migration discipline.
   - Document that table creation in stores is prototype bootstrap behavior.
   - Add a lightweight migration plan note in docs, not a full Alembic conversion yet.
   - Avoid broad schema rewrites in this milestone.

### Next.js Entry Point

1. Scaffold `onebrain-web`.
   - TypeScript, React, Next.js App Router.
   - A minimal app shell with login-aware starter routes.
   - Environment variable for the FastAPI API base URL.

2. Add an API client boundary.
   - Export FastAPI's OpenAPI schema as a generated artifact or command.
   - Add a generated or typed client path for Next.js.
   - Keep cookies/auth behavior compatible with the current FastAPI session model.

3. Migrate only the first UI slice.
   - The first slice should be the app shell plus read-only session check, not the full chat/admin UI.
   - Chat/document upload migration comes after the shell proves the API client, auth, and dev workflow.

## Data Flow

For the existing FastAPI UI:

```text
browser -> FastAPI static UI -> FastAPI API -> Python services/stores
```

For the new Next.js UI:

```text
browser -> Next.js app -> FastAPI API -> Python services/stores
```

If deployed same-origin later, the proxy layer should forward cookies to FastAPI. If deployed cross-origin during development, the Next.js app uses an API base URL and FastAPI must explicitly allow that origin before real credentials are used.

## Error Handling

- Oversized uploads return `413 Payload too large`.
- Empty uploads continue to return `400`.
- Ingestion extraction errors continue to return `422`, but long-running work is isolated from the event loop.
- Embedding-dimension mismatch in pgvector fails startup with an operational error rather than deleting data.
- Next.js API calls should surface backend error messages in development and use concise user-facing errors in UI components.

## Testing

Backend tests:

- Existing pytest suite must run in CI.
- Add focused tests for byte-level upload limit behavior.
- Add a pgvector schema test or unit-level guard test for dimension mismatch once the store is refactored.
- Add a regression test that upload ingestion uses the threadpool boundary where practical.

Frontend tests:

- First milestone does not require full browser coverage.
- Add TypeScript typecheck and lint scripts to `onebrain-web`.
- Add a small smoke test or build check once the Next.js shell exists.

Manual verification:

- Run FastAPI locally.
- Run Next.js locally.
- Confirm the Next.js shell can call `/api/session/me`.
- Confirm the existing FastAPI static UI still works during transition.

## Rollout Plan

1. Commit this design spec.
2. Implement backend foundation changes first.
3. Add CI and make tests runnable in a clean environment.
4. Scaffold `onebrain-web` and wire the API base URL.
5. Add the first session-aware shell screen.
6. Verify both old and new UI paths.
7. Decide the next UI migration slice: chat or document library.

## Risks

- A rushed Next.js migration could duplicate auth or policy logic. Mitigation: the frontend only consumes FastAPI APIs and never reimplements authorization.
- Upload changes could alter existing behavior. Mitigation: preserve response models and add targeted tests.
- pgvector dimension mismatch behavior may block startup for an existing mismatched database. Mitigation: fail loudly with clear operator guidance rather than silently deleting customer data.
- Two UI stacks can create temporary maintenance overhead. Mitigation: keep the overlap short and migrate one screen at a time.

## Acceptance Criteria

- The architecture decision is documented and reviewed.
- CI runs the Python test suite.
- Uploads enforce size limits on actual bytes read.
- Upload ingestion no longer blocks the event loop directly.
- pgvector startup never drops customer chunk data.
- `onebrain-web` exists as a separate TypeScript/Next.js app.
- The Next.js app can call the FastAPI session endpoint through a typed or clearly bounded API client.
- Existing FastAPI UI remains available during the transition.
