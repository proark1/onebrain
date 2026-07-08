# onebrain-web

Next.js shell for the OneBrain product UI.

The Python/FastAPI service remains the source of truth for auth, access control, retrieval, ingestion, privacy operations, and service keys. This app calls that API through a typed boundary in `src/lib/onebrain-api.ts`.

## Routes

- `/chat` - streaming assistant chat backed by the FastAPI retrieval and conversation APIs.
- `/documents` - document library, upload, and pending-review workflow backed by the FastAPI document APIs.
- `/` - entry point that checks the API/session and redirects signed-in users to `/chat`.

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

## API Schema

Export the FastAPI OpenAPI schema into the web app with:

```bash
npm run openapi
```
