# onebrain-web

Next.js shell for the OneBrain product UI.

The Python/FastAPI service remains the source of truth for auth, access control, retrieval, ingestion, privacy operations, and service keys. This app calls that API through a typed boundary in `src/lib/onebrain-api.ts`.

## Run

```bash
npm install
npm run dev
```

By default the app calls `http://127.0.0.1:8000`. Override with:

```bash
ONEBRAIN_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

## API Schema

Export the FastAPI OpenAPI schema into the web app with:

```bash
npm run openapi
```
