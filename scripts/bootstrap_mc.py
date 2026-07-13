"""MC bootstrap runner (P5-06) — the ONE manual live step, as testable code.

MC cannot provision its own first box, so this CLI renders the MC box's cloud-init
(``role=="operator"``, the A14 operator overlay) and creates the box via an INJECTED
``HetznerClient`` (the real client on a live ``--no-dry-run``; ``FakeHetznerClient`` in
tests). Only the final ``--no-dry-run`` invocation with a real token is user-driven;
everything up to it is fake-tested.

The MC box is BAKED, not exchanged (G3-1 — CRITICAL). The customer-box model (MC holds
the bundle in ITS db, the customer box fetches it via ``/bootstrap``) is CIRCULAR for the
MC box: its ``fleet_url`` is its OWN url and it boots with an EMPTY DB, so a self-served
``/bootstrap`` 404s and its Postgres never comes up. Resolution: this runner renders the
full ``/opt/onebrain/.env`` DIRECTLY into cloud-init (``render_dotenv`` + the operator
overlay values). **No bootstrap token is minted and no ``/bootstrap`` exchange runs for
``role=="operator"``** — the token/exchange path is customer-box only.

MC self-enrolls at first boot (G3-2): the app's ``seed_operator_self_deployment`` creates
the ``mc`` deployment row + a fleet key matching the baked ``ONEBRAIN_FLEET_KEY`` in the
box's OWN db, so the reporter heartbeats to itself with no manual enroll. First-boot
in-compose migrate (G3-5) runs ``alembic upgrade head`` on the on-box DB — do NOT migrate
the MC DB "before boot" (impossible; the DB is created inside the box's compose stack).

Secret hygiene: the ONE Hetzner Cloud token (which now also covers DNS — the unified
Cloud API, GA 2025-11-10) is read from the environment (never a flag, never echoed); no
separate DNS token is needed. Dry-run prints the rendered cloud-init with every secret
VALUE REDACTED;
the create path prints only the request SHAPE (never the user-data). Dry-run is the
default; ``--no-dry-run`` is the single gate to a real create and hard-requires the token.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.controlplane.desired_state import active_signer_in_served_set  # noqa: E402
from app.fleet.bootstrap_bundle import render_dotenv, validate_bundle  # noqa: E402
from app.fleet.keys import generate_fleet_key  # noqa: E402
from app.provisioning.hetzner.broker import build_hetzner_broker  # noqa: E402
from app.provisioning.hetzner.client import (  # noqa: E402
    FLEET_LABEL_KEY,
    FLEET_LABEL_VALUE,
    DnsRecordRequest,
    FirewallCreateRequest,
    ServerCreateRequest,
    VolumeCreateRequest,
)
from app.provisioning.hetzner.provisioner import _default_deny_rules, _ssh_key_ids  # noqa: E402
from app.provisioning.hetzner.render import BoxRenderInputs, render_cloud_init  # noqa: E402

# The MC box's operator surface: the OneBrain API + admin UI + workers. No comm/assistant
# module (so ONEBRAIN_SERVICE_KEY / ONEBRAIN_SPACE_ID stay empty in the bundle).
_MC_MODULES = ("onebrain-api", "onebrain-admin-ui", "onebrain-workers")


@dataclass(frozen=True)
class McArtifacts:
    cloud_init: str
    bundle: dict
    server: ServerCreateRequest
    firewall: Optional[FirewallCreateRequest]
    volume: Optional[VolumeCreateRequest]
    dns: Optional[DnsRecordRequest]
    fleet_token: str
    secret_values: tuple
    # The MC box's own admin login (ONEBRAIN_ADMIN_EMAIL/PASSWORD). main() surfaces this
    # OUT-OF-BAND on the create path — it is the ONE credential the operator must keep (the
    # MC box seeds this admin at first boot; SSH is closed, so there is no other way in).
    # admin_password_generated => no operator-set ONEBRAIN_ADMIN_PASSWORD, so it was minted.
    admin_email: str
    admin_password: str
    admin_password_generated: bool


def build_mc_bundle(settings, *, dns_token: str, fleet_token: str,
                    admin_email: str, admin_password: str) -> dict:
    """The MC box's BAKED bundle (G3-1). Every foundational secret is freshly generated
    here (the MC box is never exchanged, so nothing is stored MC-side). ONEBRAIN_DNS_TOKEN
    is the UNIFIED Cloud API token (same as compute; MC may manage DNS through it); service
    key / space id are empty (MC runs no comm/assistant module). Mirrors the customer bundle
    in HetznerProvisioner._provision_box_secrets.

    ONEBRAIN_ADMIN_EMAIL/PASSWORD are the admin seed pair seed.py needs to make the MC box
    loginable (both REQUIRED bundle keys). Unlike a customer box's foundational secrets, the
    admin password is an OPERATOR-KNOWN value — resolved by build_mc_artifacts (the operator's
    ONEBRAIN_ADMIN_PASSWORD, else a freshly minted one surfaced out-of-band) and threaded in
    here — because the operator must be able to log into Mission Control afterward."""
    return {
        "POSTGRES_PASSWORD": secrets.token_urlsafe(32),
        "REDIS_PASSWORD": secrets.token_urlsafe(32),
        "ONEBRAIN_FLEET_KEY": fleet_token,
        "ONEBRAIN_LLM_API_KEY": getattr(settings, "llm_api_key", "") or "",
        # Strong per-box session-cookie secret (64 hex chars). app/main.py refuses to boot
        # onebrain-api without a >=32-char non-default value; freshly minted here (never
        # stored MC-side — the MC box is baked, not exchanged).
        "ONEBRAIN_AUTH_SECRET": secrets.token_hex(32),
        "ONEBRAIN_ADMIN_EMAIL": admin_email,
        "ONEBRAIN_ADMIN_PASSWORD": admin_password,
        "ONEBRAIN_SERVICE_KEY": "",
        "ONEBRAIN_SPACE_ID": "",
        "UPDATE_BACKUP_KEY": secrets.token_urlsafe(32),
        "UPDATE_DESIRED_STATE_PUBLIC_KEYS": (
            settings.fleet_desired_state_public_keys or settings.fleet_desired_state_public_key),
        "ONEBRAIN_DNS_TOKEN": dns_token,
    }


def _parse_images(raw: str) -> dict:
    raw = raw or "{}"
    # Convenience: `--images-json @path` reads the JSON from a file. An inline JSON object is
    # painful to pass through PowerShell (5.1 strips the embedded double quotes when handing a
    # string to a native exe -> "Expecting property name ... char 1"); a single-quoted '@file'
    # has no inner quotes to mangle, so this is the robust cross-shell way to pass the map.
    if raw.startswith("@"):
        try:
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"--images-json @file could not be read: {exc}") from exc
    try:
        images = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--images-json is not valid JSON: {exc}") from exc
    if not isinstance(images, dict) or not all(isinstance(v, str) for v in images.values()):
        raise ValueError("--images-json must be a JSON object of module_id -> digest-pinned image ref")
    return images


def build_mc_artifacts(args, settings) -> McArtifacts:
    """Pure (no network): assemble the MC bundle, render the operator-overlay cloud-init
    with the bundle BAKED as /opt/onebrain/.env (G3-1), and build the create request
    shapes (default-deny firewall P5-05, server, optional volume + DNS). Raises ValueError
    on any fail-closed condition (G1-1 interlock, missing images, invalid bundle)."""
    # G1-1 preflight: never bake a box whose served wrapper-key set excludes MC's active
    # desired-state signer — it would fail its OWN G1-1 startup assertion on first boot.
    # Inert when emission is off (no private key): active_signer_in_served_set -> True.
    if not active_signer_in_served_set(settings):
        raise ValueError(
            "active_signer_not_in_public_key_set: the desired-state private key's derived public "
            "key is not in ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS — the MC box would fail its "
            "G1-1 startup assertion. Fix the served set (scripts/rotate_desired_state_key.py "
            "overlap-set) before bootstrapping.")

    images = _parse_images(args.images_json)
    missing = [m for m in _MC_MODULES if m not in images]
    if missing:
        raise ValueError(f"--images-json must cover the MC modules; missing: {missing}")

    # The MC box seeds its OWN admin from ONEBRAIN_ADMIN_EMAIL/PASSWORD at first boot (seed.py).
    # SSH is closed and the MC box is never exchanged, so a box with no admin email is a box the
    # operator can never log into. FAIL CLOSED without it (both are REQUIRED bundle keys).
    admin_email = (settings.admin_email or "").strip().lower()
    if not admin_email:
        raise ValueError(
            "ONEBRAIN_ADMIN_EMAIL required — the MC box seeds no admin and you cannot log in "
            "without it")
    # The admin password is an OPERATOR-KNOWN value: use the operator's ONEBRAIN_ADMIN_PASSWORD
    # when set, else mint one and flag it so main() surfaces it out-of-band on the create path
    # (it is the ONE login credential the operator must keep). Either way it is baked into the
    # MC box's /opt/onebrain/.env, so the seeded admin's password matches what is surfaced.
    admin_password = settings.admin_password or ""
    admin_password_generated = not admin_password
    if admin_password_generated:
        admin_password = secrets.token_urlsafe(32)

    # DNS now rides the UNIFIED Cloud API (GA 2025-11-10): the SAME Hetzner Cloud token as
    # compute covers DNS — there is no separate ONEBRAIN_FLEET_DNS_TOKEN. Baked as the box's
    # ONEBRAIN_DNS_TOKEN so the MC box can manage DNS through the Cloud API at runtime.
    dns_token = settings.hetzner_api_token
    _, _, fleet_token = generate_fleet_key()
    bundle = build_mc_bundle(settings, dns_token=dns_token, fleet_token=fleet_token,
                             admin_email=admin_email, admin_password=admin_password)
    errors = validate_bundle(bundle)
    if errors:
        raise ValueError(f"MC secret bundle invalid: {errors[0]}")

    # The baked /opt/onebrain/.env: the bundle (render_dotenv) PLUS the operator-overlay
    # ${VAR} values the render references but that are NOT bundle keys (the desired-state
    # PRIVATE key — MC's own signing key from escrow — and the callback allowed-hosts).
    dotenv = render_dotenv(bundle)
    private_key = getattr(settings, "fleet_desired_state_private_key", "") or ""
    overlay = [
        # Arm Mission Control: is_operator_surface is a read-only @property, so the render's
        # ONEBRAIN_IS_OPERATOR_SURFACE=true does NOT set operator_mode. Bake the settable
        # field so the fleet router + G3-2 self-seed + P5-04 scheduler come up on the MC box.
        ("ONEBRAIN_OPERATOR_MODE", "true"),
        ("ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY", private_key),
        # The APP-level accepted wrapper-key SET (G1-1). Sourced from the operator's own
        # served set — build_mc_artifacts' preflight above already asserts it contains the
        # active signer — so the MC box passes its OWN G1-1 startup assertion (and /bootstrap
        # never 409s a customer bundle). The render references it as a ${VAR} in onebrain-api.env.
        ("ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS",
         settings.fleet_desired_state_public_keys or settings.fleet_desired_state_public_key),
        ("ONEBRAIN_PROVISIONING_CALLBACK_ALLOWED_HOSTS",
         getattr(settings, "provisioning_callback_allowed_hosts", "") or ""),
    ]
    dotenv += "".join(f"{k}={v}\n" for k, v in overlay)

    deployment_id = args.deployment_id
    compose_project = f"onebrain-{deployment_id}"
    fqdn = args.fqdn or ""
    # The callback bearer is baked in box.env (G1-7). On the MC box the callback posts to
    # itself and 404s harmlessly (no provisioning run tracks the MC box) — the MC box's
    # real success signal is its self-heartbeat, not this callback.
    callback_token = secrets.token_urlsafe(32)
    cloud_init = render_cloud_init(BoxRenderInputs(
        deployment_id=deployment_id,
        account_id=args.account_id,
        compose_project=compose_project,
        enabled_modules=_MC_MODULES,
        images={m: images[m] for m in _MC_MODULES},
        fqdn=fqdn,
        fleet_url=args.fleet_public_url,
        run_id=f"{deployment_id}-bootstrap",   # synthetic (no provisioning run for the MC box)
        fleet_public_desired_state_key=settings.fleet_desired_state_public_key,
        release_public_key=settings.release_verify_public_key,
        registry_allowlist=settings.release_registry_allowlist,
        role="operator",          # A14 overlay + G3-1: no bootstrap token, no /bootstrap exchange step
        bootstrap_token="",       # G3-1: the MC box is NEVER minted a first-boot token
        callback_token=callback_token,
        dotenv=dotenv,            # G3-1: the baked /opt/onebrain/.env body
    ))

    firewall = None
    if not settings.hetzner_firewall_id:
        firewall = FirewallCreateRequest(
            name=f"{compose_project}-fw",
            rules=_default_deny_rules(settings.hetzner_firewall_allow_ssh),
            labels={"deployment_id": deployment_id, "role": "operator"})
    server = ServerCreateRequest(
        name=compose_project,
        server_type=args.server_type or settings.hetzner_server_type,
        image=args.image or settings.hetzner_image,
        location=args.location or settings.hetzner_location,
        user_data=cloud_init,
        ssh_key_ids=_ssh_key_ids(settings.hetzner_ssh_key_ids),
        firewall_ids=(settings.hetzner_firewall_id,) if settings.hetzner_firewall_id else (),
        # The constant fleet label (alongside deployment_id + role) so the MC box is counted
        # by the fleet-size cap and deduped by the broker's deployment_id idempotency gate —
        # the MC box can never be accidentally created twice.
        labels={"deployment_id": deployment_id, "role": "operator",
                FLEET_LABEL_KEY: FLEET_LABEL_VALUE})
    volume = None
    if settings.hetzner_volume_size_gb > 0:
        volume = VolumeCreateRequest(
            name=f"{compose_project}-data", size_gb=settings.hetzner_volume_size_gb,
            location=args.location or settings.hetzner_location,
            labels={"deployment_id": deployment_id, "role": "operator"})
    dns = None
    if fqdn and settings.fleet_dns_provider == "hetzner" and settings.fleet_dns_zone_id:
        # Zone-relative LABEL (deployment_id), NOT the full fqdn: the Cloud API RRSet name is
        # relative to the zone, so name=fqdn would resolve as "mc.<zone>.<zone>" and never
        # match on re-provision. fqdn stays the box hostname (Caddy TLS + external_run_url).
        # The operator sets --fqdn <deployment_id>.<zone>. No token gate here: this builds the
        # request SHAPE (pure/no-network); the create path hard-requires the Cloud token.
        dns = DnsRecordRequest(zone_id=settings.fleet_dns_zone_id, name=deployment_id, ipv4="", ttl=300)

    # Everything that must NEVER be echoed inside the printed cloud-init: the bundle values
    # (incl. the baked ONEBRAIN_ADMIN_PASSWORD) + the desired-state private key + the callback
    # token. admin_password is listed explicitly too (belt-and-suspenders — it is already a
    # bundle value) so it stays REDACTED from the printed cloud-init regardless of how the
    # bundle is assembled; main() surfaces it out-of-band, never via the cloud-init dump.
    # (Referenced by name, not overlay position, so reordering the overlay never silently
    # drops the crown-jewel private key from redaction.)
    secret_values = tuple(
        v for v in (list(bundle.values()) + [private_key, callback_token, admin_password]) if v)
    return McArtifacts(cloud_init=cloud_init, bundle=bundle, server=server, firewall=firewall,
                       volume=volume, dns=dns, fleet_token=fleet_token, secret_values=secret_values,
                       admin_email=admin_email, admin_password=admin_password,
                       admin_password_generated=admin_password_generated)


def _redact(text: str, secret_values) -> str:
    """Mask every secret VALUE (longest first, so a secret that is a substring of another
    is redacted correctly) so a printed cloud-init never leaks a baked secret."""
    for value in sorted({v for v in secret_values if v}, key=len, reverse=True):
        text = text.replace(value, "***REDACTED***")
    return text


def _create_shape(art: McArtifacts) -> dict:
    """The create request SHAPE — structural only, NO secrets (the user-data is reduced to
    its byte length). Safe to print on the create path + as a dry-run summary."""
    return {
        "server": {
            "name": art.server.name, "server_type": art.server.server_type,
            "image": art.server.image, "location": art.server.location,
            "firewall_ids": list(art.server.firewall_ids), "ssh_key_ids": list(art.server.ssh_key_ids),
            "user_data_bytes": len(art.server.user_data),
        },
        "firewall": None if art.firewall is None else {
            "name": art.firewall.name,
            "inbound_rules": [f"{r.protocol}/{r.port}" if r.port else r.protocol for r in art.firewall.rules],
        },
        "volume": None if art.volume is None else {"name": art.volume.name, "size_gb": art.volume.size_gb},
        "dns": None if art.dns is None else {"zone_id": art.dns.zone_id, "name": art.dns.name},
        "enabled_modules": list(_MC_MODULES),
    }


def _runbook(args) -> str:
    return (
        "# --- Verification runbook -------------------------------------------\n"
        "# The MC box SELF-SEEDS its `mc` deployment row + fleet key at first boot (G3-2)\n"
        "# and heartbeats to ITSELF (fleet_url = its own public url) — no manual enroll, no\n"
        "# admin session. The first-boot in-compose migrate (G3-5) runs `alembic upgrade\n"
        "# head` on the on-box DB automatically; do NOT migrate the MC DB by hand.\n"
        "# No bootstrap token is printed (none is minted for the MC box — it is BAKED, G3-1).\n"
        "# The Mission Control admin login (email + password) IS printed ONCE on the create\n"
        "# path (the `SAVE THIS - Mission Control admin login:` line above) — SAVE IT: the MC\n"
        "# box seeds that admin at first boot and there is no other way in (SSH is closed).\n"
        "#\n"
        "# Wait a few minutes for first boot + in-compose migrate, then confirm the heartbeat:\n"
        f"#   curl -s {args.fleet_public_url.rstrip('/')}/api/fleet/overview \\\n"
        "#     -H 'Authorization: Bearer <operator-admin-session-or-key>' \\\n"
        "#     | jq '.deployments[] | select(.deployment_id==\"" + args.deployment_id + "\")'\n"
        "# Success = the mc deployment appears with a RECENT reported_at + applied_secrets_epoch.\n"
        "#\n"
        "# LIVE-STEP prerequisites (do first): migrate the operator/owner DSN to head (the one\n"
        "# operator-DSN dependency), and ESCROW both ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY and\n"
        "# ONEBRAIN_SECRET_ENCRYPTION_KEY offline (G3-7 — a single MC box is a single point of loss)."
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bootstrap_mc", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--deployment-id", default="mc", help="the MC deployment id (default: mc)")
    parser.add_argument("--account-id", default="", help="the operator's platform account id")
    parser.add_argument("--fleet-public-url", required=True,
                        help="the MC box's own public URL (the reporter heartbeats to it)")
    parser.add_argument("--images-json", required=True,
                        help="JSON map module_id -> digest-pinned image ref (must cover the MC modules). "
                             "Accepts inline JSON or @path to read it from a file (use @file on PowerShell, "
                             "which mangles inline double quotes).")
    parser.add_argument("--server-type", default="", help="Hetzner server type (default: settings.hetzner_server_type)")
    parser.add_argument("--location", default="", help="Hetzner location (default: settings.hetzner_location)")
    parser.add_argument("--image", default="", help="Hetzner OS image (default: settings.hetzner_image)")
    parser.add_argument("--fqdn", default="", help="the MC box's fqdn (Caddy TLS + optional DNS A record)")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True,
                        help="render+validate+print only (default). --no-dry-run creates the real box "
                             "(hard-requires ONEBRAIN_HETZNER_API_TOKEN in env).")
    return parser


def main(argv=None, *, settings=None, client=None) -> int:
    args = _build_parser().parse_args(argv)
    settings = settings if settings is not None else get_settings()

    try:
        artifacts = build_mc_artifacts(args, settings)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    shape = _create_shape(artifacts)
    runbook = _runbook(args)

    if args.dry_run:
        # Local eyeball, NO Hetzner call. The rendered cloud-init is printed with every
        # secret VALUE redacted (defense in depth); the create SHAPE carries no secrets.
        print("# --- DRY RUN (no Hetzner call) --------------------------------------")
        print("# rendered cloud-init (secret values redacted):")
        print(_redact(artifacts.cloud_init, artifacts.secret_values))
        print("\n# create request SHAPE:")
        print(json.dumps(shape, indent=2))
        # Surface the admin EMAIL (not secret) so the operator can eyeball it; deliberately
        # print NO real password value in dry-run — note only where it will come from.
        print("\n# Mission Control admin login (surfaced out-of-band on --no-dry-run):")
        print(f"#   email:    {artifacts.admin_email}")
        if artifacts.admin_password_generated:
            print("#   password: will be GENERATED and printed once on --no-dry-run "
                  "(set ONEBRAIN_ADMIN_PASSWORD to choose your own instead)")
        else:
            print("#   password: the value you set via ONEBRAIN_ADMIN_PASSWORD "
                  "(printed once on --no-dry-run)")
        print("\n" + runbook)
        return 0

    # Create path (--no-dry-run). Hard-require the Hetzner token in env (never a flag,
    # never echoed) and NEVER print the cloud-init/user-data (it carries every secret) —
    # only the SHAPE. Never proceed tokenless.
    if not getattr(settings, "hetzner_api_token", ""):
        print("error: --no-dry-run requires ONEBRAIN_HETZNER_API_TOKEN in the environment "
              "(never passed as a flag). Aborting without creating anything.", file=sys.stderr)
        return 2
    try:
        broker = build_hetzner_broker(settings, client=client)
    except RuntimeError as exc:   # A6 guard / unbuilt remote broker
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("# create request SHAPE:")
    print(json.dumps(shape, indent=2))
    try:
        result = broker.provision_box(server=artifacts.server, volume=artifacts.volume,
                                      dns=artifacts.dns, firewall=artifacts.firewall)
    except RuntimeError as exc:   # fleet-size cost cap (or A6 guard) — abort, create nothing
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if result.reused:
        # SELF-GUARD (G3-1 hygiene): the broker's idempotency gate found an existing MC box
        # and created NOTHING. Say so plainly instead of implying a fresh create — a second
        # MC box was NOT (and must not be) made. The freshly assembled artifacts (incl. a
        # generated admin password) were NOT applied, so we deliberately do NOT reprint the
        # "SAVE THIS" login: the live box keeps the credential baked at its original create.
        print(f"\n# MC already exists (server_id={result.server_id} ip={result.public_ipv4}) "
              "— reused, NOT recreated")
        print("# The MC admin login is unchanged from the original create (this run baked no new box).")
        print("\n" + runbook)
        return 0

    print(f"\n# MC box created: server_id={result.server_id} ip={result.public_ipv4} "
          f"firewall_id={result.firewall_id or '(pre-existing)'} fqdn={result.fqdn or '(none)'}")
    # OUT-OF-BAND: printed to the operator's OWN terminal, NOT into cloud-init/user-data. This
    # is the ONE credential the operator must keep — the MC box seeds this admin at first boot
    # and there is no other login (SSH is closed). The password matches the value baked into the
    # box's /opt/onebrain/.env (operator-set ONEBRAIN_ADMIN_PASSWORD, else the minted one).
    print(f"\n# SAVE THIS - Mission Control admin login: {artifacts.admin_email} / {artifacts.admin_password}")
    print("\n" + runbook)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
