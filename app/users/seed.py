"""Seed demo user accounts (all NFT Gym) so you can log in and try each tier.

Every demo account shares one password (DEMO_PASSWORD) — fine for a synthetic
demo, obviously not for real accounts.
"""

from __future__ import annotations

import uuid

from app.auth.passwords import hash_password
from app.users.base import User

DEMO_PASSWORD = "onebrain2026"

# (email, display name, role_id, location)
DEMO_USERS = [
    ("admin@nftgym.de", "Alex Admin (DPO)", "admin", "all"),
    ("hr@nftgym.de", "Hanna HR", "hr", "all"),
    ("finance@nftgym.de", "Fatima Finance", "finance", "all"),
    ("marketing@nftgym.de", "Marek Marketing", "marketing", "all"),
    ("manager.munich@nftgym.de", "Max Manager (Munich)", "location_manager", "munich"),
    ("frontdesk.munich@nftgym.de", "Frida Front-desk (Munich)", "front_desk", "munich"),
    ("trainer.munich@nftgym.de", "Tomas Trainer (Munich)", "trainer", "munich"),
    ("frontdesk.berlin@nftgym.de", "Bruno Front-desk (Berlin)", "front_desk", "berlin"),
]


def seed_users_if_empty(store, tenant: str = "nft_gym") -> int:
    if store.count() > 0:
        return 0
    pw_hash = hash_password(DEMO_PASSWORD)
    for email, name, role_id, location in DEMO_USERS:
        store.create(User(
            id=uuid.uuid4().hex, email=email, display_name=name, password_hash=pw_hash,
            tenant_id=tenant, role_id=role_id, location=location,
        ))
    return len(DEMO_USERS)


def seed_admin_from_env(store, settings, tenant: str = "nft_gym") -> int:
    """Idempotently ensure a real admin from ONEBRAIN_ADMIN_EMAIL/PASSWORD.

    This is the safe production login path — a per-deployment credential, never a
    shared/default one. Returns 1 if it created the account, 0 otherwise.

    The account is created with must_change_password=True. On a provisioned box
    ONEBRAIN_ADMIN_PASSWORD *is* the one-time owner password minted by Mission
    Control (which sets the same flag on its own row), so leaving it clear would
    install a "one-time" credential as a permanent admin login — one that stays
    recoverable from the box's .env and from MC's re-fetchable secret bundle.
    Even when an operator sets the variable by hand it is a plaintext on-disk
    credential, so first-login rotation is the right default either way. This
    only affects accounts at creation time; existing rows are untouched.
    """
    email = (settings.admin_email or "").strip().lower()
    password = settings.admin_password or ""
    if not email or not password:
        return 0
    if store.get_by_email(email):
        return 0
    store.create(User(
        id=uuid.uuid4().hex, email=email, display_name="Administrator",
        password_hash=hash_password(password), tenant_id=tenant,
        role_id="admin", location="all", must_change_password=True,
    ))
    return 1
