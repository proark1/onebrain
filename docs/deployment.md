# OneBrain Deployment

OneBrain deploys as three services plus Postgres:

- `onebrain-api`: Python/FastAPI API.
- `onebrain-admin-ui`: Next.js UI.
- `onebrain-workers`: Python background workers.
- `onebrain-db`: Postgres with pgvector.

The backend stays Python. Next.js renders the product UI and proxies browser
actions to the Python API.

## Railway Services

### `onebrain-api`

- Root directory: repository root.
- Builder: Dockerfile.
- Dockerfile path: `Dockerfile`.
- Health check path: `/health`.
- Public domain: yes.

The container starts with:

```bash
python -m app.deploy.start_api
```

In Postgres mode the launcher runs:

```bash
python -m alembic upgrade head
```

before starting `uvicorn`.

The baseline migration can adopt a compatible database created before Alembic
was introduced. It refuses an existing `chunks.embedding` vector dimension
mismatch instead of rewriting customer data.

### `onebrain-workers`

- Root directory: repository root.
- Builder: Dockerfile.
- Dockerfile path: `Dockerfile.worker`.
- Health check path: none.
- Public domain: no.

The container starts with:

```bash
python -m app.deploy.start_worker
```

In Postgres mode the launcher waits for the expected Alembic schema before it
starts processing jobs.

### `onebrain-admin-ui`

- Root directory: `onebrain-web`.
- Builder: Dockerfile.
- Dockerfile path: `Dockerfile`.
- Public domain: yes.

The container builds with `npm ci` and `npm run build`, then starts with:

```bash
npm run start
```

Next.js reads Railway's injected `PORT`.

## Required Variables

Set these on both `onebrain-api` and `onebrain-workers`:

```text
ONEBRAIN_VECTOR_STORE=pgvector
ONEBRAIN_DATABASE_URL=${{Postgres.DATABASE_URL}}
ONEBRAIN_MIGRATION_EMBEDDING_DIM=256
ONEBRAIN_AUTH_SECRET=<strong random secret, at least 32 chars>
ONEBRAIN_COOKIE_SECURE=true
ONEBRAIN_LLM_PROVIDER=litellm
ONEBRAIN_EMBEDDINGS_PROVIDER=litellm
GEMINI_API_KEY=<provider key>
ONEBRAIN_ADMIN_EMAIL=<admin email>
ONEBRAIN_ADMIN_PASSWORD=<strong admin password>
ONEBRAIN_PII_PHASE=synthetic
ONEBRAIN_REQUIRE_APPROVAL=false
ONEBRAIN_BLOCK_PUBLIC_ON_PII=true
```

Worker tuning variables:

```text
ONEBRAIN_WORKER_POLL_SECONDS=2
ONEBRAIN_WORKER_BATCH_SIZE=1
ONEBRAIN_JOB_MAX_ATTEMPTS=3
ONEBRAIN_SCHEMA_WAIT_SECONDS=60
ONEBRAIN_SCHEMA_WAIT_POLL_SECONDS=2
```

Set this on `onebrain-admin-ui`:

```text
ONEBRAIN_API_BASE_URL=https://<onebrain-api domain>
```

Use Railway's private service URL for `ONEBRAIN_API_BASE_URL` when available.
The browser still talks to same-origin Next.js routes; the Next.js server
forwards those calls to FastAPI.

## Smoke Checks

After deploy:

1. Open `https://<onebrain-api domain>/health`.
2. Open the Next.js domain and confirm it redirects to login or chat depending
   on session state.
3. Sign in with `ONEBRAIN_ADMIN_EMAIL` and `ONEBRAIN_ADMIN_PASSWORD`.
4. Upload a synthetic test document in Postgres mode.
5. Confirm the upload returns a job id or appears in the documents list after
   the worker processes it.
6. Check worker logs for `worker started` and `job succeeded`.

## Local Docker Checks

Build the API image:

```bash
docker build -t onebrain-api .
```

Build the worker image:

```bash
docker build -f Dockerfile.worker -t onebrain-workers .
```

Build the Next.js image:

```bash
docker build -t onebrain-admin-ui ./onebrain-web
```

Run API locally in memory mode:

```bash
docker run --rm -p 8000:8000 -e ONEBRAIN_AUTH_SECRET=local-secret-local-secret-local-secret onebrain-api
```

For local HTTP login, also set:

```bash
ONEBRAIN_COOKIE_SECURE=false
```
