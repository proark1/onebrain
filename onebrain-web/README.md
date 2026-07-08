# onebrain-web

Next.js shell for the OneBrain product UI.

The Python/FastAPI service remains the source of truth for auth, access control, retrieval, ingestion, privacy operations, and service keys. This app calls that API through a typed boundary in `src/lib/onebrain-api.ts`.

## Routes

- `/chat` - streaming assistant chat backed by the FastAPI retrieval and conversation APIs.
- `/documents` - document library, upload, and pending-review workflow backed by the FastAPI document APIs.
- `/spaces` - admin account, space, app-installation, access-check, and audit workflows backed by the FastAPI platform APIs.
- `/privacy` - admin privacy center for account/space export and erase operations backed by the FastAPI privacy APIs.
- `/operator` - admin provisioning, customer readiness, release planning, service-key revoke, and rollout workflows backed by the FastAPI operator/provisioning APIs.
- `/` - entry point that checks the API/session and redirects signed-in users to `/chat`.

Admins see a compact workspace selector when the Python platform store contains an account matching their session tenant. The selected account/space scope is sent to chat, conversations, documents, upload, and review calls.

The privacy center intentionally loads all platform accounts for admins because export and erasure are account-level operations. The Python backend still performs authorization, scope validation, audit writes, export assembly, and deletion.

The spaces admin route uses the same backend ownership boundary: Next.js renders the controls, while FastAPI validates account/space/app records, records audit events, and decides access checks.

The operator route keeps provisioning and rollout authority in FastAPI. Next.js renders the control plane and forwards actions through the local proxy.

## Run

```bash
npm install
npm run dev
```

By default the app calls `http://127.0.0.1:8000`. Override with:

```bash
ONEBRAIN_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

For local HTTP login, run the FastAPI API with `ONEBRAIN_COOKIE_SECURE=false` so the browser can send the session cookie to the Next.js dev server.

## Deploy

Build this directory as its own Railway service with `onebrain-web/Dockerfile`.
Set `ONEBRAIN_API_BASE_URL` to the deployed FastAPI API URL, preferably the
private Railway service URL when available.

## API Schema

Export the FastAPI OpenAPI schema into the web app with:

```bash
npm run openapi
```
