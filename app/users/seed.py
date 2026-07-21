"""Seed demo user accounts (all NFT Gym) so you can log in and try each tier.

Every demo account shares one password (DEMO_PASSWORD) — fine for a synthetic
demo, obviously not for real accounts.
"""

from __future__ import annotations

import logging
import uuid

from app.auth.passwords import hash_password, verify_password
from app.users.base import User

_log = logging.getLogger("onebrain")

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
    credential, so first-login rotation is the right default either way.

    Creating with the flag only helps boxes provisioned afterwards, so an
    existing row that still holds the env password is repaired too — see
    `_require_rotation_if_unrotated`.
    """
    email = (settings.admin_email or "").strip().lower()
    password = settings.admin_password or ""
    if not email or not password:
        return 0
    existing = store.get_by_email(email)
    if existing:
        _require_rotation_if_unrotated(store, existing, password)
        return 0
    store.create(User(
        id=uuid.uuid4().hex, email=email, display_name="Administrator",
        password_hash=hash_password(password), tenant_id=tenant,
        role_id="admin", location="all", must_change_password=True,
    ))
    return 1


def _require_rotation_if_unrotated(store, user, password: str) -> bool:
    """Close the permanent-credential hole on a box provisioned before the flag.

    Setting must_change_password at creation is forward-only: every box already
    provisioned kept an admin row without it, so the one-time owner password is
    still a permanent full-admin login there — and it stays readable from the
    box's .env and re-fetchable from Mission Control's secret bundle. Those are
    exactly the deployments holding customer data, and nothing else repairs them.

    The flag is set ONLY when the stored hash still verifies against the env
    password, which is what distinguishes "never rotated" from "the owner chose
    their own password and this variable is now stale". An owner who has rotated
    is never disturbed.

    Rotation is required, not forced-by-lockout: `resolve_principal` 403s a
    must-change principal out of everything but the change-password allowlist,
    and it does so per request — so this takes hold on sessions already open,
    not just at the next login. Returns True when it changed something.
    """
    if getattr(user, "must_change_password", False):
        return False
    if not verify_password(password, user.password_hash):
        return False
    # Same hash, flag flipped: this must not double as a password reset. An
    # operator who is mid-incident needs the credential they have to keep
    # working long enough to rotate it.
    store.update_password(user.id, user.password_hash, must_change_password=True)
    _log.warning(
        "Admin %s still held the provisioning password; rotation is now required.",
        user.email,
    )
    return True
