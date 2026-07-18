from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.controlplane.base import (
    BackupRun,
    CustomerDeployment,
    DeploymentModule,
    ReleaseManifest,
    ReleasePromotion,
    RolloutRun,
    compute_update_plan,
)
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.desired_state import sign_desired_state_for
from app.controlplane.development_gate import (
    DEVELOPMENT_GATE_CORE_MODULE_IDS,
    DEVELOPMENT_GATE_MODULE_IDS,
)
from app.controlplane.promotion import (
    ALLOWED_PROMOTION_TRANSITIONS,
    decide_transition,
    prepare_candidate,
    reconcile_heartbeat_promotion,
    reconcile_promotion_timeouts,
    register_candidate,
)
from app.fleet.heartbeat import ModuleReport, UpdateReport, build_heartbeat_v2
from app.fleet.base import FleetKey
from app.fleet.keys import hash_secret
from app.fleet.memory import MemoryFleetStore
from app.provisioning.bundles import resolve_module_composition
from app.routers import operator as operator_router
from app.trust.release import release_signature_fields, sign_release
from app.trust.signing import generate_keypair


DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/proark1/onebrain-api@{DIGEST}"
DEVELOPMENT_GATE_MODULES = resolve_module_composition(
    operator_router.DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS
).modules


def _release(version: str = "2026.07.13.1") -> ReleaseManifest:
    return ReleaseManifest(
        version=version,
        git_sha="a" * 40,
        modules={"onebrain-api": version},
        images={"onebrain-api": IMAGE},
        rollback_kind="code_only",
    )


def _development_gate_release(version: str = "2026.07.13.1") -> ReleaseManifest:
    return ReleaseManifest(
        version=version,
        git_sha="a" * 40,
        modules={
            module_id: (
                version
                if module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS
                else f"{version}-{module_id}"
            )
            for module_id in DEVELOPMENT_GATE_MODULES
        },
        images={
            module_id: f"ghcr.io/proark1/{module_id}@sha256:{format(index + 1, '064x')}"
            for index, module_id in enumerate(DEVELOPMENT_GATE_MODULES)
        },
        rollback_kind="code_only",
    )


def _development_gate_reports(release: ReleaseManifest) -> list[ModuleReport]:
    return [
        ModuleReport(module_id=module_id, version=version)
        for module_id, version in sorted(release.modules.items())
    ]


def test_promotion_transition_table_accepts_only_documented_edges():
    for current, allowed in ALLOWED_PROMOTION_TRANSITIONS.items():
        for candidate in ALLOWED_PROMOTION_TRANSITIONS:
            if candidate in allowed:
                assert decide_transition(current, candidate) == candidate
            else:
                with pytest.raises(ValueError, match="invalid_promotion_transition"):
                    decide_transition(current, candidate)


def test_prepare_candidate_carries_forward_unchanged_baseline_artifacts():
    baseline = ReleaseManifest(
        version="1",
        git_sha="1" * 40,
        modules={"onebrain-api": "1", "assistant-service": "1"},
        images={
            "onebrain-api": IMAGE,
            "assistant-service": f"ghcr.io/proark1/assistant-service@sha256:{'b' * 64}",
        },
        rollback_kind="code_only",
    )
    candidate = prepare_candidate(
        version="2",
        git_sha="2" * 40,
        changed_modules={"onebrain-api": "2"},
        changed_images={"onebrain-api": f"ghcr.io/proark1/onebrain-api@sha256:{'c' * 64}"},
        baseline=baseline,
    )
    assert candidate.modules == {"onebrain-api": "2", "assistant-service": "1"}
    assert candidate.images["assistant-service"] == baseline.images["assistant-service"]
    assert candidate.status == "draft"


def test_candidate_registration_is_idempotent_but_conflicts_on_changed_content():
    store = MemoryControlPlaneStore()
    dev_private, dev_public = generate_keypair()
    _, production_public = generate_keypair()
    release = _release()
    signature = sign_release(release_signature_fields(release), dev_private)

    promotion, created = register_candidate(
        store,
        release,
        dev_signature=signature,
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
        production_public_key=production_public,
    )
    assert created is True
    assert promotion.state == "dev_pending"
    same, created = register_candidate(
        store,
        release,
        dev_signature=signature,
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
        production_public_key=production_public,
    )
    assert created is False
    assert same == promotion

    changed = replace(release, git_sha="b" * 40)
    changed_signature = sign_release(release_signature_fields(changed), dev_private)
    with pytest.raises(ValueError, match="version_conflict"):
        register_candidate(
            store,
            changed,
            dev_signature=changed_signature,
            dev_signing_key_id="dev-1",
            development_public_key=dev_public,
            production_public_key=production_public,
        )


def test_development_and_production_keys_cannot_be_the_same():
    private, public = generate_keypair()
    release = _release()
    with pytest.raises(ValueError, match="must_differ"):
        register_candidate(
            MemoryControlPlaneStore(),
            release,
            dev_signature=sign_release(release_signature_fields(release), private),
            dev_signing_key_id="bad",
            development_public_key=public,
            production_public_key=public,
        )


def test_shared_planner_blocks_customers_until_approved_and_checks_heartbeat():
    now = datetime.now(timezone.utc)
    deployment = CustomerDeployment(
        id="customer",
        customer_name="Customer",
        last_heartbeat_at=now.isoformat(),
        last_heartbeat_healthy=True,
    )
    release = replace(_release(), signature="signed")
    modules = [DeploymentModule("customer", "onebrain-api", "old")]
    pending = ReleasePromotion(release.version, state="dev_verified")
    plan = compute_update_plan(
        "customer",
        release.version,
        deployment=deployment,
        release=release,
        modules=modules,
        latest_backup=lambda: None,
        promotion=pending,
        promotion_required=True,
        production_signature_valid=True,
        heartbeat_max_age_seconds=600,
        now=now,
    )
    assert plan.allowed is False
    assert plan.reason == "release_not_customer_approved"

    approved = replace(pending, state="customer_approved")
    plan = compute_update_plan(
        "customer",
        release.version,
        deployment=deployment,
        release=release,
        modules=modules,
        latest_backup=lambda: None,
        promotion=approved,
        promotion_required=True,
        production_signature_valid=True,
        heartbeat_max_age_seconds=600,
        now=now,
    )
    assert plan.allowed is True

    unhealthy = replace(deployment, last_heartbeat_healthy=False)
    plan = compute_update_plan(
        "customer",
        release.version,
        deployment=unhealthy,
        release=release,
        modules=modules,
        latest_backup=lambda: None,
        promotion=approved,
        promotion_required=True,
        production_signature_valid=True,
        heartbeat_max_age_seconds=600,
        now=now,
    )
    assert plan.reason == "deployment_unhealthy"


def test_successful_dev_rollout_needs_matching_later_heartbeat():
    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc).isoformat()
    gate = CustomerDeployment(
        id="dev",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=now,
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS:
        store.upsert_module(DeploymentModule(gate.id, module_id, "old"))
    dev_private, dev_public = generate_keypair()
    release = _development_gate_release()
    signature = sign_release(release_signature_fields(release), dev_private)
    register_candidate(
        store,
        release,
        dev_signature=signature,
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    rollout = RolloutRun("roll-dev", gate.id, release.version, "pending", "ci")
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
        fields={"gate_deployment_id": gate.id, "dev_rollout_id": rollout.id},
    )
    store.start_rollout(rollout)
    store.update_rollout_exec(rollout.id, exec_status="running")
    completed = datetime.now(timezone.utc).isoformat()
    store.complete_verified_rollout(
        rollout.id,
        verified_modules=release.modules,
        completed_at=completed,
    )

    heartbeat = build_heartbeat_v2(
        deployment_id=gate.id,
        reported_at=completed,
        version=release.version,
        modules=_development_gate_reports(release),
        update=UpdateReport(
            last_target_version=release.version,
            outcome="succeeded",
            attempt_id=rollout.id,
            ts=completed,
        ),
    )
    verified = reconcile_heartbeat_promotion(store, heartbeat, received_at=completed)
    assert verified.state == "dev_verified"
    assert {
        module.module_id: module.version
        for module in store.list_modules(gate.id)
        if module.status == "active"
    } == release.modules
    assert len([event for event in store.list_release_promotion_events(release.version)
                if event.action == "dev_verified"]) == 1
    assert reconcile_heartbeat_promotion(store, heartbeat, received_at=completed) is None


def test_dev_success_without_verification_heartbeat_times_out_and_cannot_revive():
    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc)
    gate = CustomerDeployment(
        id="dev",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=now.isoformat(),
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    store.upsert_module(DeploymentModule(gate.id, "onebrain-api", "old"))
    dev_private, dev_public = generate_keypair()
    release = _release()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    completed = (now - timedelta(minutes=20)).isoformat()
    rollout = RolloutRun("roll-timeout", gate.id, release.version, "pending", "ci")
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
        fields={"gate_deployment_id": gate.id, "dev_rollout_id": rollout.id},
    )
    store.start_rollout(rollout)
    store.update_rollout_exec(rollout.id, exec_status="running")
    store.update_rollout_status(rollout.id, "success")
    store.update_rollout_exec(rollout.id, exec_status="succeeded", completed_at=completed)

    changed = reconcile_promotion_timeouts(store, now=now, deadline_seconds=600)

    assert changed[0].state == "dev_failed"
    assert changed[0].failure_reason == "dev_verification_timeout"
    persisted_rollout = store.get_rollout(rollout.id)
    assert persisted_rollout.status == "success"
    assert persisted_rollout.exec_status == "succeeded"
    heartbeat = build_heartbeat_v2(
        deployment_id=gate.id,
        reported_at=now.isoformat(),
        version=release.version,
        modules=[ModuleReport(module_id="onebrain-api", version=release.version)],
        update=UpdateReport(
            last_target_version=release.version,
            outcome="succeeded",
            attempt_id=rollout.id,
            ts=now.isoformat(),
        ),
    )
    assert reconcile_heartbeat_promotion(store, heartbeat, received_at=now.isoformat()) is None
    assert store.get_release_promotion(release.version).state == "dev_failed"


def test_dev_convergence_timeout_finalizes_active_rollout():
    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc)
    gate = CustomerDeployment(
        id="dev",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=now.isoformat(),
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    store.upsert_module(DeploymentModule(gate.id, "onebrain-api", "old"))
    dev_private, dev_public = generate_keypair()
    release = _release()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    dispatched = (now - timedelta(minutes=20)).isoformat()
    rollout = RolloutRun("roll-convergence-timeout", gate.id, release.version, "pending", "ci")
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
        fields={"gate_deployment_id": gate.id, "dev_rollout_id": rollout.id},
    )
    store.start_rollout(rollout)
    store.update_rollout_exec(
        rollout.id,
        exec_status="dispatched",
        dispatched_at=dispatched,
    )

    changed = reconcile_promotion_timeouts(store, now=now, deadline_seconds=600)

    assert changed[0].state == "dev_failed"
    assert changed[0].failure_reason == "dev_convergence_timeout"
    persisted_rollout = store.get_rollout(rollout.id)
    assert persisted_rollout.status == "failed"
    assert persisted_rollout.exec_status == "failed"
    assert persisted_rollout.failure_reason == "dev_convergence_timeout"
    assert persisted_rollout.completed_at == now.isoformat()
    assert store.list_active_rollout(gate.id) is None


def test_authenticated_customer_health_failure_pauses_approved_release():
    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc).isoformat()
    customer = CustomerDeployment(id="customer", customer_name="Customer")
    release = _release()
    store.create_deployment(customer)
    store.upsert_module(DeploymentModule(customer.id, "onebrain-api", "old"))
    dev_private, dev_public = generate_keypair()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
    )
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_deploying"}),
        "dev_verified",
        actor="fleet:dev",
        action="dev_verified",
    )
    production_private, _ = generate_keypair()
    production_signature = sign_release(release_signature_fields(release), production_private)
    store.set_release_production_signature(
        release.version,
        signature=production_signature,
        signing_key_id="production-1",
        actor="operator",
    )
    store.approve_release_for_customers(
        release.version,
        signature=production_signature,
        signing_key_id="production-1",
        actor="operator",
    )
    rollout = RolloutRun("customer-attempt", customer.id, release.version, "pending", "operator")
    store.start_rollout(rollout)
    heartbeat = build_heartbeat_v2(
        deployment_id=customer.id,
        reported_at=now,
        onebrain_healthy=False,
        version="old",
        update=UpdateReport(
            last_target_version=release.version,
            outcome="in_progress",
            attempt_id=rollout.id,
            ts=now,
        ),
    )

    paused = reconcile_heartbeat_promotion(store, heartbeat, received_at=now)

    assert paused.state == "customer_paused"
    assert paused.failure_reason == "customer_health_failed"


def test_candidate_endpoint_accepts_only_the_configured_machine_credential(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException

    store = MemoryControlPlaneStore()
    dev_private, dev_public = generate_keypair()
    _, production_public = generate_keypair()
    release = _release()
    signature = sign_release(release_signature_fields(release), dev_private)
    settings = SimpleNamespace(
        operator_mode=True,
        release_candidate_key_id="candidate-ci",
        release_candidate_key_hash=hash_secret("candidate-secret"),
        dev_release_verify_public_key=dev_public,
        release_verify_public_key=production_public,
        release_registry_allowlist="ghcr.io/proark1",
    )
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    body = operator_router.ReleaseCandidateRequest(
        action="register",
        version=release.version,
        git_sha=release.git_sha,
        modules=release.modules,
        images=release.images,
        rollback_kind=release.rollback_kind,
        dev_signature=signature,
        dev_signing_key_id="dev-ci-v1",
    )
    out = operator_router.release_candidate(
        body,
        authorization="Bearer candidate-secret",
        x_onebrain_candidate_key_id="candidate-ci",
    )
    assert out.created is True
    assert out.release.promotion.state == "dev_pending"

    with pytest.raises(HTTPException) as exc:
        operator_router.release_candidate(
            body,
            authorization="Bearer wrong-secret",
            x_onebrain_candidate_key_id="candidate-ci",
        )
    assert exc.value.status_code == 401


def test_second_candidate_stays_pending_while_gate_verifies_first_rollout():
    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc).isoformat()
    gate = CustomerDeployment(
        id="dev-gate",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=now,
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    store.upsert_module(DeploymentModule(gate.id, "onebrain-api", "old"))

    dev_private, dev_public = generate_keypair()
    first = _release("2026.07.13.1")
    second = _release("2026.07.13.2")
    for release in (first, second):
        register_candidate(
            store,
            release,
            dev_signature=sign_release(release_signature_fields(release), dev_private),
            dev_signing_key_id="dev-1",
            development_public_key=dev_public,
        )
    store.transition_release_promotion(
        first.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
        fields={"gate_deployment_id": gate.id, "dev_rollout_id": "roll-first"},
    )
    store.start_rollout(RolloutRun("roll-first", gate.id, first.version, "pending", "ci"))

    queued = operator_router._dispatch_development_candidate(store, second.version, actor="ci")

    assert queued.state == "dev_pending"
    assert store.get_release_promotion(second.version).state == "dev_pending"
    assert store.get_rollout("roll-first").status == "pending"


def test_epoch_pending_candidate_stays_queued_until_next_heartbeat(monkeypatch):
    from types import SimpleNamespace

    import app.config as config_module
    from app.controlplane.rollout_exec import SECRETS_EPOCH_PENDING_REASON

    store = MemoryControlPlaneStore()
    gate = CustomerDeployment(
        id="dev-gate",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS:
        store.upsert_module(DeploymentModule(gate.id, module_id, "old"))
    private_key, public_key = generate_keypair()
    release = _development_gate_release()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), private_key),
        dev_signing_key_id="dev-1",
        development_public_key=public_key,
    )
    settings = SimpleNamespace(
        release_promotion_required=True,
        release_require_signature=True,
        release_verify_public_key="",
        dev_release_verify_public_key=public_key,
        fleet_report_seconds=60,
        fleet_public_url="https://mc.example.com",
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "_resolve_pull_target", lambda _deployment_id: SimpleNamespace(
        allowed=False,
        reason=SECRETS_EPOCH_PENDING_REASON,
    ))

    queued = operator_router._dispatch_development_candidate(store, release.version, actor="ci")
    assert queued.state == "dev_pending"
    assert store.list_rollouts(gate.id) == []

    monkeypatch.setattr(operator_router, "_resolve_pull_target", lambda _deployment_id: SimpleNamespace(
        allowed=True,
        reason="",
    ))
    monkeypatch.setattr(operator_router, "_dispatch_child_rollout", lambda *_args, **_kwargs: None)
    dispatched = operator_router.dispatch_waiting_development_candidate(store, actor="fleet:dev-gate")
    assert dispatched.state == "dev_deploying"
    assert dispatched.dev_rollout_id


@pytest.mark.parametrize("retry_failed_candidate", [False, True])
def test_dev_dispatch_persists_rollout_before_promotion_foreign_key(
    monkeypatch, retry_failed_candidate
):
    from types import SimpleNamespace

    import app.config as config_module

    class ForeignKeyCheckingStore(MemoryControlPlaneStore):
        def transition_release_promotion(self, version, from_states, to_state, **kwargs):
            rollout_id = (kwargs.get("fields") or {}).get("dev_rollout_id", "")
            if rollout_id and self.get_rollout(rollout_id) is None:
                raise ValueError("promotion rollout foreign key missing")
            return super().transition_release_promotion(
                version, from_states, to_state, **kwargs
            )

    store = ForeignKeyCheckingStore()
    gate = CustomerDeployment(
        id="dev-gate",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        last_heartbeat_at=datetime.now(timezone.utc).isoformat(),
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS:
        store.upsert_module(DeploymentModule(gate.id, module_id, "old"))
    dev_private, dev_public = generate_keypair()
    release = _development_gate_release()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    if retry_failed_candidate:
        store.transition_release_promotion(
            release.version,
            frozenset({"dev_pending"}),
            "dev_deploying",
            actor="mission-control",
            action="dev_rollout_started",
            fields={"gate_deployment_id": gate.id},
        )
        store.transition_release_promotion(
            release.version,
            frozenset({"dev_deploying"}),
            "dev_failed",
            actor="mission-control",
            action="dev_preflight_failed",
            fields={"failure_reason": "dev_preflight_failed"},
        )
    settings = SimpleNamespace(
        release_promotion_required=True,
        release_require_signature=True,
        release_verify_public_key="",
        dev_release_verify_public_key=dev_public,
        fleet_report_seconds=60,
        fleet_public_url="https://mc.example.com",
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(
        operator_router,
        "_resolve_pull_target",
        lambda _deployment_id: SimpleNamespace(allowed=True, reason=""),
    )
    dispatched = {}

    def fake_dispatch(*_args, **kwargs):
        rollout = store.get_rollout(kwargs["child_id"])
        assert rollout is not None
        assert kwargs["child_precreated"] is True
        dispatched["rollout"] = rollout

    monkeypatch.setattr(operator_router, "_dispatch_child_rollout", fake_dispatch)

    promotion = operator_router._dispatch_development_candidate(
        store, release.version, actor="candidate:ci"
    )

    assert release.signature == ""
    assert promotion.state == "dev_deploying"
    assert promotion.dev_rollout_id == dispatched["rollout"].id
    assert store.get_rollout(promotion.dev_rollout_id) is not None


def test_restore_required_retry_requires_note_and_persists_linked_ack(monkeypatch):
    from types import SimpleNamespace

    import app.config as config_module

    store = MemoryControlPlaneStore()
    now = datetime.now(timezone.utc).isoformat()
    gate = CustomerDeployment(
        id="dev-gate",
        customer_name="Development",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        current_migration="0030",
        last_heartbeat_at=now,
        last_heartbeat_healthy=True,
    )
    store.create_deployment(gate)
    store.designate_release_gate(gate.id)
    for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS:
        store.upsert_module(DeploymentModule(gate.id, module_id, "old"))
    store.record_backup(BackupRun("backup", gate.id, "success", "verified"))

    dev_private, dev_public = generate_keypair()
    release = replace(
        _development_gate_release("2026.07.18.271"),
        rollback_kind="restore_required",
        migration_from="0030",
        migration_to="0034",
    )
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    previous_rollout = RolloutRun(
        "previous-dev-rollout",
        gate.id,
        release.version,
        "pending",
        "ci",
        ack_restore_required=True,
    )
    store.start_rollout(previous_rollout)
    store.update_rollout_status(previous_rollout.id, "failed")
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
        fields={
            "gate_deployment_id": gate.id,
            "dev_rollout_id": previous_rollout.id,
            "dev_attempt_id": previous_rollout.id,
        },
    )
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_deploying"}),
        "dev_failed",
        actor="mission-control",
        action="dev_preflight_failed",
        fields={"failure_reason": "dev_preflight_failed"},
    )
    settings = SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,
        release_promotion_required=True,
        release_require_signature=True,
        release_verify_public_key="",
        dev_release_verify_public_key=dev_public,
        fleet_report_seconds=60,
        fleet_public_url="https://mc.example.com",
    )
    principal = SimpleNamespace(role_id="admin", user_id="operator@example.com")
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(
        operator_router,
        "_resolve_pull_target",
        lambda _deployment_id: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(
        operator_router,
        "_dispatch_child_rollout",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(HTTPException, match="restore_required_review_note_required"):
        operator_router.retry_development_release(
            release.version,
            operator_router.DevelopmentRetryIn(ack_restore_required=True),
            principal,
        )
    assert store.get_release_promotion(release.version).state == "dev_failed"

    blocked = operator_router.retry_development_release(
        release.version,
        operator_router.DevelopmentRetryIn(note="Reviewed backup and restore plan."),
        principal,
    )
    assert blocked.promotion.state == "dev_failed"
    assert store.get_release_promotion(release.version).dev_rollout_id == ""
    assert store.list_active_rollout(gate.id) is None

    out = operator_router.retry_development_release(
        release.version,
        operator_router.DevelopmentRetryIn(
            note="Reviewed backup and restore plan.",
            ack_restore_required=True,
        ),
        principal,
    )

    promotion = store.get_release_promotion(release.version)
    rollout = store.get_rollout(promotion.dev_rollout_id)
    assert out.promotion.state == "dev_deploying"
    assert rollout.ack_restore_required is True
    assert rollout.notes == "Reviewed backup and restore plan."
    retry_event = store.list_release_promotion_events(release.version)[-1]
    assert retry_event.action == "dev_rollout_retried"
    assert retry_event.actor == principal.user_id
    assert retry_event.note == (
        "restore_required acknowledged: Reviewed backup and restore plan."
    )


def test_gate_replacement_is_validated_before_atomic_marker_swap(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException

    store = MemoryControlPlaneStore()
    fleet = MemoryFleetStore()
    production_private, production_public = generate_keypair()
    baseline = replace(_development_gate_release("2026.07.12.1"), status="active")
    baseline = replace(
        baseline,
        signature=sign_release(release_signature_fields(baseline), production_private),
        signing_key_id="production-1",
    )
    store.create_release(baseline)
    old_gate = CustomerDeployment(
        id="old-gate",
        customer_name="Old development gate",
        environment="development",
        deployment_type="dedicated_server",
    )
    replacement = CustomerDeployment(
        id="replacement-gate",
        customer_name="Replacement development gate",
        environment="development",
        deployment_type="dedicated_server",
        current_version=baseline.version,
        last_heartbeat_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        last_heartbeat_healthy=True,
        last_reported_version=baseline.version,
    )
    store.create_deployment(old_gate)
    store.create_deployment(replacement)
    for module_id in DEVELOPMENT_GATE_MODULES:
        store.upsert_module(DeploymentModule(
            replacement.id, module_id, baseline.modules[module_id], status="active"))
    store.designate_release_gate(old_gate.id)
    fleet.create_key(FleetKey(id="replacement-key", key_hash="hash", deployment_id=replacement.id))
    settings = SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,
        fleet_report_seconds=60,
        release_verify_public_key=production_public,
    )
    principal = SimpleNamespace(role_id="admin", user_id="operator")
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(operator_router, "get_fleet_store", lambda: fleet)

    with pytest.raises(HTTPException, match="heartbeat_stale"):
        operator_router.designate_development_gate(replacement.id, principal)
    assert store.get_release_gate().id == old_gate.id

    store.update_deployment_telemetry(
        replacement.id,
        heartbeat_at=datetime.now(timezone.utc).isoformat(),
        healthy=True,
        reported_version=baseline.version,
    )
    designated = operator_router.designate_development_gate(replacement.id, principal)
    assert designated.ready is True
    assert store.get_release_gate().id == replacement.id


def test_development_provision_rejects_existing_normalized_deployment(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    store = MemoryControlPlaneStore()
    store.create_deployment(CustomerDeployment(
        id=operator_router.DEVELOPMENT_GATE_DEPLOYMENT_ID,
        customer_name="Development gate",
        environment="development",
        deployment_type="dedicated_server",
    ))
    settings = SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,
        provisioner_backend="hetzner",
    )
    principal = SimpleNamespace(role_id="admin", user_id="operator")
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)

    with pytest.raises(HTTPException) as exc:
        operator_router.provision_development_gate(
            operator_router.DevelopmentGateProvisionIn(
                owner_email="owner@example.com",
                dry_run=True,
            ),
            principal,
        )

    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


def test_development_gate_replacement_requires_module_complete_baseline_and_uses_generated_identity(monkeypatch):
    from types import SimpleNamespace

    import app.routers.provisioning as provisioning_router

    store = MemoryControlPlaneStore()
    production_private, production_public = generate_keypair()
    baseline = replace(_development_gate_release(), status="active")
    baseline = replace(
        baseline,
        signature=sign_release(release_signature_fields(baseline), production_private),
        signing_key_id="production-1",
    )
    store.create_release(baseline)
    old_gate = CustomerDeployment(
        id=operator_router.DEVELOPMENT_GATE_DEPLOYMENT_ID,
        customer_name="Current gate",
        environment="development",
        deployment_type="dedicated_server",
    )
    store.create_deployment(old_gate)
    store.designate_release_gate(old_gate.id)
    settings = SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,
        provisioner_backend="hetzner",
        release_verify_public_key=production_public,
        release_registry_allowlist="ghcr.io/proark1",
        dev_release_verify_public_key="dev-public",
        fleet_public_url="https://mc.example.com",
    )
    principal = SimpleNamespace(role_id="admin", user_id="operator")
    captured = {}
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)
    monkeypatch.setattr(provisioning_router, "_provision_customer_impl",
                        lambda body, _principal: captured.setdefault("body", body))

    result = operator_router.provision_development_gate(
        operator_router.DevelopmentGateProvisionIn(owner_email="owner@example.com", dry_run=False),
        principal,
    )

    assert result is captured["body"]
    assert result.module_ids == list(operator_router.DEVELOPMENT_GATE_OPTIONAL_MODULE_IDS)
    assert tuple(result.module_versions) == DEVELOPMENT_GATE_MODULES
    assert result.mint_integration_keys is True
    assert result.deployment_id.startswith(operator_router.DEVELOPMENT_GATE_DEPLOYMENT_ID + "-")
    assert result.deployment_id != old_gate.id
    assert result.account_id.startswith("onebrain-development-")
    assert result.environment == "development"
    assert result.release_ring == "internal"
    assert result.external_provisioning is True


def test_development_gate_preflight_names_missing_modules(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException

    store = MemoryControlPlaneStore()
    production_private, production_public = generate_keypair()
    baseline = replace(_release(), status="active")
    baseline = replace(baseline, signature=sign_release(release_signature_fields(baseline), production_private))
    store.create_release(baseline)
    settings = SimpleNamespace(
        is_operator_surface=True,
        operator_mode=True,
        provisioner_backend="hetzner",
        release_verify_public_key=production_public,
        release_registry_allowlist="ghcr.io/proark1",
    )
    principal = SimpleNamespace(role_id="admin", user_id="operator")
    monkeypatch.setattr(operator_router, "get_settings", lambda: settings)
    monkeypatch.setattr(operator_router, "get_control_plane_store", lambda: store)

    with pytest.raises(HTTPException, match="development gate modules") as exc:
        operator_router.provision_development_gate(
            operator_router.DevelopmentGateProvisionIn(owner_email="owner@example.com", dry_run=True),
            principal,
        )

    assert exc.value.status_code == 409
    assert "assistant-service" in exc.value.detail


def test_dev_signature_is_served_only_to_the_designated_gate():
    from types import SimpleNamespace

    store = MemoryControlPlaneStore()
    gate = CustomerDeployment(
        id="dev-gate",
        customer_name="Dev",
        environment="development",
        deployment_type="dedicated_server",
        release_ring="internal",
        current_version="old",
    )
    customer = CustomerDeployment(id="customer", customer_name="Customer", current_version="old")
    store.create_deployment(gate)
    store.create_deployment(customer)
    store.designate_release_gate(gate.id)
    store.upsert_module(DeploymentModule(gate.id, "onebrain-api", "old"))
    store.upsert_module(DeploymentModule(customer.id, "onebrain-api", "old"))
    dev_private, dev_public = generate_keypair()
    wrapper_private, _ = generate_keypair()
    release = _release()
    dev_signature = sign_release(release_signature_fields(release), dev_private)
    register_candidate(
        store,
        release,
        dev_signature=dev_signature,
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    store.start_rollout(RolloutRun("dev-offer", gate.id, release.version, "pending", "ci"))
    store.start_rollout(RolloutRun("customer-offer", customer.id, release.version, "pending", "operator"))
    settings = SimpleNamespace(
        fleet_desired_state_private_key=wrapper_private,
        fleet_desired_state_ttl_seconds=300,
    )
    now = datetime.now(timezone.utc)
    dev_envelope = sign_desired_state_for(store, gate.id, settings=settings, now=now)
    assert dev_envelope.release.signature == dev_signature
    assert sign_desired_state_for(store, customer.id, settings=settings, now=now) is None


def test_paused_customer_release_is_not_served_to_an_inflight_rollout():
    from types import SimpleNamespace

    store = MemoryControlPlaneStore()
    customer = CustomerDeployment(id="customer", customer_name="Customer", current_version="old")
    store.create_deployment(customer)
    store.upsert_module(DeploymentModule(customer.id, "onebrain-api", "old"))
    release = _release()
    dev_private, dev_public = generate_keypair()
    register_candidate(
        store,
        release,
        dev_signature=sign_release(release_signature_fields(release), dev_private),
        dev_signing_key_id="dev-1",
        development_public_key=dev_public,
    )
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_pending"}),
        "dev_deploying",
        actor="ci",
        action="dev_rollout_started",
    )
    store.transition_release_promotion(
        release.version,
        frozenset({"dev_deploying"}),
        "dev_verified",
        actor="fleet:dev",
        action="dev_verified",
    )
    production_private, _ = generate_keypair()
    production_signature = sign_release(release_signature_fields(release), production_private)
    store.set_release_production_signature(
        release.version,
        signature=production_signature,
        signing_key_id="production-1",
        actor="operator",
    )
    store.approve_release_for_customers(
        release.version,
        signature=production_signature,
        signing_key_id="production-1",
        actor="operator",
    )
    store.start_rollout(RolloutRun("customer-offer", customer.id, release.version, "pending", "operator"))
    wrapper_private, _ = generate_keypair()
    settings = SimpleNamespace(
        fleet_desired_state_private_key=wrapper_private,
        fleet_desired_state_ttl_seconds=300,
        release_promotion_required=True,
    )
    now = datetime.now(timezone.utc)
    assert sign_desired_state_for(store, customer.id, settings=settings, now=now) is not None

    store.transition_release_promotion(
        release.version,
        frozenset({"customer_approved"}),
        "customer_paused",
        actor="operator",
        action="customer_delivery_paused",
        fields={"customer_paused_reason": "review"},
    )
    assert sign_desired_state_for(store, customer.id, settings=settings, now=now) is None
