"""Rotate a deployment off the shared-password demo accounts.

The demo accounts (email + the shared DEMO_PASSWORD) are convenient for a local
demo but are a standing credential exposure on any real deployment. This one-shot
script:

  1. ensures a real admin exists from ONEBRAIN_ADMIN_EMAIL / ONEBRAIN_ADMIN_PASSWORD, and
  2. deletes every shared-password demo account.

Run it once against the live database with the app's environment loaded, e.g.:

    export ONEBRAIN_DATABASE_URL=...           # the live Postgres URL
    export ONEBRAIN_VECTOR_STORE=pgvector
    export ONEBRAIN_ADMIN_EMAIL=you@nftgym.de
    export ONEBRAIN_ADMIN_PASSWORD='<a strong password>'
    python scripts/secure_users.py

It is idempotent: re-running it is safe.
"""

from __future__ import annotations

from app.config import get_settings
from app.deps import get_user_store
from app.users.seed import DEMO_USERS, seed_admin_from_env


def main() -> None:
    settings = get_settings()
    store = get_user_store()

    if seed_admin_from_env(store, settings):
        print(f"✓ admin bootstrapped: {settings.admin_email}")
    elif settings.admin_email:
        print(f"• admin already present: {settings.admin_email}")
    else:
        print("! ONEBRAIN_ADMIN_EMAIL / ONEBRAIN_ADMIN_PASSWORD not set — no admin created")

    removed = sum(1 for email, *_ in DEMO_USERS if store.delete_by_email(email))
    print(f"✓ removed {removed} demo account(s)")
    print(f"• users remaining: {store.count()}")

    if store.count() == 0:
        print("WARNING: no users remain — set ONEBRAIN_ADMIN_* and re-run so you can log in.")


if __name__ == "__main__":
    main()
