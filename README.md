# onebrain

An access-gated, GDPR-minded internal AI brain for **NFT Gym**. Employees upload
company documents; the brain answers questions **only from what the asker's role
is allowed to see**. The language model never sees a chunk the caller isn't
entitled to — access is enforced in code and the datastore, not in a prompt.

This is the **Stage 0 prototype**: it runs online, cheap, with **no API keys and
no database**, on synthetic data. Every moving part sits behind an interface, so
moving to a self-hosted EU stack later is a config change, not a rewrite.

> ⚠️ Do not load real member or employee PII into this prototype. Test on
> synthetic / anonymized data until it's on EU infrastructure with a DPIA.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

Run the tests (they prove the access boundary holds):

```bash
pytest -q
```

## Try the gating

Switch roles in the sidebar and watch the document list — and the answers —
change:

| Ask this | public | front-desk | HR | finance |
|---|:--:|:--:|:--:|:--:|
| "What are the opening hours?" | ✅ | ✅ | ✅ | ✅ |
| "How do I handle a refund?" | — | ✅ | — | — |
| "What are the trainer salary bands?" | — | — | ✅ | — |
| "What was Q1 revenue by location?" | — | — | — | ✅ |

A "—" means the brain replies that you don't have access — because the chunks
were filtered out before retrieval, not because the model was asked to be coy.

## How it works

```
upload ─▶ extract ─▶ chunk ─▶ label + embed ─▶ vector store   (once per file)
ask ─▶ resolve role ─▶ permission filter ─▶ top-k search ─▶ LLM ─▶ answer  (per question)
```

Only the top-k relevant chunks reach the model, so per-question token cost stays
flat no matter how big the corpus grows.

## Architecture (modular by design)

| Concern | Interface | Local (default) | Production swap |
|---|---|---|---|
| Identity | `auth/principal.py` | role via header | OIDC / JWT |
| Access rule | `security/policy.py` | ABAC filter | + OpenFGA/OPA |
| Embeddings | `embeddings/base.py` | hashing (no key) | LiteLLM |
| Vector store | `store/base.py` | in-memory + pickle | pgvector / Qdrant |
| LLM | `llm/base.py` | extractive (no key) | LiteLLM (EU-sovereign) |

## Go to production

Set these in `.env` (see `.env.example`) — no code changes:

```bash
ONEBRAIN_EMBEDDINGS_PROVIDER=litellm
ONEBRAIN_LLM_PROVIDER=litellm
ONEBRAIN_VECTOR_STORE=pgvector
ONEBRAIN_DATABASE_URL=postgresql://…
# plus: pip install litellm "psycopg[binary]" pgvector  and the provider API key
```

## Deploy to Railway

Railway builds the `Dockerfile` automatically (config in `railway.json`).

1. **New Project → Deploy from GitHub repo** → pick this repo. It builds and
   boots straight away in **local mode** (no keys) with the seeded demo data.
2. **Use Gemini** — in the service's **Variables**, add:
   ```
   ONEBRAIN_LLM_PROVIDER=litellm
   ONEBRAIN_EMBEDDINGS_PROVIDER=litellm
   GEMINI_API_KEY=your-google-ai-studio-key
   ```
   (Model names default to `gemini/gemini-2.5-flash` and
   `gemini/text-embedding-004` — override with `ONEBRAIN_LITELLM_MODEL` /
   `ONEBRAIN_LITELLM_EMBEDDING_MODEL` if you like.)
3. **Persist uploads** (optional) — add a **Volume** mounted at `/data`.
   Without it, uploaded docs reset on each redeploy (the seed data always
   reloads). The container already points `ONEBRAIN_DATA_DIR` at `/data`.
4. Pick an **EU region** and keep to **synthetic data** until the compliance
   groundwork is done.

Railway injects `$PORT`; the container binds to it. Health check: `/health`.

When you outgrow the single-instance memory store (multiple replicas, real
persistence), switch to Postgres: add a Postgres plugin and set
`ONEBRAIN_VECTOR_STORE=pgvector` + `ONEBRAIN_DATABASE_URL` — no code change.

## Layout

```
app/
  auth/         identity + roles
  security/     the access-control policy (the boundary)
  embeddings/   pluggable embedder
  store/        pluggable vector store
  llm/          pluggable model + RAG prompt
  ingest/       extract → chunk → label → embed
  retrieval/    the gateway: filter → top-k → generate
  routers/      HTTP endpoints
  static/       the UI (vanilla ES modules, no build step)
tests/          access-boundary + retrieval tests
```
