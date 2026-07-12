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

import json
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.controlplane.desired_state import active_signer_in_served_set
from app.fleet.bootstrap_bundle import validate_bundle
from app.fleet.enrollment import mint_deployment_fleet_key
from app.fleet.keys import generate_bootstrap_token, hash_secret
from app.provisioning.hetzner.broker import HetznerBroker
from app.provisioning.hetzner.client import (
    DnsRecordRequest,
    FirewallCreateRequest,
    FirewallRule,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.render import BoxRenderInputs, render_cloud_init
from app.provisioning.runs import (
    STATUS_DISPATCHED,
    BoxBootstrapToken,
    BoxSecretBundle,
    OneTimeSecretCipher,
    ProvisioningRun,
    now_iso,
)


def store_owner_one_time_password(prov_store, settings, run: ProvisioningRun, otp: str) -> ProvisioningRun:
    """H-10/A8: wrap the owner OTP (minted hash-only by CustomerProvisioner) in a
    short-TTL OneTimeSecretEnvelope (purpose 'owner_one_time_password'; the TTL is
    the existing bootstrap-secret TTL, so no new column), store it, and point the
    run's bootstrap_secret_id + erasure_manifest.secret_ids at it. The owner OTP
    IS the bootstrap secret for a Hetzner box, so the EXISTING read_bootstrap_secret
    endpoint returns it once. No-op (returns the run unchanged) when otp is empty."""
    if not otp:
        return run
    envelope = OneTimeSecretCipher(settings).envelope(
        purpose="owner_one_time_password",
        account_id=run.account_id,
        deployment_id=run.deployment_id,
        plaintext=otp,
    )
    stored = prov_store.create_secret(envelope)
    manifest = dict(run.result_payload.get("erasure_manifest", {}))
    manifest["secret_ids"] = list(manifest.get("secret_ids", [])) + [stored.id]
    return prov_store.update_run(replace(
        run,
        bootstrap_secret_id=stored.id,
        result_payload={**run.result_payload, "erasure_manifest": manifest},
    ))


def _ssh_key_ids(csv: str) -> tuple[int, ...]:
    """Parse the operator-config csv of Hetzner SSH key ids (break-glass only).
    Non-numeric entries are dropped defensively — there is no inbound-22 path a
    malformed id could open (H-3 firewall never opens 22)."""
    return tuple(int(part.strip()) for part in (csv or "").split(",") if part.strip().isdigit())


def _default_deny_rules(allow_ssh: bool) -> tuple[FirewallRule, ...]:
    """The default-deny inbound rule set for a box's Cloud Firewall (§5, P5-05). Only
    HTTP(S) is exposed; Postgres/Redis have NO inbound rule (so 5432/6379 are internet-
    unreachable). Inbound 22 is emitted ONLY as a deliberate break-glass."""
    rules = [
        FirewallRule(direction="in", protocol="tcp", port="80"),
        FirewallRule(direction="in", protocol="tcp", port="443"),
    ]
    if allow_ssh:
        rules.append(FirewallRule(direction="in", protocol="tcp", port="22"))
    return tuple(rules)


class HetznerProvisioner:
    """Executor for `provisioner_backend == "hetzner"`. Reachable only when a
    Hetzner target is explicitly selected (dormant on every live deployment until
    Phase 5). Owner-credential minting is P4-04; this executor leaves that seam
    (the rendered owner-OTP env ref is a `${VAR}` placeholder)."""

    def __init__(self, settings, broker: HetznerBroker, control_store, *,
                 prov_store=None, fleet_store=None, now_iso=now_iso):
        self.settings = settings
        self.broker = broker
        self.control_store = control_store
        # P5-03: the provisioning + fleet stores are injected so dispatch can mint the
        # box's secret bundle + a single-use bootstrap token (G3-3). When absent (the
        # pure-executor tests that only assert server creation) bundle assembly is
        # skipped and the box renders with empty tokens — the router seam (_dispatch_run)
        # always injects them for a real provision.
        self.prov_store = prov_store
        self.fleet_store = fleet_store
        self.now_iso = now_iso

    @property
    def enabled(self) -> bool:
        return bool(self.settings.hetzner_api_token) and self.settings.provisioner_backend == "hetzner"

    def dispatch(self, run: ProvisioningRun, *, owner_otp: str = "",
                 service_key: str = "", space_id: str = "") -> ProvisioningRun:
        # owner_otp / service_key / space_id are threaded in from the bundle-assembly
        # seam (provision_customer, G3-3): they are minted by CustomerProvisioner and do
        # NOT flow to dispatch on their own. They populate the box's secret bundle.
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

        # 3. D-6 coordinates + optional public hostname. DNS is gated STRICTLY on the
        #    Hetzner provider (P5-05) — a "cloudflare"/unknown provider skips DNS (serve
        #    on the raw IP) rather than mis-calling the Hetzner DNS client; a zone id is
        #    required (the A record needs one).
        compose_project = f"onebrain-{run.deployment_id}"
        dns_enabled = (settings.fleet_dns_provider == "hetzner"
                       and bool(settings.fleet_base_domain and settings.fleet_dns_zone_id))
        fqdn = f"{run.deployment_id}.{settings.fleet_base_domain}" if dns_enabled else ""

        # 4a. Mint + persist the box secret bundle and a single-use bootstrap token
        #     (P5-03). Fail closed: an invalid bundle or a G1-1 interlock violation
        #     raises RuntimeError -> dispatch_failed, so a box that can't come up (or
        #     one whose accepted wrapper-key set would exclude MC's active signer) is
        #     never created. Skipped when the stores aren't injected (pure-executor tests).
        bootstrap_token = ""
        callback_token = ""
        if self.prov_store is not None and self.fleet_store is not None:
            bootstrap_token, callback_token = self._provision_box_secrets(
                run, owner_otp=owner_otp, service_key=service_key, space_id=space_id)

        # 4b. Render cloud-init (pure). A render ValueError (hostile id / a release
        #     image map that fails to cover an enabled module) becomes a
        #     dispatch_failed rather than a 500 — fail closed, never emit.
        try:
            cloud_init = render_cloud_init(BoxRenderInputs(
                deployment_id=run.deployment_id,
                account_id=run.account_id,
                compose_project=compose_project,
                enabled_modules=tuple(modules),
                images=dict(release.images),
                fqdn=fqdn,
                fleet_url=settings.fleet_url,
                run_id=run.id,   # baked into the box callback URL so the box can report back (not {run_id})
                fleet_public_desired_state_key=settings.fleet_desired_state_public_key,
                release_public_key=settings.release_verify_public_key,
                registry_allowlist=settings.release_registry_allowlist,
                bootstrap_token=bootstrap_token,
                callback_token=callback_token,
            ))
        except ValueError as exc:
            raise RuntimeError(f"cloud-init render failed: {exc}") from exc

        # 5. Broker create. When no pre-created firewall is configured, a default-deny
        #    Cloud Firewall (P5-05) is created IN the flow and attached in the server
        #    create (H-3): inbound tcp 80 + 443 (+ 22 ONLY under the break-glass flag);
        #    NO Postgres/Redis inbound rule, so 5432/6379 stay unreachable from the
        #    internet (paired with the compose `expose:` — never `ports:`). Egress is
        #    unrestricted at the Hetzner layer (the metadata-egress block is box iptables).
        firewall = None
        if not settings.hetzner_firewall_id:
            firewall = FirewallCreateRequest(
                name=f"{compose_project}-fw",
                rules=_default_deny_rules(settings.hetzner_firewall_allow_ssh),
                labels={"deployment_id": run.deployment_id},
            )
        server = ServerCreateRequest(
            name=compose_project,
            server_type=settings.hetzner_server_type,
            image=settings.hetzner_image,
            location=settings.hetzner_location,
            user_data=cloud_init,
            ssh_key_ids=_ssh_key_ids(settings.hetzner_ssh_key_ids),
            # A pre-created firewall is attached as-is; otherwise the broker creates the
            # default-deny one above and attaches ITS id in the same create call.
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
            # Hetzner DNS treats a record name WITHOUT a trailing dot as RELATIVE to the zone,
            # so the A-record name is the zone-relative LABEL (deployment_id), NOT the full
            # fqdn — POSTing "dep_a.fleet.example" into zone fleet.example would resolve as
            # "dep_a.fleet.example.fleet.example" (and _find_dns_record, which compares against
            # Hetzner's relative names, would never match -> a duplicate A record on every
            # re-provision). fqdn is kept below for the box hostname / external_run_url.
            dns = DnsRecordRequest(zone_id=settings.fleet_dns_zone_id, name=run.deployment_id, ipv4="", ttl=300)

        # A HetznerApiError IS a RuntimeError; it propagates to _dispatch_run,
        # which maps it to dispatch_failed (mirroring dispatch_workflow's shape).
        result = self.broker.provision_box(server=server, volume=volume, dns=dns, firewall=firewall)

        # 7. Write the D-6 slot convention + an erasure manifest (ids only; teardown
        #    execution is Phase-4-OUT). secret_ids is populated by P4-04. firewall_id is
        #    the CREATED default-deny firewall ("" when a pre-existing one was attached).
        erasure_manifest = {
            "server_id": result.server_id,
            "volume_ids": list(result.volume_ids),
            "dns_record_id": result.dns_record_id,
            "firewall_id": result.firewall_id,
            "user_data": "rendered",
            "secret_ids": [],
        }
        return replace(
            run,
            status=STATUS_DISPATCHED,
            external_provider="hetzner",
            railway_project_id=f"hetzner:{result.server_id}",
            railway_environment_id=compose_project,
            # The public hostname is the full fqdn we constructed (the DNS record carries only
            # the zone-relative label); fall back to the raw IP when DNS is disabled.
            external_run_url=fqdn or result.public_ipv4,
            result_payload={
                **run.result_payload,
                "service_ids": {module_id: module_id for module_id in modules},
                "erasure_manifest": erasure_manifest,
            },
            dispatched_at=self.now_iso(),
        )

    def _provision_box_secrets(self, run: ProvisioningRun, *, owner_otp: str,
                               service_key: str, space_id: str) -> tuple[str, str]:
        """Mint the box's real secrets, seal the RE-READABLE bundle (G1-4 seal_bundle,
        never the one-time envelope), persist it + a single-use first-boot bootstrap
        token, and return (raw_bootstrap_token, raw_callback_token) for the renderer to
        bake into box.env. Fail closed (RuntimeError -> dispatch_failed).

        A retry (a bundle already exists for this deployment) REUSES the stored bundle —
        so the owner OTP already baked into it is never re-minted — and only re-mints a
        fresh bootstrap token."""
        settings = self.settings
        # G1-1: never ship a bundle whose accepted wrapper-key set excludes MC's active
        # signer (that would strand the box at envelope_signature_invalid). Skipped when
        # emission is off (no private key) — nothing to sign with, nothing to brick.
        if not active_signer_in_served_set(settings):
            raise RuntimeError(
                "active_signer_not_in_public_key_set: refusing to provision a box whose "
                "accepted wrapper-key set excludes MC's active desired-state signer")

        if self.prov_store.get_secret_bundle(run.deployment_id) is None:
            _, fleet_token = mint_deployment_fleet_key(
                self.fleet_store, run.deployment_id,
                label=f"box:{run.deployment_id}", now_iso=self.now_iso())
            bundle = {
                "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
                "REDIS_PASSWORD": secrets.token_urlsafe(32),
                "ONEBRAIN_FLEET_KEY": fleet_token,
                "ONEBRAIN_LLM_API_KEY": getattr(settings, "llm_api_key", "") or "",
                "ONEBRAIN_ADMIN_PASSWORD": owner_otp,
                "ONEBRAIN_SERVICE_KEY": service_key,
                "ONEBRAIN_SPACE_ID": space_id,
                "UPDATE_BACKUP_KEY": secrets.token_urlsafe(32),
                "UPDATE_DESIRED_STATE_PUBLIC_KEYS": (
                    settings.fleet_desired_state_public_keys
                    or settings.fleet_desired_state_public_key),
                "ONEBRAIN_DNS_TOKEN": "",   # empty for a customer box (only the MC box sets it, P5-06)
            }
            errors = validate_bundle(bundle)
            if errors:
                raise RuntimeError(f"secret bundle invalid: {errors[0]}")
            cipher = OneTimeSecretCipher(settings)
            self.prov_store.upsert_secret_bundle(BoxSecretBundle(
                deployment_id=run.deployment_id,
                account_id=run.account_id,
                ciphertext=cipher.seal_bundle(json.dumps(bundle)),
                key_version=cipher.key_version,
                secrets_epoch=0,
            ))

        # Mint the single-use, short-TTL first-boot token (fresh on every dispatch, incl.
        # a retry). Only its hash is stored; the raw token is baked into user-data.
        _, token_secret, raw_token = generate_bootstrap_token()
        ttl = max(1, int(getattr(settings, "fleet_bootstrap_token_ttl_seconds", 3600) or 3600))
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        self.prov_store.create_bootstrap_token(BoxBootstrapToken(
            token_hash=hash_secret(token_secret),
            deployment_id=run.deployment_id,
            account_id=run.account_id,
            expires_at=expires.isoformat(),
        ))
        # G1-7: the callback bearer is minted per-run and baked in user-data (box.env),
        # NEVER placed in the exchange bundle, so fail_cb authenticates before the exchange.
        callback_token = secrets.token_urlsafe(32)
        return raw_token, callback_token
