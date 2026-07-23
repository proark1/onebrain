"""The Hetzner provisioning executor (P4-03).

It renders the box's cloud-init (P4-02) and calls the token-isolating broker
(P4-01), reusing the provisioning run state machine, callback, and one-time
secret envelope.

Pure of any live call — the broker is injected (a `FakeHetznerClient`-backed
`InProcessHetznerBroker` in every test). No readiness poll, no smoke check: the
box's own cloud-init posts the existing provisioning callback (`apply_callback`)
with the smoke result and `bootstrap_password`.

D-6 slot convention (decided Phase 3, docs/hetzner-migration-sequence.md): a
successful dispatch writes `railway_project_id = "hetzner:<server_id>"`,
`railway_environment_id = "<compose_project>"`, and
`result_payload["service_ids"] = {module_id: compose_service_name}` (service ==
module id) so `resolve_provisioned_target()` / `target_provider()` classify the run
as hetzner with NO schema migration.

A15 (fleet-key mint/registration — producer already exists, cited not rebuilt):
the box authenticates the desired-state GET (P4-05) + heartbeat via a
deployment-pinned fleet key. That hash-only registration path already exists at
`app/fleet/enrollment.py:mint_deployment_fleet_key`. In P4 the renderer emits
`ONEBRAIN_FLEET_KEY` as a `${VAR}` placeholder (delivery is the Phase-5
bootstrap-token exchange), so it is not exercised end-to-end here (dormant).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.controlplane.desired_state import active_signer_in_served_set
from app.platform.base import DEFAULT_LOCALE, normalize_locale
from app.fleet.bootstrap_bundle import validate_bundle
from app.fleet.enrollment import mint_deployment_fleet_key
from app.fleet.keys import generate_bootstrap_token, hash_secret
from app.provisioning.hetzner.broker import HetznerBroker
from app.provisioning.hetzner.client import (
    FLEET_LABEL_KEY,
    FLEET_LABEL_VALUE,
    DnsRecordRequest,
    FirewallCreateRequest,
    FirewallRule,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.render import BoxRenderInputs, enabled_product_dbs, render_cloud_init
from app.provisioning.customer_bootstrap import CustomerBootstrapDescriptor, encode_customer_bootstrap
from app.provisioning.runs import (
    STATUS_DISPATCHED,
    BoxBootstrapToken,
    BoxSecretBundle,
    OneTimeSecretCipher,
    ProvisioningRun,
    hash_callback_secret,
    now_iso,
)


_PRODUCTION_ENVIRONMENTS = {"prod", "production", "staging"}
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


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


def _ssh_key_ids(csv: str) -> tuple:
    """Parse the operator-config csv of Hetzner SSH keys (break-glass only) — accepts
    numeric ids OR key NAMES (the Hetzner API's create-server `ssh_keys` field takes
    either; the console shows names, so operators reference by name). A numeric entry
    is sent as an int id; anything else is passed through as a key name. Blank entries
    dropped. No inbound-22 path a bad entry could open (H-3 firewall never opens 22
    unless hetzner_firewall_allow_ssh)."""
    out: list = []
    for part in (csv or "").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part) if part.isdigit() else part)
    return tuple(out)


def _provider_hostname_label(value: str) -> str:
    """Map a normalized deployment id to one stable RFC 1123 label."""
    label = value.strip().lower().replace("_", "-").strip("-")
    if len(label) <= 63:
        return label
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{label[:54].rstrip('-')}-{digest}"


def _is_valid_dns_hostname(value: str) -> bool:
    """Accept a DNS hostname Caddy can obtain a certificate for.

    The renderer's broader injection guard deliberately permits some values
    that are useful for internal identifiers (notably underscores).  A public
    TLS hostname needs stricter RFC 1123 labels, however, otherwise a
    production deployment could be created with Caddy silently unable to
    obtain a certificate.
    """
    hostname = (value or "").strip().rstrip(".").lower()
    if not hostname or len(hostname) > 253:
        return False
    labels = hostname.split(".")
    return len(labels) >= 2 and all(_DNS_LABEL_RE.fullmatch(label) for label in labels)


def _requires_public_tls(settings, deployment) -> bool:
    """Whether this target is a production-like customer deployment.

    Check both the control plane's own environment and the persisted target
    environment.  That prevents a mistakenly "development" MC process from
    creating an HTTP-only production customer box.
    """
    return bool(getattr(settings, "is_production_like", False)) or (
        str(getattr(deployment, "environment", "")).strip().lower()
        in _PRODUCTION_ENVIRONMENTS
    )


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
        if self.settings.provisioner_backend != "hetzner":
            return False
        if getattr(self.settings, "hetzner_broker_url", ""):
            return True
        return bool(self.settings.hetzner_api_token) and self.settings.hetzner_allow_inprocess_broker

    def dispatch(self, run: ProvisioningRun, *, owner_otp: str = "",
                 service_key: str = "", space_id: str = "", owner_email: str = "",
                 integration_credentials: dict[str, tuple[str, str]] | None = None) -> ProvisioningRun:
        # owner_otp / service_key / space_id / owner_email are threaded in from the
        # bundle-assembly seam (provision_customer, G3-3): they are minted/collected by
        # CustomerProvisioner and do NOT flow to dispatch on their own. They populate the
        # box's secret bundle — owner_email + owner_otp are the ONEBRAIN_ADMIN_EMAIL /
        # ONEBRAIN_ADMIN_PASSWORD pair seed.py needs to create a loginable box admin.
        if not self.enabled:
            raise RuntimeError(
                "Hetzner provisioning is not configured: use a remote broker, or an explicit in-process dogfood token."
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
        deployment = self.control_store.get_deployment(run.deployment_id)
        if not deployment:
            raise RuntimeError(f"deployment {run.deployment_id} no longer exists")
        customer_bootstrap = encode_customer_bootstrap(CustomerBootstrapDescriptor(
            account_id=run.account_id,
            account_kind=str(run.request_payload.get("account_kind", "organization")),
            customer_name=deployment.customer_name,
            module_ids=run.module_ids,
        ))

        # Development candidates are signed by a CI-only key that customer boxes
        # must never trust. Select the baked release verifier from persisted
        # deployment purpose, not from caller-controlled workflow payload.
        is_development = deployment.environment == "development"
        release_public_key = (
            getattr(settings, "dev_release_verify_public_key", "")
            if is_development
            else settings.release_verify_public_key
        )
        if is_development and not release_public_key:
            raise RuntimeError(
                "development provisioning requires ONEBRAIN_DEV_RELEASE_VERIFY_PUBLIC_KEY"
            )

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
        provider_label = _provider_hostname_label(run.deployment_id)
        compose_project = _provider_hostname_label(f"onebrain-{run.deployment_id}")
        dns_provider = (settings.fleet_dns_provider or "").strip().lower()
        base_domain = (settings.fleet_base_domain or "").strip().rstrip(".").lower()
        dns_enabled = (
            dns_provider == "hetzner"
            and bool(base_domain and settings.fleet_dns_zone_id)
        )
        if _requires_public_tls(settings, deployment):
            if not dns_enabled:
                raise RuntimeError(
                    "production Hetzner provisioning requires ONEBRAIN_FLEET_DNS_PROVIDER=hetzner, "
                    "ONEBRAIN_FLEET_BASE_DOMAIN, and ONEBRAIN_FLEET_DNS_ZONE_ID; refusing to "
                    "create an HTTP-only/raw-IP customer box"
                )
            if not _is_valid_dns_hostname(base_domain):
                raise RuntimeError(
                    "ONEBRAIN_FLEET_BASE_DOMAIN must be a valid public DNS hostname for "
                    "production Hetzner provisioning"
                )
        fqdn = f"{provider_label}.{base_domain}" if dns_enabled else ""

        # 4a. Mint + persist the box secret bundle and a single-use bootstrap token
        #     (P5-03). Fail closed: an invalid bundle or a G1-1 interlock violation
        #     raises RuntimeError -> dispatch_failed, so a box that can't come up (or
        #     one whose accepted wrapper-key set would exclude MC's active signer) is
        #     never created. Skipped when the stores aren't injected (pure-executor tests).
        bootstrap_token = ""
        callback_token = ""
        if self.prov_store is not None and self.fleet_store is not None:
            bootstrap_token, callback_token = self._provision_box_secrets(
                run, owner_otp=owner_otp, service_key=service_key, space_id=space_id,
                owner_email=owner_email, integration_credentials=integration_credentials,
                enabled_modules=tuple(modules))

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
                callback_url=str(run.request_payload.get("callback_url", "") or ""),
                postgres_app_role=getattr(settings, "postgres_app_role", "") or "onebrain_app",
                postgres_worker_role=getattr(settings, "postgres_worker_role", "") or "onebrain_worker",
                postgres_assistant_role=getattr(settings, "postgres_assistant_role", "") or "assistant_app",
                postgres_communication_role=getattr(settings, "postgres_communication_role", "") or "communication_app",
                fleet_public_desired_state_key=settings.fleet_desired_state_public_key,
                release_public_key=release_public_key,
                release_version=release.version,
                release_migration=release.migration_to,
                module_versions=dict(release.modules),
                registry_allowlist=settings.release_registry_allowlist,
                bootstrap_token=bootstrap_token,
                callback_token=callback_token,
                customer_bootstrap=customer_bootstrap,
                # UI language rides a plain box.env value (not the strict descriptor),
                # so a box on an older release ignores it instead of failing bootstrap.
                customer_default_locale=normalize_locale(
                    str(run.request_payload.get("default_locale", DEFAULT_LOCALE))
                ),
                # BK3: non-secret offsite-backup config baked into box.env (the two S3 creds ride
                # the sealed bundle as ${VAR} refs). backup_dbs = the enabled products' DB names.
                backup_enabled=bool(getattr(settings, "backup_enabled", False)),
                backup_s3_endpoint=getattr(settings, "backup_object_store_endpoint", "") or "",
                backup_s3_bucket=getattr(settings, "backup_object_store_bucket", "") or "",
                backup_s3_region=getattr(settings, "backup_object_store_region", "") or "",
                backup_retention_days=int(getattr(settings, "backup_retention_days", 30) or 30),
                backup_dbs=enabled_product_dbs(tuple(modules)),
                drive_policy_mode=(
                    "storage_and_indexing"
                    if getattr(settings, "pii_phase", "synthetic") == "dpia_signed"
                    else "storage_only"
                ),
                pii_phase=getattr(settings, "pii_phase", "synthetic"),
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
                # The fleet label goes on EVERY resource, not just the server, so teardown can
                # prove OneBrain ownership before deleting (never a resource merely sharing the id).
                labels={"deployment_id": run.deployment_id, FLEET_LABEL_KEY: FLEET_LABEL_VALUE},
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
            # The constant fleet label goes on EVERY box (alongside deployment_id) so the
            # broker's fleet-size cap counts it, and the deployment_id label is the broker's
            # idempotency key (exactly one server per deployment).
            labels={"deployment_id": run.deployment_id, FLEET_LABEL_KEY: FLEET_LABEL_VALUE},
        )
        volume = None
        if settings.hetzner_volume_size_gb > 0:
            volume = VolumeCreateRequest(
                name=f"{compose_project}-data",
                size_gb=settings.hetzner_volume_size_gb,
                location=settings.hetzner_location,
                # Fleet ownership label (see the firewall above) so teardown scopes volume
                # deletion to OneBrain-owned volumes.
                labels={"deployment_id": run.deployment_id, FLEET_LABEL_KEY: FLEET_LABEL_VALUE},
            )
        dns = None
        if dns_enabled:
            # The Cloud API RRSet name is RELATIVE to the zone, so the A-record name is the
            # zone-relative LABEL (deployment_id), NOT the full fqdn — a "dep_a.fleet.example"
            # RRSet in zone fleet.example would resolve as "dep_a.fleet.example.fleet.example"
            # (and the upsert's name-keyed RRSet probe would never match -> a fresh RRSet on
            # every re-provision). fqdn is kept below for the box hostname / external_run_url.
            dns = DnsRecordRequest(zone_id=settings.fleet_dns_zone_id, name=provider_label, ipv4="", ttl=300)

        # A HetznerApiError is a RuntimeError; _dispatch_run maps it to
        # dispatch_failed.
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
            # Root-disk Hetzner Backups state (observability; makes the extra backup image
            # discoverable at teardown — the offsite pg_dump prefix is added by BK8).
            "hetzner_backups": bool(result.backups_enabled),
        }
        result_payload = {
            **run.result_payload,
            "service_ids": {module_id: module_id for module_id in modules},
            "erasure_manifest": erasure_manifest,
        }
        # G1-7: MC verifies /runs/<id>/callback against this per-run token hash.
        # The box bakes the corresponding ONEBRAIN_PROVISIONING_CALLBACK_TOKEN;
        # its done_cb/fail_cb authenticate only for this run. The hash is present
        # only when a callback token was minted (stores injected).
        if callback_token:
            result_payload["callback_token_hash"] = hash_callback_secret(callback_token)
        return replace(
            run,
            status=STATUS_DISPATCHED,
            external_provider="hetzner",
            railway_project_id=f"hetzner:{result.server_id}",
            railway_environment_id=compose_project,
            # The public hostname is the full fqdn we constructed (the DNS record carries only
            # the zone-relative label); fall back to the raw IP when DNS is disabled.
            external_run_url=fqdn or result.public_ipv4,
            result_payload=result_payload,
            dispatched_at=self.now_iso(),
        )

    def _provision_box_secrets(self, run: ProvisioningRun, *, owner_otp: str,
                               service_key: str, space_id: str,
                               owner_email: str = "",
                               integration_credentials: dict[str, tuple[str, str]] | None = None,
                               enabled_modules: tuple[str, ...] = ()) -> tuple[str, str]:
        """Mint the box's real secrets, seal the RE-READABLE bundle (G1-4 seal_bundle,
        never the one-time envelope), persist it + a single-use first-boot bootstrap
        token, and return (raw_bootstrap_token, raw_callback_token) for the renderer to
        bake into box.env. Fail closed (RuntimeError -> dispatch_failed).

        A retry (a bundle already exists for this deployment) REUSES the stored bundle —
        so the owner OTP already baked into it is never re-minted — and only re-mints a
        fresh bootstrap token."""
        settings = self.settings
        if integration_credentials is None:
            assistant_key = service_key
            communication_key = service_key
            communication_space_id = space_id
        else:
            assistant_key = integration_credentials.get("assistant", ("", ""))[0]
            communication_key, communication_space_id = integration_credentials.get(
                "communication", ("", "")
            )
        legacy_key = communication_key or assistant_key or service_key
        legacy_space_id = communication_space_id or space_id
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
                "POSTGRES_APP_PASSWORD": secrets.token_urlsafe(32),
                "POSTGRES_WORKER_PASSWORD": secrets.token_urlsafe(32),
                "POSTGRES_ASSISTANT_PASSWORD": secrets.token_urlsafe(32),
                "POSTGRES_COMMUNICATION_PASSWORD": secrets.token_urlsafe(32),
                "REDIS_PASSWORD": secrets.token_urlsafe(32),
                "ONEBRAIN_FLEET_KEY": fleet_token,
                "ONEBRAIN_LLM_API_KEY": getattr(settings, "llm_api_key", "") or "",
                # Strong per-box session-cookie secret (64 hex chars). app/main.py refuses to
                # boot onebrain-api without a >=32-char non-default value; a box provisioned
                # without it would crash-loop. Sealed into the re-readable bundle like every
                # other foundational secret and delivered via the /bootstrap exchange.
                "ONEBRAIN_AUTH_SECRET": secrets.token_hex(32),
                # Separate HMAC key for the shared multi-replica login limiter;
                # never reuse the cookie-signing secret across security roles.
                "ONEBRAIN_LOGIN_RATE_LIMIT_SECRET": secrets.token_hex(32),
                # The admin seed pair (seed.py needs BOTH). owner_email is the customer's
                # login email (normalized to match the platform owner User + the box's own
                # seed-time .strip().lower()); owner_otp is the one-time password. A REQUIRED
                # bundle key, so an empty email fails validate_bundle -> dispatch_failed.
                "ONEBRAIN_ADMIN_EMAIL": (owner_email or "").strip().lower(),
                "ONEBRAIN_ADMIN_PASSWORD": owner_otp,
                "ONEBRAIN_SERVICE_KEY": legacy_key,
                "ONEBRAIN_SPACE_ID": legacy_space_id,
                "ONEBRAIN_ASSISTANT_SERVICE_KEY": assistant_key,
                "ONEBRAIN_COMMUNICATION_SERVICE_KEY": communication_key,
                "ONEBRAIN_COMMUNICATION_SPACE_ID": communication_space_id,
                "UPDATE_BACKUP_KEY": secrets.token_urlsafe(32),
                "UPDATE_DESIRED_STATE_PUBLIC_KEYS": (
                    settings.fleet_desired_state_public_keys
                    or settings.fleet_desired_state_public_key),
                "ONEBRAIN_DNS_TOKEN": "",   # empty for a customer box (only the MC box sets it, P5-06)
                # BK3: the ONE shared fleet S3 credential delivered to every box (empty when
                # backups off). It MUST be a PUT/GET/LIST-only key (NO DELETE) — a shared delete
                # key would let any compromised box wipe every other tenant's DR history; retention
                # DELETE is the S3 lifecycle rule, not the box. Cross-tenant isolation is by the
                # <deployment_id>/ object prefix (derived on the box from ONEBRAIN_DEPLOYMENT_ID).
                "ONEBRAIN_BACKUP_S3_ACCESS_KEY": getattr(settings, "backup_object_store_access_key", "") or "",
                "ONEBRAIN_BACKUP_S3_SECRET_KEY": getattr(settings, "backup_object_store_secret_key", "") or "",
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

        stored = self.prov_store.get_secret_bundle(run.deployment_id)
        if stored is None:
            raise RuntimeError("secret bundle was not persisted")
        try:
            stored_bundle = json.loads(OneTimeSecretCipher(settings).open_bundle(stored.ciphertext))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("stored secret bundle cannot be opened") from exc
        if "assistant-service" in enabled_modules and not stored_bundle.get("ONEBRAIN_ASSISTANT_SERVICE_KEY"):
            raise RuntimeError(
                "stored secret bundle lacks the Assistant credential; replace the legacy box"
            )
        if any(module in enabled_modules for module in ("communication-api", "communication-workers")):
            if not stored_bundle.get("ONEBRAIN_COMMUNICATION_SERVICE_KEY"):
                raise RuntimeError(
                    "stored secret bundle lacks the Communication credential; replace the legacy box"
                )
            if not stored_bundle.get("ONEBRAIN_COMMUNICATION_SPACE_ID"):
                raise RuntimeError(
                    "stored secret bundle lacks the Communication space; replace the legacy box"
                )
        if (
            "assistant-service" in enabled_modules
            and any(module in enabled_modules for module in ("communication-api", "communication-workers"))
            and stored_bundle["ONEBRAIN_ASSISTANT_SERVICE_KEY"]
            == stored_bundle["ONEBRAIN_COMMUNICATION_SERVICE_KEY"]
        ):
            raise RuntimeError("Assistant and Communication credentials must be distinct")

        # Mint the single-use, short-TTL first-boot token (fresh on every dispatch, incl. a
        # retry). Only its hash is stored; the raw token is baked into user-data. Minting a
        # FRESH token on every re-dispatch is also the documented recovery path for a first-boot
        # box stranded by a lost /bootstrap 200 (token consumed, but the box never wrote .env):
        # re-provision reuses the re-readable stored bundle and hands the box a fresh, usable
        # token. See app/routers/fleet.py bootstrap_exchange "ACCEPTED RESIDUAL".
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
