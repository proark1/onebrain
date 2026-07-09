# OneBrain Deployment

OneBrain deploys as three services plus Postgres:

- `onebrain`: Python/FastAPI API.
- `onebrain-admin-ui`: Next.js UI.
- `onebrain-workers`: Python background workers.
- `Postgres`: Postgres with pgvector.

The backend stays Python. Next.js renders the product UI and proxies browser
actions to the Python API.

## Railway Services

### `onebrain`

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

Set these on both `onebrain` and `onebrain-workers`:

```text
ONEBRAIN_VECTOR_STORE=pgvector
ONEBRAIN_ENVIRONMENT=production
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
ONEBRAIN_RETRIEVAL_MIN_SCORE=0.05
ONEBRAIN_RLS_ENFORCED=true
```

`ONEBRAIN_MIGRATION_EMBEDDING_DIM` must match the embedding provider or the
existing `chunks.embedding` column. The current Railway database uses
`3072`; a fresh local-hashing database can use the default `256`.

`ONEBRAIN_RETRIEVAL_MIN_SCORE` filters weak vector matches before they reach the
LLM. Tune it for the active embedding model after checking answer quality on
representative customer questions.

Production-like environments (`ONEBRAIN_ENVIRONMENT=staging` or `production`)
refuse to start unless `ONEBRAIN_VECTOR_STORE=pgvector`,
`ONEBRAIN_DATABASE_URL` is set, and `ONEBRAIN_RLS_ENFORCED=true`.

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
ONEBRAIN_API_BASE_URL=http://${{onebrain.RAILWAY_PRIVATE_DOMAIN}}:8080
```

Railway injects `PORT=8080` in the Python API container. Use the private
Railway hostname plus that port for `ONEBRAIN_API_BASE_URL`. The browser still
talks to same-origin Next.js routes, including `/login`; the Next.js server
forwards those calls to FastAPI.

External customer provisioning variables for `onebrain`:

```text
ONEBRAIN_GITHUB_OWNER=<github owner>
ONEBRAIN_GITHUB_REPO=<repo>
ONEBRAIN_GITHUB_WORKFLOW=provision-customer.yml
ONEBRAIN_GITHUB_REF=main
ONEBRAIN_GITHUB_DISPATCH_TOKEN=<token allowed to dispatch the workflow>
ONEBRAIN_PROVISIONING_CALLBACK_KEY_ID=callback-v1
ONEBRAIN_PROVISIONING_CALLBACK_KEY_HASH=<sha256 hash from app.provisioning.runs.hash_callback_secret>
ONEBRAIN_SECRET_ENCRYPTION_KEY=<Fernet key or 32-byte hex>
ONEBRAIN_SECRET_ENCRYPTION_KEY_VERSION=v1
ONEBRAIN_BOOTSTRAP_SECRET_TTL_SECONDS=3600
```

Set these as GitHub Actions repository secrets:

```text
ONEBRAIN_PROVISIONING_CALLBACK_KEY=<plaintext callback key matching the hash>
RAILWAY_API_TOKEN=<Railway account/workspace token that can create projects>
RAILWAY_TOKEN=<fallback Railway token if RAILWAY_API_TOKEN is not used>
RAILWAY_WORKSPACE=<optional Railway workspace id or name>
GEMINI_API_KEY=<optional provider key for provisioned stacks>
ONEBRAIN_MIGRATION_EMBEDDING_DIM=<optional override, defaults to 3072 with Gemini>
ASSISTANT_SERVICE_IMAGE=<optional Docker image for assistant-service>
COMMUNICATION_API_IMAGE=<optional Docker image for communication-api>
COMMUNICATION_WIDGET_IMAGE=<optional Docker image for communication-widget>
COMMUNICATION_VOICE_IMAGE=<optional Docker image for communication-voice>
COMMUNICATION_WORKERS_IMAGE=<optional Docker image for communication-workers>
```

Use workflow dry-run mode for the first contract test. Real Railway provisioning
runs only when `dry_run=false` and the required GitHub secrets are present. The
workflow creates the OneBrain API, worker, admin UI, and Postgres services from
this repo. Bundle module services are created too; when an optional image secret
is absent, the service is recorded as pending deployable code in the
provisioning run result payload.

Optional API root handoff:

```text
ONEBRAIN_ADMIN_UI_URL=https://<onebrain-admin-ui domain>
ONEBRAIN_LEGACY_STATIC_UI_ENABLED=false
```

When `ONEBRAIN_ADMIN_UI_URL` is set, the API service root redirects to the
Next.js console. The old FastAPI static UI is disabled unless
`ONEBRAIN_LEGACY_STATIC_UI_ENABLED=true`.

## Smoke Checks

After deploy:

1. Open `https://<onebrain-api domain>/health`.
2. Open the Python API root and confirm it returns API JSON or redirects to the
   configured Next.js console.
3. Open the Next.js domain and confirm it shows the signed-out state or chat
   depending on session state.
4. Sign in with `ONEBRAIN_ADMIN_EMAIL` and `ONEBRAIN_ADMIN_PASSWORD`.
5. Upload a synthetic test document in Postgres mode.
6. Confirm the upload returns a job id or appears in the documents list after
   the worker processes it.
7. As an admin, call `GET /api/operator/observability` and confirm it returns
   runtime, retrieval, storage, service-key, and job queue sections without
   customer content.
8. Provision a test customer with brand colors and confirm the operator
   customer row shows the expected swatches.
9. Mint a temporary service key, call `/api/service/capabilities` and
   `/api/service/brand-theme`, rotate the
   key, and confirm the old key receives `401` while the new key works.
10. Check worker logs for `worker started` and `job succeeded`.

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
