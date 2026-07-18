from types import SimpleNamespace

import pytest

from app.controlplane.development_gate import (
    CURRENT_MODULE_SET_INVALID,
    DEVELOPMENT_GATE_CORE_MODULE_IDS,
    DEVELOPMENT_GATE_MODULE_IDS,
    TARGET_MODULE_SET_INVALID,
    validate_module_transition,
    verify_reported_modules,
)


@pytest.mark.parametrize(
    "current",
    [DEVELOPMENT_GATE_CORE_MODULE_IDS, DEVELOPMENT_GATE_MODULE_IDS],
)
def test_module_transition_accepts_exact_core_or_full_to_full(current):
    assert validate_module_transition(current, DEVELOPMENT_GATE_MODULE_IDS) == ""


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


def test_module_transition_requires_exact_full_target():
    assert (
        validate_module_transition(
            DEVELOPMENT_GATE_CORE_MODULE_IDS,
            DEVELOPMENT_GATE_MODULE_IDS - {"communication-workers"},
        )
        == TARGET_MODULE_SET_INVALID
    )


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
