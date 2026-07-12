"""Per-module env-var + health-probe manifests (Hetzner P0, WP7).

Pure-data contracts: the drift tripwire against MODULE_IDS, the pinned probe
ports, and the env validator that closes the SERVICE_KEY/SPACE_ID gap
provision-customer.yml left open.
"""

from __future__ import annotations

import pytest

from app.controlplane.base import MODULE_IDS
from app.module_manifest import (
    MODULE_ENV_REQUIREMENTS, MODULE_HEALTH_PROBES, parse_local_modules, validate_module_env,
)


def test_manifests_cover_exactly_module_ids():
    # The drift tripwire: adding/removing a module id must touch both manifests.
    assert set(MODULE_HEALTH_PROBES) == MODULE_IDS
    assert set(MODULE_ENV_REQUIREMENTS) == MODULE_IDS


def test_probe_ports_match_pinned_expectations():
    # C6 — this is a PIN, not a proof: it re-asserts the spec table's constants
    # and only prevents accidental in-repo edits. The real sources of truth are
    # the three source repos' Dockerfiles/healthcheck; cross-repo drift is
    # caught by the P1 provisioner checklist (verify ports against the actually
    # built images), not by this test.
    expected_ports = {
        "onebrain-api": 8000,
        "onebrain-admin-ui": 3000,
        "assistant-service": 8000,
        "communication-api": 4000,
        "communication-widget": 5174,
        "communication-voice": 4100,
        "communication-workers": 4200,
    }
    for module_id, port in expected_ports.items():
        probe = MODULE_HEALTH_PROBES[module_id]
        assert probe.kind == "http" and probe.port == port
    # onebrain-workers has no listener: no claim is made, ever.
    assert MODULE_HEALTH_PROBES["onebrain-workers"].kind == "none"
    # Never the Railway-masked :8080 wiring from provision-customer.yml.
    assert all(probe.port != 8080 for probe in MODULE_HEALTH_PROBES.values())


def test_comm_requires_service_key_and_space_id():
    # The gap that let comm silently run in local-brain fallback: the pair
    # provision-customer.yml never set must be named as missing.
    missing = validate_module_env("communication-api", {})
    assert "ONEBRAIN_SERVICE_KEY" in missing
    assert "ONEBRAIN_SPACE_ID" in missing

    # Empty-string values count as missing.
    blank = {name: "" for name in MODULE_ENV_REQUIREMENTS["communication-api"]}
    assert set(validate_module_env("communication-api", blank)) == set(
        MODULE_ENV_REQUIREMENTS["communication-api"])

    populated = {name: "x" for name in MODULE_ENV_REQUIREMENTS["communication-api"]}
    assert validate_module_env("communication-api", populated) == []


def test_unknown_module_raises():
    with pytest.raises(KeyError):
        validate_module_env("not-a-module", {})


def test_parse_local_modules_drops_unknown():
    assert parse_local_modules("communication-api, ghost-module ,onebrain-workers") == [
        "communication-api", "onebrain-workers"]
    assert parse_local_modules("") == []
    assert parse_local_modules("all-unknown,also-unknown") == []
