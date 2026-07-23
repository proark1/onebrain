"""Pure policy helpers for the dedicated OneBrain development release gate."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence


DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS = (
    "assistant",
    "kpi_dashboard",
    "ai_employees",
    "communication",
    "buchhaltung",
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
LEGACY_CORE_GATE_REPLACEMENT_REQUIRED = "development_gate_replacement_required"
TARGET_MODULE_SET_INVALID = "development_gate_target_module_set_invalid"


def is_current_replacement_bootstrap_failure(
    promotion,
    events: Sequence,
    *,
    gate_deployment_id: str,
) -> bool:
    """Return whether the current failure authorizes replacement provisioning.

    Promotion-event stores return events in ascending creation order. Requiring
    the final event prevents an older replacement failure from surviving a later
    retry that failed for a different reason.
    """
    if (
        not gate_deployment_id
        or promotion is None
        or promotion.state != "dev_failed"
        or promotion.gate_deployment_id != gate_deployment_id
        or promotion.failure_reason != "dev_preflight_failed"
        or not events
    ):
        return False
    latest = events[-1]
    return bool(
        latest.release_version == promotion.release_version
        and latest.action == "dev_preflight_failed"
        and latest.from_state == "dev_deploying"
        and latest.to_state == "dev_failed"
        and latest.note == LEGACY_CORE_GATE_REPLACEMENT_REQUIRED
    )


def validate_module_transition(
    current_module_ids: Iterable[str],
    target_module_ids: Iterable[str],
) -> str:
    """Return an empty string only for an exact full-stack -> full-stack update.

    A legacy Core-only host cannot start or report the optional services because
    its compose profiles and local module allowlist are fixed at provisioning
    time. Such a gate must be replaced through the development-gate provisioner
    before it can receive a full-stack candidate.
    """
    current = frozenset(str(module_id).strip() for module_id in current_module_ids)
    target = frozenset(str(module_id).strip() for module_id in target_module_ids)
    if target != DEVELOPMENT_GATE_MODULE_IDS:
        return TARGET_MODULE_SET_INVALID
    if current == DEVELOPMENT_GATE_CORE_MODULE_IDS:
        return LEGACY_CORE_GATE_REPLACEMENT_REQUIRED
    if current != DEVELOPMENT_GATE_MODULE_IDS:
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
