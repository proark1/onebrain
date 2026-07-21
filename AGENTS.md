# OneBrain

Multi-tenant, GDPR-conscious AI platform. FastAPI backend (`app/`), Next.js 16
console (`onebrain-web/`), Hetzner host assets (`deploy/`).
Railway is retired — it is not part of any environment. Do not re-add it.

## Repo map

| Path | What |
|---|---|
| `app/routers/` | HTTP surface (largest module — start here to trace a request) |
| `app/drive/` | Files, revisions, malware quarantine |
| `app/ai_employees/` | AI employee runtime, memory, missions |
| `app/provisioning/` | Hetzner broker client and box rendering |
| `app/controlplane/`, `app/fleet/` | Mission Control: releases, rollouts, heartbeats |
| `app/platform/`, `app/store/` | Account/space data layer (memory + postgres) |
| `app/config.py` | Every setting. `.env.example` documents the operator-facing subset |
| `onebrain-web/src/` | Console: `app/` routes, `components/*-panel.tsx`, `lib/` clients |
| `deploy/box/`, `deploy/broker/` | Host bundles |
| `docs/` | Current contracts and runbooks. `docs/archive/` is history, not instructions |

`onebrain-web/` is the only browser console. The legacy `app/static/` UI was
deleted — it carried a second copy of the API client, session auth and operator
console, and could never be reached on a real box anyway (see below). Do not add
a `/static` mount back.

**A box's config comes from exactly two places, and both are closed sets.**
`box.env` is rendered by `app/provisioning/hetzner/render.py` from an explicit
pair list; `/opt/onebrain/.env` is written by `render_dotenv` in
`app/fleet/bootstrap_bundle.py`, which emits only `BUNDLE_KEYS` and drops
everything else. A setting absent from both **cannot be configured on a
provisioned box** — boxes have no SSH. Adding a setting to `app/config.py` and
`.env.example` alone ships a knob nobody can turn.

## Checks — run these before calling work done

Backend, from the repo root:

```bash
python -m pytest -q
python scripts/verify_requirements_lock.py
```

Frontend, from `onebrain-web/`:

```bash
npm run lint && npm run typecheck && npm run test && npm run build
```

**If you touched a route, schema, or router registration, regenerate both
OpenAPI contracts or CI fails.** Both need a ≥32-char `ONEBRAIN_AUTH_SECRET` in
the environment, because they build the real app:

```bash
python scripts/export_openapi.py onebrain-web/src/lib/openapi.json --surface operator
python scripts/export_openapi.py onebrain-web/src/lib/openapi.customer.json --surface customer
```

Two ways a green local run still fails CI:

- **shellcheck is CI-only.** `tests/test_box_update_sh.py` *skips* it when the
  binary is absent, so `deploy/box/*.sh` edits look clean locally and fail in CI.
- **Windows:** pytest can exit non-zero with a `PermissionError` on
  `%TEMP%\pytest-of-*\pytest-current` *after* every test passed, and the crash
  suppresses the summary line. A deep temp path also trips the 260-char limit
  (`WinError 206`). Both go away with a short explicit `--basetemp=C:/obt`.

## CI gates that are easy to trip

- **Dependencies are hash-locked.** Edit `requirements.in` / `requirements-dev.in`,
  then regenerate with the exact `uv pip compile` command in the lock header.
  Never hand-edit a `.txt`.
- **Third-party GitHub Actions must be pinned to a full 40-hex commit SHA.**
- **Container images must be pinned to `@sha256:…`** — in `Dockerfile`,
  `Dockerfile.worker`, `onebrain-web/Dockerfile`, and workflow `image:` keys.
- **A secret-pattern scan runs over the whole tree.** Never commit anything
  matching `sk-…`, `ghp_…`, or `key/secret/token/password = "…"`.
- Migrations: `app/db/schema.py` pins `REQUIRED_ALEMBIC_REVISION`; bump it with
  the migration.

## Architecture invariants — do not weaken

Topology: super admin → Mission Control (`mc.onlyonebrain.com`) → infrastructure
broker → development gate and customer boxes, each a full isolated suite.

- **Mission Control** is the super-admin control plane. It holds deployment
  metadata, release manifests, approval state and fleet health — never customer
  content. It never auto-deploys a release to customers; an operator chooses.
- **Customer boxes and the dev gate** must not reach `/api/fleet`,
  `/api/operator`, `/api/provisioning`, `/api/rollouts`. Two layers enforce it:
  `Settings.is_operator_surface` decides whether those routers mount at all
  (`app/main.py`), and the rendered proxy deny-list in
  `app/provisioning/hetzner/render.py`. A new control-plane route must be
  covered by both.
- **The broker** holds the Hetzner API token so no other host does. It enforces
  approved regions, sizes, images, firewall shape, DNS zone and server cap, and
  exposes no destructive operation. Do not reintroduce an in-process broker or
  loosen the production guard to make provisioning easier.
- **Releases are digest-pinned and signed.** The production signing key stays
  offline; a development key cannot approve a customer release.
- **`box.env` is `.`-sourced by host scripts**, so every rendered value must be
  shell-safe: use `_shell_kv`, never `_kv` (`app/provisioning/hetzner/render.py`).
  An unquoted multi-word value is parsed as a command and kills the bootstrap
  before any secret is fetched — leaving a dead box that Mission Control still
  reports healthy. `deploy/box/onebrain_dotenv.sh` is for `.env` only; it
  deliberately does not expand `${VAR}`.
- Never print, commit, render, or return in an API response: an API token,
  private key, client credential, bootstrap password, service-key plaintext, or
  customer content.

## Shipping

When a task is complete, ship it unless told otherwise: run the checks above,
stage only task-related files, commit, push a branch, and open a PR. Never push
to `main` directly — it bypasses CI.

A green PR is not a merged PR: review threads (including the
`gemini-code-assist` bot's) must be resolved first, and auto-merge is off.

Do not ship if checks fail, there are merge conflicts, unrelated local changes
are present, secrets are detected, or the request was for review or planning only.
