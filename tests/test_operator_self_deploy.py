"""Operator self-deploy — "green main -> Mission Control".

MC auto-rolls its OWN box to each development-VERIFIED release, signed with the CI
development key. This suite pins every gate the feature widens (planner, desired-state
serve, multi-key box verifier) plus the trigger and the reconcile that close the loop,
and — critically — that customer delivery and the dormant default are untouched.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.controlplane.base import (
    CustomerDeployment,
    DeploymentModule,
    ReleaseManifest,
    ReleasePromotion,
    ReleasePromotionEvent,
    RolloutRun,
    compute_update_plan,
    is_operator_self_deployment,
    release_promotion_plan_context,
)
from app.controlplane.desired_state import sign_desired_state_for
from app.controlplane.development_gate import DEVELOPMENT_GATE_CORE_MODULE_IDS
from app.controlplane.memory import MemoryControlPlaneStore
from app.controlplane.pull_reconcile import (
    operator_self_converged,
    reconcile_pull_targets,
    synthesize_operator_self_status,
)
from app.fleet.heartbeat import ModuleReport, UpdateReport, build_heartbeat_v2
from app.routers import operator as operator_router
from app.trust.release import parse_registry_allowlist, release_signature_fields, sign_release
from app.trust.signing import generate_keypair

DEADLINE = 1800
MC = "mc"


def _ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


def _images() -> dict:
    return {
        module_id: f"ghcr.io/proark1/{module_id}@sha256:{format(index + 1, '064x')}"
        for index, module_id in enumerate(sorted(DEVELOPMENT_GATE_CORE_MODULE_IDS))
    }


def _release(version: str, migration: str = "0034") -> ReleaseManifest:
    return ReleaseManifest(
        version=version,
        git_sha="a" * 40,
        modules={module_id: version for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS},
        images=_images(),
        migration_from=migration,
        migration_to=migration,
        rollback_kind="code_only",
    )


def _seed_promotion(store, release, dev_priv, *, state="dev_verified") -> str:
    dev_sig = sign_release(release_signature_fields(release), dev_priv)
    ts = _ts()
    store.create_release_candidate(
        release,
        ReleasePromotion(
            release_version=release.version, state=state, dev_signature=dev_sig,
            dev_signing_key_id="dev-1", gate_deployment_id="",
            created_at=ts, updated_at=ts,
        ),
        ReleasePromotionEvent(
            id="", release_version=release.version, actor="test", action="seed",
            to_state=state, metadata={}, created_at=ts,
        ),
    )
    return dev_sig


def _mc_deployment(store, *, current_version="", current_migration="0034", healthy=True):
    store.create_deployment(CustomerDeployment(
        id=MC, customer_name=MC, deployment_type="dedicated_server",
        release_ring="manual", update_policy="manual",
        current_version=current_version, current_migration=current_migration,
        last_heartbeat_at=_ts(), last_heartbeat_healthy=healthy,
    ))
    for module_id in DEVELOPMENT_GATE_CORE_MODULE_IDS:
        # module rows require a non-empty version even when the deployment row is fresh.
        store.upsert_module(DeploymentModule(MC, module_id, current_version or "2026.07.22.001"))


def _settings(*, ds_priv, ds_pub, prod_pub, dev_pub, enabled=True, deployment_id=MC,
              operator_mode=True):
    return SimpleNamespace(
        operator_mode=operator_mode,
        operator_auto_deploy_enabled=enabled,
        deployment_id=deployment_id,
        release_promotion_required=True,
        release_require_signature=True,
        release_verify_public_key=prod_pub,
        dev_release_verify_public_key=dev_pub,
        fleet_report_seconds=60,
        fleet_desired_state_private_key=ds_priv,
        fleet_desired_state_public_key=ds_pub,
        fleet_desired_state_public_keys="",
        fleet_desired_state_ttl_seconds=900,
    )


# --- the shared predicate ----------------------------------------------------

def test_predicate_only_matches_mc_own_deployment_when_enabled():
    mc = CustomerDeployment(id=MC, customer_name=MC)
    other = CustomerDeployment(id="cust-1", customer_name="cust-1")
    on = SimpleNamespace(operator_mode=True, operator_auto_deploy_enabled=True, deployment_id=MC)
    assert is_operator_self_deployment(mc, on) is True
    assert is_operator_self_deployment(other, on) is False           # a different deployment id
    assert is_operator_self_deployment(None, on) is False
    off = SimpleNamespace(operator_mode=True, operator_auto_deploy_enabled=False, deployment_id=MC)
    assert is_operator_self_deployment(mc, off) is False             # dormant by default
    not_operator = SimpleNamespace(operator_mode=False, operator_auto_deploy_enabled=True, deployment_id=MC)
    assert is_operator_self_deployment(mc, not_operator) is False    # a customer box never qualifies


# --- planner (compute_update_plan) -------------------------------------------

def _plan(store, version, *, operator_self, dev_pub, prod_pub):
    release = store.get_release(version)
    promotion = store.get_release_promotion(version)
    return compute_update_plan(
        MC, version,
        deployment=store.get_deployment(MC),
        release=release,
        modules=store.list_modules(MC),
        latest_backup=lambda: None,
        require_signed_release=True,
        promotion=promotion,
        promotion_required=True,
        production_signature_valid=None,
        development_signature_valid=True,
        gate_deployment_id="dev-gate",
        heartbeat_max_age_seconds=600,
        operator_self=operator_self,
    )


def test_plan_allows_operator_self_at_dev_verified():
    dev_priv, dev_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_verified")
    plan = _plan(store, release.version, operator_self=True, dev_pub=dev_pub, prod_pub=prod_pub)
    assert plan.allowed, plan.reason


def test_plan_rejects_operator_self_before_dev_verified():
    dev_priv, dev_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_deploying")
    plan = _plan(store, release.version, operator_self=True, dev_pub=dev_pub, prod_pub=prod_pub)
    assert not plan.allowed
    assert plan.reason == "release_not_dev_verified"


def test_plan_without_operator_self_still_requires_customer_approved():
    # The SAME dev_verified release, planned as an ordinary deployment (operator_self
    # False), must stay blocked: the widened gate is scoped strictly to MC's own box.
    dev_priv, dev_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_verified")
    plan = _plan(store, release.version, operator_self=False, dev_pub=dev_pub, prod_pub=prod_pub)
    assert not plan.allowed
    assert plan.reason == "release_not_customer_approved"


def test_plan_context_reports_operator_self_only_for_mc(monkeypatch):
    import app.config as config_module

    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_verified")
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    promotion = store.get_release_promotion(release.version)
    mc_ctx = release_promotion_plan_context(release, promotion, store.get_deployment(MC))
    assert mc_ctx["operator_self"] is True
    other = CustomerDeployment(id="cust-1", customer_name="cust-1")
    assert release_promotion_plan_context(release, promotion, other)["operator_self"] is False


# --- desired-state serve -----------------------------------------------------

def test_desired_state_serves_dev_signature_to_mc_at_dev_verified():
    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    dev_sig = _seed_promotion(store, release, dev_priv, state="dev_verified")
    store.start_rollout(RolloutRun(id="roll_mc_x", deployment_id=MC, target_version=release.version,
                                   status="pending", started_by="operator-self:test"))
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    envelope = sign_desired_state_for(store, MC, settings=settings, now=datetime.now(timezone.utc))
    assert envelope is not None
    assert envelope.release.signature == dev_sig       # the CI development signature, not production
    assert envelope.release.version == release.version


def test_desired_state_dormant_for_mc_when_flag_off():
    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_verified")
    store.start_rollout(RolloutRun(id="roll_mc_x", deployment_id=MC, target_version=release.version,
                                   status="pending", started_by="operator-self:test"))
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub, enabled=False)
    # With auto-deploy off MC is an ordinary deployment: a dev_verified (not yet
    # customer_approved) release is NOT served even though a rollout targets it.
    assert sign_desired_state_for(store, MC, settings=settings, now=datetime.now(timezone.utc)) is None


def test_desired_state_does_not_serve_mc_before_dev_verified():
    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store)
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_deploying")
    # Point MC's steady state at the not-yet-verified release; the serve must still refuse.
    store._deployments[MC] = replace(store.get_deployment(MC), current_version=release.version)
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    assert sign_desired_state_for(store, MC, settings=settings, now=datetime.now(timezone.utc)) is None


# --- trigger (dispatch_operator_self_rollout) --------------------------------

def _trigger_store():
    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_version="2026.07.22.442")
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_verified")
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    return store, settings, release


def test_trigger_opens_forward_rollout_for_newest_dev_verified(monkeypatch):
    import app.config as config_module

    store, settings, release = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    assert rollout is not None
    assert rollout.deployment_id == MC and rollout.target_version == release.version
    active = store.list_active_rollout(MC)
    assert active is not None and active.id == rollout.id
    assert (active.request_payload or {}).get("pull") is True     # offered via the pull path


def test_trigger_is_idempotent_while_a_rollout_is_active(monkeypatch):
    import app.config as config_module

    store, settings, _ = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    first = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    assert first is not None
    assert operator_router.dispatch_operator_self_rollout(store, settings, actor="test") is None


def test_trigger_is_a_noop_when_disabled():
    store, settings, _ = _trigger_store()
    off = SimpleNamespace(**{**settings.__dict__, "operator_auto_deploy_enabled": False})
    assert operator_router.dispatch_operator_self_rollout(store, off, actor="test") is None
    assert store.list_active_rollout(MC) is None


def test_trigger_only_rolls_forward(monkeypatch):
    import app.config as config_module

    store, settings, release = _trigger_store()
    # MC is already ON the newest release -> nothing to do.
    store._deployments[MC] = replace(store.get_deployment(MC), current_version=release.version)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    assert operator_router.dispatch_operator_self_rollout(store, settings, actor="test") is None


def test_trigger_does_not_retry_a_failed_target(monkeypatch):
    import app.config as config_module

    store, settings, release = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    store.start_rollout(RolloutRun(
        id="roll_mc_prev", deployment_id=MC, target_version=release.version,
        status="failed", started_by="operator-self:test"))
    assert operator_router.dispatch_operator_self_rollout(store, settings, actor="test") is None


# --- reconcile (close the loop) ----------------------------------------------

def _mc_heartbeat(version, *, migration="0034", healthy=True):
    body = build_heartbeat_v2(
        deployment_id=MC, reported_at=_ts(), version=version, migration_revision=migration,
        onebrain_healthy=healthy, modules=[], update=UpdateReport(),
    )
    return SimpleNamespace(payload=body.model_dump())


def test_reconcile_completes_mc_rollout_on_version_and_health(monkeypatch):
    import app.config as config_module

    store, settings, release = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    assert rollout is not None
    now = datetime.now(timezone.utc)
    heartbeats = {MC: _mc_heartbeat(release.version)}
    reconcile_pull_targets(store, store, heartbeats, now=now, deadline_seconds=DEADLINE,
                           dispatch_child=lambda *_a, **_k: None, operator_self_deployment_id=MC)
    done = store.get_rollout(rollout.id)
    assert done.status == "success" and done.exec_status == "succeeded"
    assert store.get_deployment(MC).current_version == release.version   # converged


def test_reconcile_times_out_mc_rollout_without_touching_the_promotion(monkeypatch):
    import app.config as config_module

    store, settings, release = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    # Backdate the dispatch so the convergence deadline has passed, and never report the
    # target version (MC stays on its old version).
    store.update_rollout_exec(rollout.id, dispatched_at=(datetime.now(timezone.utc)
                              - timedelta(seconds=DEADLINE + 60)).isoformat(),
                              request_payload={"pull": True, "target_source": "operator_self"})
    now = datetime.now(timezone.utc)
    heartbeats = {MC: _mc_heartbeat("2026.07.22.442")}
    reconcile_pull_targets(store, store, heartbeats, now=now, deadline_seconds=DEADLINE,
                           dispatch_child=lambda *_a, **_k: None, operator_self_deployment_id=MC)
    assert store.get_rollout(rollout.id).status == "failed"
    # A self-update timeout must NOT corrupt the release pipeline.
    assert store.get_release_promotion(release.version).state == "dev_verified"
    assert store.get_deployment(MC).current_version == "2026.07.22.442"


def test_reconcile_failure_of_customer_approved_target_leaves_customer_delivery_alone(monkeypatch):
    # THE fleet-safety invariant: MC also self-rolls customer_approved releases, and a
    # FAILED self-rollout must NOT pause that release's customer delivery.
    import app.config as config_module

    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_version="2026.07.22.442")
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="customer_approved")
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    assert rollout is not None and rollout.target_version == release.version
    # Convergence deadline passes with MC never reporting the target version.
    store.update_rollout_exec(rollout.id, dispatched_at=(datetime.now(timezone.utc)
                              - timedelta(seconds=DEADLINE + 60)).isoformat(),
                              request_payload={"pull": True, "target_source": "operator_self"})
    reconcile_pull_targets(store, store, {MC: _mc_heartbeat("2026.07.22.442")},
                           now=datetime.now(timezone.utc), deadline_seconds=DEADLINE,
                           dispatch_child=lambda *_a, **_k: None, operator_self_deployment_id=MC)
    assert store.get_rollout(rollout.id).status == "failed"
    assert store.get_release_promotion(release.version).state == "customer_approved"   # NOT paused


def test_heartbeat_failure_of_mc_self_rollout_does_not_pause_customer_delivery(monkeypatch):
    # Twin of the reconcile guard, on the reconcile_heartbeat_promotion path (codex P1):
    # MC's OWN heartbeat reporting a FAILED self-update of a customer_approved release
    # must NOT pause customer delivery — that path keys only on attempt_id/deployment/
    # version and would otherwise fire once MC's `update` block carries a real attempt_id.
    import app.config as config_module
    from app.controlplane.promotion import reconcile_heartbeat_promotion

    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_version="2026.07.22.442")
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="customer_approved")
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    assert rollout is not None
    # MC's own heartbeat: the self-update FAILED and matches the exact attempt.
    body = build_heartbeat_v2(
        deployment_id=MC, reported_at=_ts(), version="2026.07.22.442", migration_revision="0034",
        onebrain_healthy=False, modules=[],
        update=UpdateReport(attempt_id=rollout.id, last_target_version=release.version, outcome="failed"),
    )
    reconcile_heartbeat_promotion(store, body, received_at=_ts())
    assert store.get_release_promotion(release.version).state == "customer_approved"   # NOT paused


def test_self_seed_backfills_modules_on_an_upgraded_mc():
    # codex P2: an MC whose row predates this change (created without module rows) must
    # still get its modules backfilled, or auto-deploy is silently blocked on
    # no_modules_installed.
    from app.controlplane.self_seed import seed_operator_self_deployment
    from app.fleet.memory import MemoryFleetStore

    control = MemoryControlPlaneStore()
    fleet = MemoryFleetStore()
    control.create_deployment(CustomerDeployment(id=MC, customer_name=MC, current_migration="0034"))
    assert control.list_modules(MC) == []
    settings = SimpleNamespace(operator_mode=True, deployment_id=MC,
                               build_version="2026.07.22.500", fleet_key="")
    assert seed_operator_self_deployment(settings, control, fleet) is True   # backfilled
    installed = {m.module_id for m in control.list_modules(MC) if m.status == "active"}
    assert installed == set(DEVELOPMENT_GATE_CORE_MODULE_IDS)
    assert seed_operator_self_deployment(settings, control, fleet) is False  # now idempotent


def test_operator_self_converged_rejects_an_unhealthy_reported_module():
    # codex P2: API up on the target version but another local service down must NOT be
    # read as converged when the heartbeat carries that module's health.
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_migration="0034")
    release = _release("2026.07.22.500")
    store.create_release(release)
    child = SimpleNamespace(target_version=release.version, deployment_id=MC)

    def _hb(module_healthy):
        body = build_heartbeat_v2(
            deployment_id=MC, reported_at=_ts(), version=release.version, migration_revision="0034",
            onebrain_healthy=True,
            modules=[ModuleReport(module_id="onebrain-admin-ui", version=release.version,
                                  healthy=module_healthy)],
            update=UpdateReport())
        return SimpleNamespace(payload=body.model_dump())

    assert operator_self_converged(store, child, _hb(module_healthy=False)) is False
    assert operator_self_converged(store, child, _hb(module_healthy=True)) is True


def test_trigger_filter_restricts_to_verified_even_without_the_promotion_gate(monkeypatch):
    # Defense in depth: the trigger's own _newest_operator_self_target filter is the
    # UNCONDITIONAL "dev_verified only" gate — it holds even if release_promotion_required
    # is off (the serve/planner state checks are conditional on that flag).
    import app.config as config_module

    dev_priv, dev_pub = generate_keypair()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_version="2026.07.22.442")
    release = _release("2026.07.22.500")
    _seed_promotion(store, release, dev_priv, state="dev_deploying")   # not yet verified
    settings = _settings(ds_priv=ds_priv, ds_pub=ds_pub, prod_pub=prod_pub, dev_pub=dev_pub)
    settings.release_promotion_required = False
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    assert operator_router.dispatch_operator_self_rollout(store, settings, actor="test") is None


def test_reconcile_ignores_mc_when_no_operator_self_id(monkeypatch):
    import app.config as config_module

    store, settings, _ = _trigger_store()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    rollout = operator_router.dispatch_operator_self_rollout(store, settings, actor="test")
    # Default operator_self_deployment_id="" (feature off in the tick) -> MC untouched.
    reconcile_pull_targets(store, store, {MC: _mc_heartbeat("2026.07.22.500")},
                           now=datetime.now(timezone.utc), deadline_seconds=DEADLINE,
                           dispatch_child=lambda *_a, **_k: None)
    assert store.get_rollout(rollout.id).status == "pending"


def test_operator_self_converged_requires_version_and_health():
    store = MemoryControlPlaneStore()
    _mc_deployment(store, current_migration="0034")
    release = _release("2026.07.22.500")
    store.create_release(release)
    child = SimpleNamespace(target_version=release.version, deployment_id=MC)
    assert operator_self_converged(store, child, _mc_heartbeat(release.version)) is True
    assert operator_self_converged(store, child, _mc_heartbeat("2026.07.22.442")) is False   # old version
    assert operator_self_converged(store, child, _mc_heartbeat(release.version, healthy=False)) is False
    assert operator_self_converged(store, child, _mc_heartbeat(release.version, migration="9999")) is False


def test_synthesize_operator_self_status_matrix():
    fresh = SimpleNamespace(id="c1", dispatched_at=_ts())
    stale = SimpleNamespace(id="c1", dispatched_at=(datetime.now(timezone.utc)
                            - timedelta(seconds=DEADLINE + 60)).isoformat())
    now = datetime.now(timezone.utc)
    assert synthesize_operator_self_status(fresh, converged=True, now=now, deadline_seconds=DEADLINE) == "success"
    assert synthesize_operator_self_status(fresh, converged=False, now=now, deadline_seconds=DEADLINE) is None
    assert synthesize_operator_self_status(stale, converged=False, now=now, deadline_seconds=DEADLINE) == "failed"
    # A converged box at the deadline still succeeds (never a spurious timeout).
    assert synthesize_operator_self_status(stale, converged=True, now=now, deadline_seconds=DEADLINE) == "success"


# --- multi-key release verification (box verifier <-> app twin) --------------

_BOX_DIR = Path(__file__).resolve().parents[1] / "deploy" / "box"


def _load_box_verify():
    spec = importlib.util.spec_from_file_location("onebrain_box_verify", _BOX_DIR / "onebrain_box_verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["onebrain_box_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


def _signed_envelope(*, ds_priv, rel_priv):
    from app.trust import envelope as E

    good_img = "ghcr.io/proark1/onebrain-api@sha256:" + "a" * 64
    fields = dict(version="2026.7.2", git_sha="abc123",
                  modules={"onebrain-api": "2026.7.2"}, images={"onebrain-api": good_img},
                  migration_from="0019", migration_to="0020", rollback_kind="code_only")
    block = E.SignedReleaseBlock(signature=sign_release(fields, rel_priv), **fields)
    env = E.DesiredStateEnvelope(
        deployment_id="dep_a", release=block, version_floor="", nonce="abcd1234efgh",
        issued_at="2026-07-12T00:00:00+00:00", expires_at="2026-07-12T23:59:00+00:00")
    return E.sign_desired_state(env, ds_priv).model_dump()


def _box_verify(bv, env, *, ds_pub, release_keys=None, release_single=""):
    return bv.verify_desired_state_multi(
        env,
        desired_state_public_keys=[ds_pub],
        release_public_keys=release_keys,
        release_public_key_b64=release_single,
        expected_deployment_id="dep_a",
        now=datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc),
        floor_state=SimpleNamespace(floor_version="", seen_nonces=frozenset()),
        registry_allowlist=parse_registry_allowlist("ghcr.io/proark1"),
    )


def test_release_multi_key_accepts_either_trusted_key():
    bv = _load_box_verify()
    ds_priv, ds_pub = generate_keypair()
    prod_priv, prod_pub = generate_keypair()
    dev_priv, dev_pub = generate_keypair()
    # A release signed with the DEV key is accepted when the box trusts prod+dev.
    env_dev = _signed_envelope(ds_priv=ds_priv, rel_priv=dev_priv)
    assert _box_verify(bv, env_dev, ds_pub=ds_pub, release_keys=[prod_pub, dev_pub]) == []
    # A release signed with the PROD key is still accepted by the same set.
    env_prod = _signed_envelope(ds_priv=ds_priv, rel_priv=prod_priv)
    assert _box_verify(bv, env_prod, ds_pub=ds_pub, release_keys=[prod_pub, dev_pub]) == []


def test_release_multi_key_rejects_untrusted_signer():
    bv = _load_box_verify()
    ds_priv, ds_pub = generate_keypair()
    _, prod_pub = generate_keypair()
    dev_priv, dev_pub = generate_keypair()
    stranger_priv, _ = generate_keypair()
    env = _signed_envelope(ds_priv=ds_priv, rel_priv=stranger_priv)
    assert _box_verify(bv, env, ds_pub=ds_pub, release_keys=[prod_pub, dev_pub]) == ["release_signature_invalid"]
    # And the dev key alone does NOT let a prod-only box in (customer boxes keep one key).
    env_dev = _signed_envelope(ds_priv=ds_priv, rel_priv=dev_priv)
    assert _box_verify(bv, env_dev, ds_pub=ds_pub, release_keys=[prod_pub]) == ["release_signature_invalid"]


def test_release_single_key_fallback_is_unchanged():
    bv = _load_box_verify()
    ds_priv, ds_pub = generate_keypair()
    rel_priv, rel_pub = generate_keypair()
    env = _signed_envelope(ds_priv=ds_priv, rel_priv=rel_priv)
    # No release SET configured -> falls back to the singular key, exactly as before.
    assert _box_verify(bv, env, ds_pub=ds_pub, release_keys=None, release_single=rel_pub) == []
    _, other_pub = generate_keypair()
    assert _box_verify(bv, env, ds_pub=ds_pub, release_keys=None,
                       release_single=other_pub) == ["release_signature_invalid"]


# --- self-seed completeness --------------------------------------------------

def test_self_seed_registers_modules_and_migration():
    from app.controlplane.self_seed import seed_operator_self_deployment
    from app.db.schema import REQUIRED_ALEMBIC_REVISION
    from app.fleet.memory import MemoryFleetStore

    control = MemoryControlPlaneStore()
    fleet = MemoryFleetStore()
    # fleet_key="" -> the self-seed skips key registration (covered elsewhere) but still
    # creates the deployment row + module rows, which is what this test pins.
    settings = SimpleNamespace(operator_mode=True, deployment_id=MC,
                               build_version="2026.07.22.500", fleet_key="")
    assert seed_operator_self_deployment(settings, control, fleet) is True
    deployment = control.get_deployment(MC)
    assert deployment is not None
    assert deployment.current_migration == REQUIRED_ALEMBIC_REVISION
    installed = {m.module_id for m in control.list_modules(MC) if m.status == "active"}
    assert installed == set(DEVELOPMENT_GATE_CORE_MODULE_IDS)
    # Idempotent: a second boot is a no-op.
    assert seed_operator_self_deployment(settings, control, fleet) is False


# --- config interlock --------------------------------------------------------

def test_auto_deploy_requires_the_dev_key():
    from unittest.mock import PropertyMock, patch

    from app.config import Settings

    _MSG = "required by ONEBRAIN_OPERATOR_AUTO_DEPLOY_ENABLED"
    with patch.object(Settings, "is_production_like", new_callable=PropertyMock, return_value=True), \
         patch.object(Settings, "is_operator_surface", new_callable=PropertyMock, return_value=True):
        missing = Settings.model_construct(operator_auto_deploy_enabled=True,
                                           dev_release_verify_public_key="")
        with pytest.raises(RuntimeError) as exc:
            missing.assert_production_mission_control_ready()
        assert _MSG in str(exc.value)
        # With the dev key present the interlock stops complaining (other production
        # gates may still fail, but never OUR check).
        present = Settings.model_construct(operator_auto_deploy_enabled=True,
                                           dev_release_verify_public_key="devpub")
        try:
            present.assert_production_mission_control_ready()
        except RuntimeError as exc2:
            assert _MSG not in str(exc2)
