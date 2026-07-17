"""The secret-bundle contract — the single source of truth for the secrets a
provisioned box receives (P5-02/P5-03/P5-06).

The exchange bundle must supply a value for every ``${VAR}`` the Hetzner renderer
emits (verified against ``app/provisioning/hetzner/render.py`` ``_box_env`` +
``_module_env`` + ``render_env_files``). This module is PURE — no I/O, no config
read, no store — so it is trivially unit-testable and reused by both the customer
exchange path (P5-03) and the MC-box baking path (P5-06).

The bundle JSON is sealed/opened with the RE-READABLE
``OneTimeSecretCipher.seal_bundle``/``open_bundle`` pair (G1-4 / G2-1), never the
one-time envelope path — the bundle is read on first boot AND on every rotation
tick.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Dict, List

# Canonical bundle key order. render_dotenv emits keys in THIS order.
BUNDLE_KEYS = (
    "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "POSTGRES_WORKER_PASSWORD",
    "POSTGRES_ASSISTANT_PASSWORD", "POSTGRES_COMMUNICATION_PASSWORD", "REDIS_PASSWORD",
    "ONEBRAIN_FLEET_KEY", "ONEBRAIN_LLM_API_KEY",
    # ONEBRAIN_AUTH_SECRET signs session cookies. app/main.py FAILS CLOSED (RuntimeError,
    # refuses to boot) unless it is a strong (>=32-char) non-default secret, so a box that
    # bakes no value crashes onebrain-api on startup. A fresh per-box secrets.token_hex(32)
    # is minted into the bundle (MC + customer), making it a REQUIRED key with a min-length
    # floor (validate_bundle) — never provision a box whose api can't come up.
    "ONEBRAIN_AUTH_SECRET",
    # Distinct HMAC key for shared PostgreSQL login limits. It is deliberately
    # not reused for cookies so rotating either security boundary is isolated.
    "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
    # ONEBRAIN_ADMIN_EMAIL + ONEBRAIN_ADMIN_PASSWORD are the admin seed pair: seed.py
    # (seed_admin_from_env) creates a loginable admin at container start ONLY when BOTH
    # are non-empty. Without the email the box comes up with no admin and — SSH being
    # closed — is permanently unreachable, so the email is a REQUIRED key (fail closed).
    "ONEBRAIN_ADMIN_EMAIL", "ONEBRAIN_ADMIN_PASSWORD",
    # Legacy generic integration refs remain for older single-module boxes. New
    # renders use distinct app credentials so Assistant and Communication never
    # share an app-scoped service principal.
    "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_SPACE_ID",
    "ONEBRAIN_ASSISTANT_SERVICE_KEY",
    "ONEBRAIN_COMMUNICATION_SERVICE_KEY", "ONEBRAIN_COMMUNICATION_SPACE_ID",
    "UPDATE_BACKUP_KEY",
    "UPDATE_DESIRED_STATE_PUBLIC_KEYS",   # P5-02: the accepted wrapper-key SET (csv)
    "ONEBRAIN_DNS_TOKEN",                 # §5 lists it; empty for a normal customer box
    # BK3: the offsite-backup S3 credentials (Part 2). SECRETS -> they ride the sealed bundle,
    # never user-data (the box metadata endpoint would otherwise serve them). OPTIONAL: empty
    # when backups are off (backup_enabled=false), so a box with no bucket configured stays valid.
    "ONEBRAIN_BACKUP_S3_ACCESS_KEY", "ONEBRAIN_BACKUP_S3_SECRET_KEY",
)
# NOTE (G1-7): ONEBRAIN_PROVISIONING_CALLBACK_TOKEN is deliberately NOT in the bundle.
# It stays baked in user-data (box.env) so the cloud-init metadata-egress-block FAILURE
# callback (fail_cb) can authenticate BEFORE the bundle exchange has run. It is
# short-lived and used only during the provisioning/smoke window. See P5-03.

# Keys without which a fresh box cannot come up — a bundle missing/empty on any of
# these must fail closed (dispatch_failed), never provision a box that can't boot.
REQUIRED_KEYS = (
    "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "POSTGRES_WORKER_PASSWORD",
    "POSTGRES_ASSISTANT_PASSWORD", "POSTGRES_COMMUNICATION_PASSWORD", "REDIS_PASSWORD",
    "ONEBRAIN_FLEET_KEY", "ONEBRAIN_AUTH_SECRET", "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET",
    "ONEBRAIN_ADMIN_EMAIL", "ONEBRAIN_ADMIN_PASSWORD",
    "UPDATE_BACKUP_KEY",
)
# Extra floor for keys the app itself rejects when too short. ONEBRAIN_AUTH_SECRET must
# clear app/main.py's >=32-char cookie-secret guard, so a present-but-weak value is as
# fatal as a missing one — reject it here rather than boot-loop the box.
MIN_KEY_LENGTHS = {
    "ONEBRAIN_AUTH_SECRET": 32,
    "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET": 32,
}
# Legitimately empty in valid configs: no LLM key (local provider), no comm/assistant
# module (service key + space id), dormant desired-state emission (empty pubkey set),
# and DNS unmanaged (customer box). These are allowed to be empty/absent.
OPTIONAL_KEYS = tuple(k for k in BUNDLE_KEYS if k not in REQUIRED_KEYS)

# These credentials were introduced across multiple production hardening
# releases, so a long-lived encrypted bundle can be missing any of them. They
# are deliberately handled by an MC-side backfill: a customer host must never
# mint a password that the MC escrow cannot later serve again during rotation.
RUNTIME_DB_PASSWORD_KEYS = (
    "POSTGRES_APP_PASSWORD",
    "POSTGRES_WORKER_PASSWORD",
    "POSTGRES_ASSISTANT_PASSWORD",
    "POSTGRES_COMMUNICATION_PASSWORD",
)


def backfill_runtime_db_passwords(
    bundle: Dict[str, object],
    *,
    password_factory: Callable[[], str] | None = None,
) -> tuple[Dict[str, object], tuple[str, ...]]:
    """Return a copy with only missing restricted-runtime passwords added.

    This is intentionally idempotent: valid existing values are preserved, so
    it is safe to retry after a control-plane interruption. Callers must run it
    only on Mission Control after decrypting a stored bundle, then re-seal and
    bump the bundle epoch so the box re-fetches the authoritative values.
    """

    updated = dict(bundle)
    mint = password_factory or (lambda: secrets.token_urlsafe(32))
    added: list[str] = []
    for key in RUNTIME_DB_PASSWORD_KEYS:
        current = updated.get(key)
        if isinstance(current, str) and current.strip():
            continue
        if current is not None and not isinstance(current, str):
            raise ValueError(f"Secret bundle key {key} must be a string when present.")
        password = mint()
        if not isinstance(password, str) or len(password) < 32:
            raise ValueError(f"Generated {key} must be a string of at least 32 characters.")
        updated[key] = password
        added.append(key)
    return updated, tuple(added)


def render_dotenv(bundle: Dict[str, str]) -> str:
    """The ``/opt/onebrain/.env`` body: ``KEY=value`` lines for the keys PRESENT in
    the bundle, in canonical BUNDLE_KEYS order, LF-terminated, no quoting of secret
    values (compose interpolates ``${VAR}`` from this file verbatim). Keys absent
    from the bundle are skipped; extra keys not in BUNDLE_KEYS are ignored."""
    lines = [f"{key}={bundle[key]}" for key in BUNDLE_KEYS if key in bundle]
    return "".join(f"{line}\n" for line in lines)


def validate_bundle(bundle: Dict[str, str]) -> List[str]:
    """Return an ordered list of error strings for REQUIRED keys that are missing, empty,
    or below their MIN_KEY_LENGTHS floor (empty OPTIONAL keys — DNS token, space id,
    service/LLM keys, pubkey set — are allowed). An empty list means the bundle is safe
    to ship."""
    errors: List[str] = []
    for key in REQUIRED_KEYS:
        if key not in bundle:
            errors.append(f"missing required bundle key: {key}")
            continue
        value = str(bundle[key])
        if not value.strip():
            errors.append(f"empty required bundle key: {key}")
            continue
        min_len = MIN_KEY_LENGTHS.get(key)
        if min_len is not None and len(value) < min_len:
            errors.append(
                f"weak required bundle key: {key} must be at least {min_len} chars "
                "(app/main.py refuses to boot with a short cookie secret)")
    return errors
