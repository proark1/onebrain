"""Safe, idempotent preparation of an already-enrolled development gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from app.fleet.bootstrap_bundle import backfill_runtime_db_passwords, validate_bundle
from app.provisioning.bundles import resolve_module_composition
from app.provisioning.runs import OneTimeSecretCipher
from app.provisioning.service import PURPOSE_SCOPES
from app.servicekeys.base import ServiceKey, generate_key, hash_secret, parse_key, verify_secret


INTEGRATION_BUNDLE_KEYS = {
    "assistant": "ONEBRAIN_ASSISTANT_SERVICE_KEY",
    "communication": "ONEBRAIN_COMMUNICATION_SERVICE_KEY",
}


@dataclass(frozen=True)
class GatePreparationResult:
    deployment_id: str
    updated: bool
    secrets_epoch: int


def _scopes_for(purposes: tuple[str, ...]) -> tuple[str, ...]:
    scopes: list[str] = []
    for purpose in purposes:
        scopes.extend(PURPOSE_SCOPES.get(purpose, ()))
    return tuple(dict.fromkeys(scopes))


def _expected_key(app, account_id: str) -> dict:
    return {
        "tenant_id": account_id,
        "account_id": account_id,
        "app_id": app.app_id,
        "space_ids": tuple(f"sp_{account_id}_{key}" for key in app.space_keys),
        "purposes": tuple(app.purposes),
        "scopes": _scopes_for(app.purposes),
        "label": f"{app.display_name} integration",
    }


def _stored_key_is_valid(store, raw: object, expected: dict) -> bool:
    if not isinstance(raw, str):
        return False
    parsed = parse_key(raw.strip())
    if not parsed:
        return False
    key_id, secret = parsed
    stored = store.get(key_id)
    return bool(
        stored
        and stored.status == "active"
        and verify_secret(secret, stored.key_hash)
        and stored.tenant_id == expected["tenant_id"]
        and stored.account_id == expected["account_id"]
        and stored.app_id == expected["app_id"]
        and tuple(stored.space_ids) == expected["space_ids"]
        and tuple(stored.purposes) == expected["purposes"]
        and tuple(stored.scopes) == expected["scopes"]
    )


def prepare_existing_gate_bundle(
    *,
    deployment,
    provision_store,
    service_key_store,
    settings,
    optional_module_ids: tuple[str, ...],
) -> GatePreparationResult:
    """Repair a gate's escrowed bundle without exposing any raw secret.

    Existing valid integration credentials are retained. Missing or invalid
    Assistant/Communication credentials are minted hash-only in Mission Control,
    then their plaintext is placed solely in the encrypted box bundle. If bundle
    persistence fails, newly-created keys are revoked and the prior ciphertext is
    restored before the error is propagated.
    """
    bundle_row = provision_store.get_secret_bundle(deployment.id)
    if bundle_row is None:
        raise ValueError("development gate has no encrypted secret bundle")
    account_id = (bundle_row.account_id or deployment.account_id or "").strip()
    if not account_id:
        raise ValueError("development gate has no account identity")

    cipher = OneTimeSecretCipher(settings)
    try:
        decoded = json.loads(cipher.open_bundle(bundle_row.ciphertext))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("development gate secret bundle could not be opened") from exc
    if not isinstance(decoded, dict):
        raise ValueError("development gate secret bundle could not be opened")

    bundle, repaired = backfill_runtime_db_passwords(decoded)
    changed = bool(repaired)
    created_key_ids: list[str] = []
    composition = resolve_module_composition(optional_module_ids)
    apps = {app.app_id: app for app in composition.apps}

    try:
        for app_id, bundle_key in INTEGRATION_BUNDLE_KEYS.items():
            app = apps.get(app_id)
            if app is None:
                raise ValueError(f"development gate composition lacks {app_id}")
            expected = _expected_key(app, account_id)
            raw = bundle.get(bundle_key)
            if not _stored_key_is_valid(service_key_store, raw, expected):
                key_id, secret, plaintext = generate_key()
                service_key_store.create(ServiceKey(
                    id=key_id,
                    key_hash=hash_secret(secret),
                    tenant_id=expected["tenant_id"],
                    scopes=expected["scopes"],
                    label=expected["label"],
                    account_id=expected["account_id"],
                    app_id=expected["app_id"],
                    space_ids=expected["space_ids"],
                    purposes=expected["purposes"],
                ))
                created_key_ids.append(key_id)
                bundle[bundle_key] = plaintext
                changed = True

        communication_space = f"sp_{account_id}_customer_service"
        if bundle.get("ONEBRAIN_COMMUNICATION_SPACE_ID") != communication_space:
            bundle["ONEBRAIN_COMMUNICATION_SPACE_ID"] = communication_space
            changed = True
        if bundle.get("ONEBRAIN_SERVICE_KEY") != bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"]:
            bundle["ONEBRAIN_SERVICE_KEY"] = bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"]
            changed = True
        if bundle.get("ONEBRAIN_SPACE_ID") != communication_space:
            bundle["ONEBRAIN_SPACE_ID"] = communication_space
            changed = True

        errors = validate_bundle(bundle)
        if errors:
            raise ValueError(f"development gate secret bundle is invalid: {errors[0]}")
        if not changed:
            return GatePreparationResult(deployment.id, False, bundle_row.secrets_epoch)

        sealed = cipher.seal_bundle(json.dumps(bundle, separators=(",", ":"), sort_keys=True))
        provision_store.upsert_secret_bundle(replace(bundle_row, ciphertext=sealed))
        try:
            epoch = provision_store.bump_secrets_epoch(deployment.id)
        except Exception:
            provision_store.upsert_secret_bundle(bundle_row)
            raise
        return GatePreparationResult(deployment.id, True, epoch)
    except Exception:
        for key_id in created_key_ids:
            service_key_store.revoke(key_id)
        raise
