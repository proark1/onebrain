"""Fleet auto-enrollment — a deployment self-joins the fleet by minting a
per-deployment fleet key and receiving the three env vars its reporter needs.

`fleet_enrollment_vars` is pure (the exact env a reporting deployment sets).
`mint_deployment_fleet_key` mints a key and stores only its hash (reusing the
service-key hashing) — the plaintext token is returned once, to be delivered to
the deployment through its rendered environment (at provision time, or an operator enroll call).
"""

from __future__ import annotations

from typing import Dict, Tuple

from app.fleet.base import FleetKey
from app.fleet.keys import generate_fleet_key, hash_secret


def fleet_enrollment_vars(fleet_public_url: str, deployment_id: str, fleet_token: str) -> Dict[str, str]:
    """The env a deployment sets to start reporting to Mission Control. The reporter
    (app/fleet/reporter.py) only activates when all three are present."""
    return {
        "ONEBRAIN_FLEET_URL": fleet_public_url.rstrip("/"),
        "ONEBRAIN_DEPLOYMENT_ID": deployment_id,
        "ONEBRAIN_FLEET_KEY": fleet_token,
    }


def mint_deployment_fleet_key(fleet_store, deployment_id: str, *, label: str, now_iso: str) -> Tuple[str, str]:
    """Mint a fleet key bound to a deployment; persist only the hash. Returns
    (key_id, plaintext_token). The token is shown once."""
    key_id, secret, token = generate_fleet_key()
    fleet_store.create_key(FleetKey(
        id=key_id, key_hash=hash_secret(secret), deployment_id=deployment_id,
        label=label or f"enrollment:{deployment_id}", created_at=now_iso,
    ))
    return key_id, token
