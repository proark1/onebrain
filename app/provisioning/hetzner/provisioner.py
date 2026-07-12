"""The Hetzner provisioning executor (P4-03).

Mirrors `app.provisioning.runs.GitHubWorkflowDispatcher.dispatch(run) ->
ProvisioningRun`: it renders the box's cloud-init (P4-02) and calls the
token-isolating broker (P4-01), reusing the provisioning run state machine +
callback + one-time-secret envelope UNCHANGED. Only the executor changes.

Pure of any live call — the broker is injected (a `FakeHetznerClient`-backed
`InProcessHetznerBroker` in every test). No readiness poll, no smoke check: the
box's own cloud-init posts the existing provisioning callback (`apply_callback`)
with the smoke result + `bootstrap_password`, exactly like the Railway workflow.

D-6 slot convention (decided Phase 3, docs/hetzner-migration-sequence.md): a
successful dispatch writes `railway_project_id = "hetzner:<server_id>"`,
`railway_environment_id = "<compose_project>"`, and
`result_payload["service_ids"] = {module_id: compose_service_name}` (service ==
module id) so `resolve_railway_target()` / `target_provider()` classify the run
as hetzner with NO schema migration.

A15 (fleet-key mint/registration — producer already exists, cited not rebuilt):
the box authenticates the desired-state GET (P4-05) + heartbeat via a
deployment-pinned fleet key. That hash-only registration path already exists at
`app/fleet/enrollment.py:mint_deployment_fleet_key`. In P4 the renderer emits
`ONEBRAIN_FLEET_KEY` as a `${VAR}` placeholder (delivery is the Phase-5
bootstrap-token exchange), so it is not exercised end-to-end here (dormant).
"""

from __future__ import annotations

from dataclasses import replace

from app.provisioning.hetzner.broker import HetznerBroker
from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.render import BoxRenderInputs, render_cloud_init
from app.provisioning.runs import STATUS_DISPATCHED, ProvisioningRun, now_iso


def _ssh_key_ids(csv: str) -> tuple[int, ...]:
    """Parse the operator-config csv of Hetzner SSH key ids (break-glass only).
    Non-numeric entries are dropped defensively — there is no inbound-22 path a
    malformed id could open (H-3 firewall never opens 22)."""
    return tuple(int(part.strip()) for part in (csv or "").split(",") if part.strip().isdigit())


class HetznerProvisioner:
    """Executor for `provisioner_backend == "hetzner"`. Reachable only when a
    Hetzner target is explicitly selected (dormant on every live deployment until
    Phase 5). Owner-credential minting is P4-04; this executor leaves that seam
    (the rendered owner-OTP env ref is a `${VAR}` placeholder)."""

    def __init__(self, settings, broker: HetznerBroker, control_store, *, now_iso=now_iso):
        self.settings = settings
        self.broker = broker
        self.control_store = control_store
        self.now_iso = now_iso

    @property
    def enabled(self) -> bool:
        return bool(self.settings.hetzner_api_token) and self.settings.provisioner_backend == "hetzner"

    def dispatch(self, run: ProvisioningRun) -> ProvisioningRun:
        if not self.enabled:
            raise RuntimeError(
                "Hetzner provisioning is not configured (set provisioner_backend=hetzner + token)."
            )
        settings = self.settings

        # 1. Enabled modules = the deployment's active DeploymentModule rows.
        modules = [
            m.module_id
            for m in self.control_store.list_modules(run.deployment_id)
            if m.status == "active"
        ]
        if not modules:
            raise RuntimeError(f"no active modules for deployment {run.deployment_id}")

        # 2. Target release images. FAIL CLOSED: a Hetzner box MUST run signed,
        #    digest-pinned images; a tag-only or unknown release cannot be put on
        #    the fleet (RuntimeError -> _dispatch_run marks the run dispatch_failed).
        version = str(run.request_payload.get("initial_version", "")).strip()
        release = self.control_store.get_release(version) if version else None
        if not release or not release.images:
            raise RuntimeError(
                f"Hetzner requires digest-pinned images for release {version or '<none>'}; "
                "a tag-only or unknown release cannot be provisioned onto the fleet."
            )

        # 3. D-6 coordinates + optional public hostname.
        compose_project = f"onebrain-{run.deployment_id}"
        dns_enabled = bool(settings.fleet_dns_provider and settings.fleet_base_domain)
        fqdn = f"{run.deployment_id}.{settings.fleet_base_domain}" if dns_enabled else ""

        # 4. Render cloud-init (pure). A render ValueError (hostile id / a release
        #    image map that fails to cover an enabled module) becomes a
        #    dispatch_failed rather than a 500 — fail closed, never emit.
        try:
            cloud_init = render_cloud_init(BoxRenderInputs(
                deployment_id=run.deployment_id,
                account_id=run.account_id,
                compose_project=compose_project,
                enabled_modules=tuple(modules),
                images=dict(release.images),
                fqdn=fqdn,
                fleet_url=settings.fleet_url,
                fleet_public_desired_state_key=settings.fleet_desired_state_public_key,
                release_public_key=settings.release_verify_public_key,
                registry_allowlist=settings.release_registry_allowlist,
            ))
        except ValueError as exc:
            raise RuntimeError(f"cloud-init render failed: {exc}") from exc

        # 5. Broker create. The firewall is attached IN this create call (H-3);
        #    the data volume (if configured) is created first and attached
        #    in-create by the broker; DNS is upserted last (if a provider is set).
        server = ServerCreateRequest(
            name=compose_project,
            server_type=settings.hetzner_server_type,
            image=settings.hetzner_image,
            location=settings.hetzner_location,
            user_data=cloud_init,
            ssh_key_ids=_ssh_key_ids(settings.hetzner_ssh_key_ids),
            firewall_ids=(settings.hetzner_firewall_id,) if settings.hetzner_firewall_id else (),
            labels={"deployment_id": run.deployment_id},
        )
        volume = None
        if settings.hetzner_volume_size_gb > 0:
            volume = VolumeCreateRequest(
                name=f"{compose_project}-data",
                size_gb=settings.hetzner_volume_size_gb,
                location=settings.hetzner_location,
                labels={"deployment_id": run.deployment_id},
            )
        dns = None
        if dns_enabled:
            dns = DnsRecordRequest(zone_id=settings.fleet_dns_zone_id, name=fqdn, ipv4="", ttl=300)

        # A HetznerApiError IS a RuntimeError; it propagates to _dispatch_run,
        # which maps it to dispatch_failed (mirroring dispatch_workflow's shape).
        result = self.broker.provision_box(server=server, volume=volume, dns=dns)

        # 7. Write the D-6 slot convention + an erasure manifest (ids only; teardown
        #    execution is Phase-4-OUT). secret_ids is populated by P4-04.
        erasure_manifest = {
            "server_id": result.server_id,
            "volume_ids": list(result.volume_ids),
            "dns_record_id": result.dns_record_id,
            "user_data": "rendered",
            "secret_ids": [],
        }
        return replace(
            run,
            status=STATUS_DISPATCHED,
            external_provider="hetzner",
            railway_project_id=f"hetzner:{result.server_id}",
            railway_environment_id=compose_project,
            external_run_url=result.fqdn or result.public_ipv4,
            result_payload={
                **run.result_payload,
                "service_ids": {module_id: module_id for module_id in modules},
                "erasure_manifest": erasure_manifest,
            },
            dispatched_at=self.now_iso(),
        )
