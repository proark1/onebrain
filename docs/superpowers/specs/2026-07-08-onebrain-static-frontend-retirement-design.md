# OneBrain Static Frontend Retirement Design

## Goal

Retire the old FastAPI-served static UI as the default browser experience now
that the Next.js console covers chat, documents, spaces, privacy, operator, and
workspace selection and is deployed as its own Railway service.

## Design

FastAPI remains the Python API service. It serves `/api/*`, `/health`, and
OpenAPI docs. The root path `/` becomes API-first:

- If `ONEBRAIN_ADMIN_UI_URL` is set, `/` redirects to that Next.js URL.
- Otherwise `/` returns a small JSON status payload with links to `/docs` and
  `/health`.

The legacy files under `app/static` are not deleted in this slice. They are
mounted only when `ONEBRAIN_LEGACY_STATIC_UI_ENABLED=true`, which keeps a local
debug escape hatch without leaving two product UIs enabled in production.

## Configuration

- `ONEBRAIN_ADMIN_UI_URL`: optional Next.js console URL for API-root redirects.
- `ONEBRAIN_LEGACY_STATIC_UI_ENABLED`: defaults to `false`; set to `true` only
  for local debugging.

## Testing

Add FastAPI `TestClient` coverage for:

- default `/` JSON response,
- configured `/` redirect,
- static UI disabled by default,
- static UI enabled only by the explicit legacy flag.
