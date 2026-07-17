# OneBrain

OneBrain is a general, GDPR-conscious AI platform for organizations. A customer
deployment provides an isolated workspace for knowledge, AI communication, and
personal-assistant capabilities across industries and customer brands.

## Deployment model

Production is hosted on dedicated Hetzner infrastructure.

```text
Mission Control (private super-admin)
        |
        | authenticated, metadata-only control requests
        v
Hetzner broker (private infrastructure authority)
        |
        | bounded server, DNS, firewall, and volume actions
        v
Development gate / isolated customer deployments
```

- **Mission Control** is used only by global super-admins. It tracks deployment
  metadata, release versions, health, and explicit rollout decisions. It does
  not handle customer content.
- **Hetzner broker** is a private service that holds the Hetzner API token. It
  accepts only authenticated, validated requests from Mission Control and has
  no customer UI or data access.
- **Development gate** is a full, dummy-data customer-shaped instance used for
  developing and testing the OneBrain core, AI Communication, and Personal
  Assistant before any release can be selected for customers.
- **Customer deployments** are isolated. They have no fleet/control-plane
  interface and cannot access data from any other customer or project.

Customer updates are always explicit: validate a signed release on the dev
gate, choose the customer in Mission Control, create a recoverable rollout,
and verify health before marking it complete. Nothing advances customers
automatically.

Production can run more than one API replica. Login failures are therefore
limited through the shared PostgreSQL store, and background jobs and streaming
AI turns use fenced, expiring leases so a stopped replica cannot overwrite a
new owner. The operational activation and recovery checks are in the
[production runbook](docs/production-activation-runbook.md).

The current detailed guidance lives in [docs/README.md](docs/README.md).

## Local development

Prerequisites: Python 3.12, Docker, and Node.js 20+ for `onebrain-web`.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r requirements-dev.txt
copy .env.example .env
pytest -q
```

The default `.env` values are for local, synthetic development only. Use
Postgres with pgvector, RLS, strong secrets, and HTTPS for any real deployment.
`requirements.txt` and `requirements-dev.txt` are hash-locked; edit their
matching `.in` file deliberately, regenerate with the command in the lock-file
header, and run `python scripts/verify_requirements_lock.py` before committing.

## Repository layout

- `app/` — OneBrain API, worker, storage, and deployment control-plane code.
- `onebrain-web/` — optional web client.
- `deploy/box/` — customer-shaped Hetzner deployment assets and gate reporter.
- `docs/` — current documentation, technical contracts, and the historical
  archive.

## Safety boundaries

- Keep the Hetzner API token only on the private broker host.
- Keep release-signing private keys offline; deployment hosts receive public
  verification keys only.
- Use the shared PostgreSQL login limiter in every production API replica; do
  not trust client-supplied forwarding headers without an explicit proxy trust
  boundary.
- Use dummy data on the development gate.
- Do not expose Mission Control or fleet APIs to customer deployments.
- Do not treat a two-person teardown review as permission to delete a customer
  environment: live teardown is disabled.
- Do not treat archived documentation as operational instructions.
