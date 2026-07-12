"""Operator first-boot self-seed (P5-06 · G3-2).

The MC box boots with an EMPTY DB (the migration-sequence decision) and its full
`/opt/onebrain/.env` BAKED into cloud-init (G3-1) — including a freshly generated
`ONEBRAIN_FLEET_KEY`. For MC to be "self-enrolled and heartbeating to itself" it needs
an `mc` deployment ROW and an ACTIVE fleet key whose hash matches that baked key, IN
its OWN database.

Creating those normally takes a multi-step operator-admin runbook (`POST …/deployments`
then `POST …/deployments/mc/enroll`), which `scripts/bootstrap_mc.py` (pre-boot, on the
workstation, against an empty/remote DB) cannot do. So this runs INSIDE the MC app at
startup — idempotent, never fatal (mirrors the retention/reporter wiring) — so the single
bootstrap command yields a heartbeating MC with no manual enroll and no copied key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.controlplane.base import CustomerDeployment
from app.fleet.base import FleetKey
from app.fleet.keys import hash_secret, parse_fleet_key

_log = logging.getLogger("onebrain.fleet")


def seed_operator_self_deployment(settings, control_store, fleet_store) -> bool:
    """Idempotently seed MC's OWN deployment row + a fleet key matching the baked
    `ONEBRAIN_FLEET_KEY`, so the reporter's self-heartbeat authenticates against a hash
    that already exists. Returns whether it seeded this call (False = not operator_mode,
    already seeded, or no fleet key configured). Never raises fatally.

    The fleet key is REGISTERED from the raw baked token, not minted: `bootstrap_mc.py`
    already generated `fk_<id>_<secret>` and baked it as `ONEBRAIN_FLEET_KEY`, so the box
    parses that token and stores `hash_secret(secret)` keyed by `<id>` — the exact shape
    `_authenticate_fleet_key` verifies (parse -> get_key(id) -> verify_secret(secret))."""
    if not getattr(settings, "operator_mode", False):
        return False
    deployment_id = (getattr(settings, "deployment_id", "") or "mc").strip()
    try:
        if control_store.get_deployment(deployment_id) is not None:
            return False  # idempotent: a second boot with the row present is a no-op
        now_iso = datetime.now(timezone.utc).isoformat()
        control_store.create_deployment(CustomerDeployment(
            id=deployment_id,
            customer_name=deployment_id,
            deployment_type="dedicated_server",   # the MC box is a Hetzner box (WP5 semantics)
            release_ring="manual",                # MC is never auto-rolled
            update_policy="manual",
            current_version=getattr(settings, "build_version", "") or "",
            created_at=now_iso,
        ))
        # Register the baked fleet key (parse the token, store hash of the SECRET part —
        # the same value _authenticate_fleet_key verifies). No new key is minted: the box
        # already holds this exact token as ONEBRAIN_FLEET_KEY.
        parsed = parse_fleet_key(getattr(settings, "fleet_key", "") or "")
        if parsed is not None:
            key_id, secret = parsed
            fleet_store.create_key(FleetKey(
                id=key_id, key_hash=hash_secret(secret), deployment_id=deployment_id,
                label=f"operator-self-seed:{deployment_id}", created_at=now_iso))
        _log.info("Operator self-seed: created %r deployment row + fleet key.", deployment_id)
        return True
    except Exception as exc:  # never fatal — mirror the retention/reporter wiring
        _log.warning("Operator self-seed skipped: %s", exc)
        return False
