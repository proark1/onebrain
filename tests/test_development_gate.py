from types import SimpleNamespace

import pytest

from app.controlplane.development_gate import (
    CURRENT_MODULE_SET_INVALID,
    DEVELOPMENT_GATE_CORE_MODULE_IDS,
    DEVELOPMENT_GATE_MODULE_IDS,
    LEGACY_CORE_GATE_REPLACEMENT_REQUIRED,
    TARGET_MODULE_SET_INVALID,
    is_current_replacement_bootstrap_failure,
    validate_module_transition,
    verify_reported_modules,
)


def test_module_transition_accepts_exact_full_to_full():
    assert validate_module_transition(
        DEVELOPMENT_GATE_MODULE_IDS,
        DEVELOPMENT_GATE_MODULE_IDS,
    ) == ""


@pytest.mark.parametrize(
    "current",
    [
        frozenset(),
        frozenset({"onebrain-api"}),
        DEVELOPMENT_GATE_CORE_MODULE_IDS | {"foreign-service"},
        DEVELOPMENT_GATE_MODULE_IDS - {"communication-workers"},
    ],
)
def test_module_transition_rejects_partial_or_foreign_current_set(current):
    assert (
        validate_module_transition(current, DEVELOPMENT_GATE_MODULE_IDS)
        == CURRENT_MODULE_SET_INVALID
    )


def test_module_transition_requires_replacement_for_legacy_core_gate():
    assert (
        validate_module_transition(
            DEVELOPMENT_GATE_CORE_MODULE_IDS,
            DEVELOPMENT_GATE_MODULE_IDS,
        )
        == LEGACY_CORE_GATE_REPLACEMENT_REQUIRED
    )


def test_module_transition_requires_exact_full_target():
    assert (
        validate_module_transition(
            DEVELOPMENT_GATE_MODULE_IDS,
            DEVELOPMENT_GATE_MODULE_IDS - {"communication-workers"},
        )
        == TARGET_MODULE_SET_INVALID
    )


def _replacement_promotion(**overrides):
    values = {
        "release_version": "candidate-1",
        "state": "dev_failed",
        "gate_deployment_id": "gate-1",
        "failure_reason": "dev_preflight_failed",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _replacement_event(**overrides):
    values = {
        "release_version": "candidate-1",
        "action": "dev_preflight_failed",
        "from_state": "dev_deploying",
        "to_state": "dev_failed",
        "note": LEGACY_CORE_GATE_REPLACEMENT_REQUIRED,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_current_replacement_bootstrap_failure_requires_exact_latest_event():
    assert is_current_replacement_bootstrap_failure(
        _replacement_promotion(),
        [_replacement_event()],
        gate_deployment_id="gate-1",
    ) is True

    assert is_current_replacement_bootstrap_failure(
        _replacement_promotion(),
        [
            _replacement_event(),
            _replacement_event(note="restore_required_ack_needed"),
        ],
        gate_deployment_id="gate-1",
    ) is False


@pytest.mark.parametrize(
    ("promotion_overrides", "event_overrides", "gate_id"),
    [
        ({"state": "dev_verified"}, {}, "gate-1"),
        ({"gate_deployment_id": "other"}, {}, "gate-1"),
        ({"failure_reason": "dev_rollout_failed"}, {}, "gate-1"),
        ({}, {"release_version": "other"}, "gate-1"),
        ({}, {"action": "dev_rollout_failed"}, "gate-1"),
        ({}, {"from_state": "dev_pending"}, "gate-1"),
        ({}, {"to_state": "dev_verified"}, "gate-1"),
        ({}, {"note": "development_gate_current_module_set_invalid"}, "gate-1"),
        ({}, {}, ""),
    ],
)
def test_replacement_bootstrap_failure_rejects_ambiguous_evidence(
    promotion_overrides,
    event_overrides,
    gate_id,
):
    assert is_current_replacement_bootstrap_failure(
        _replacement_promotion(**promotion_overrides),
        [_replacement_event(**event_overrides)],
        gate_deployment_id=gate_id,
    ) is False


def _heartbeat(modules, *, onebrain_version="core-v1"):
    return SimpleNamespace(
        onebrain=SimpleNamespace(version=onebrain_version),
        modules=[
            SimpleNamespace(module_id=module_id, version=version, healthy=healthy)
            for module_id, version, healthy in modules
        ],
    )


def test_reported_modules_require_exact_healthy_versions_and_onebrain_identity():
    expected = {module_id: f"v-{module_id}" for module_id in DEVELOPMENT_GATE_MODULE_IDS}
    expected["onebrain-api"] = "core-v1"
    body = _heartbeat([
        (module_id, version, True)
        for module_id, version in expected.items()
    ])

    versions, reason = verify_reported_modules(body, expected)

    assert reason == ""
    assert versions == expected


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda rows: rows[:-1], "dev_module_set_mismatch"),
        (
            lambda rows: [*rows, ("foreign-service", "v-foreign", True)],
            "dev_module_set_mismatch",
        ),
        (
            lambda rows: [*rows, rows[0]],
            "dev_module_report_duplicate",
        ),
        (
            lambda rows: [(rows[0][0], rows[0][1], False), *rows[1:]],
            "dev_module_unhealthy",
        ),
        (
            lambda rows: [(rows[0][0], "wrong", True), *rows[1:]],
            "dev_module_mismatch",
        ),
    ],
)
def test_reported_modules_fail_closed(mutate, reason):
    expected = {module_id: f"v-{module_id}" for module_id in DEVELOPMENT_GATE_MODULE_IDS}
    expected["onebrain-api"] = "core-v1"
    rows = [
        (module_id, version, True)
        for module_id, version in sorted(expected.items())
    ]

    _, actual = verify_reported_modules(_heartbeat(mutate(rows)), expected)

    assert actual == reason
