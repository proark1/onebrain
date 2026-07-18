"""Pure policy helpers for the dedicated OneBrain development release gate."""

from __future__ import annotations

from collections.abc import Iterable, Mapping


DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS = (
    "assistant",
    "kpi_dashboard",
    "ai_employees",
    "communication",
)

DEVELOPMENT_GATE_CORE_MODULE_IDS = frozenset({
    "onebrain-api",
    "onebrain-admin-ui",
    "onebrain-workers",
})

DEVELOPMENT_GATE_MODULE_IDS = frozenset({
    *DEVELOPMENT_GATE_CORE_MODULE_IDS,
    "assistant-service",
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
})

CURRENT_MODULE_SET_INVALID = "development_gate_current_module_set_invalid"
TARGET_MODULE_SET_INVALID = "development_gate_target_module_set_invalid"


def validate_module_transition(
    current_module_ids: Iterable[str],
    target_module_ids: Iterable[str],
) -> str:
    """Return an empty string only for an exact Core/full -> full transition."""
    current = frozenset(str(module_id).strip() for module_id in current_module_ids)
    target = frozenset(str(module_id).strip() for module_id in target_module_ids)
    if target != DEVELOPMENT_GATE_MODULE_IDS:
        return TARGET_MODULE_SET_INVALID
    if current not in {
        DEVELOPMENT_GATE_CORE_MODULE_IDS,
        DEVELOPMENT_GATE_MODULE_IDS,
    }:
        return CURRENT_MODULE_SET_INVALID
    return ""


def reported_module_versions(body) -> tuple[dict[str, str], str]:
    """Normalize a heartbeat's module reports while rejecting ambiguous evidence."""
    versions: dict[str, str] = {}
    for report in getattr(body, "modules", ()):
        module_id = str(getattr(report, "module_id", "")).strip()
        if not module_id or module_id in versions:
            return {}, "dev_module_report_duplicate"
        if getattr(report, "healthy", None) is not True:
            return {}, "dev_module_unhealthy"
        versions[module_id] = str(getattr(report, "version", "")).strip()
    onebrain = getattr(body, "onebrain", None)
    onebrain_version = str(getattr(onebrain, "version", "")).strip()
    if versions.get("onebrain-api", "") != onebrain_version:
        return {}, "dev_module_mismatch"
    return versions, ""


def verify_reported_modules(
    body,
    expected_modules: Mapping[str, str],
) -> tuple[dict[str, str], str]:
    """Verify an exact, healthy reported set against the immutable manifest."""
    versions, reason = reported_module_versions(body)
    if reason:
        return {}, reason
    expected = {
        str(module_id).strip(): str(version).strip()
        for module_id, version in expected_modules.items()
    }
    if set(versions) != set(expected):
        return {}, "dev_module_set_mismatch"
    if versions != expected:
        return {}, "dev_module_mismatch"
    return versions, ""
