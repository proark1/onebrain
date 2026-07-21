"""Public hostnames for provisioned boxes.

The label and fqdn rules live here rather than inside the Hetzner provisioner so
that a read-only caller can resolve a box's public address without importing the
broker client, the HTTP client and the box renderer along with it. The fleet
overview is exactly that caller: it wants a link to each deployment's console
and nothing else.

Nothing here talks to Hetzner or to DNS. These are pure string rules that must
stay in step with the names the provisioner actually creates -- see
``app/provisioning/hetzner/provisioner.py``, which imports the label function
from here so the two cannot drift.
"""

from __future__ import annotations

import hashlib
import ipaddress

# Statuses whose run never produced a box we should send an operator to.
_DEAD_RUN_STATUSES = frozenset({"failed", "cancelled", "dispatch_failed"})


def provider_hostname_label(value: str) -> str:
    """Map a normalized deployment id to one stable RFC 1123 label."""
    label = value.strip().lower().replace("_", "-").strip("-")
    if len(label) <= 63:
        return label
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{label[:54].rstrip('-')}-{digest}"


def deployment_fqdn(deployment_id: str, base_domain: str) -> str:
    """`<label>.<base_domain>`, or "" when no base domain is configured.

    This is the name the provisioner builds when DNS is enabled, so it also
    resolves for a box that was adopted rather than provisioned (the
    development gate) and therefore has no provisioning run to read a hostname
    off of.
    """
    base = (base_domain or "").strip().rstrip(".").lower()
    label = provider_hostname_label(deployment_id)
    if not base or not label:
        return ""
    return f"{label}.{base}"


def console_url(host: str) -> str:
    """Turn a stored hostname into an absolute, clickable console URL.

    ``ProvisioningRun.external_run_url`` holds a bare hostname with no scheme,
    and degrades to a raw IP when DNS is disabled. A schemeless value used as an
    href resolves relative to the page it is rendered on, so callers must go
    through here rather than interpolating the stored value directly.
    """
    host = (host or "").strip().rstrip("/")
    if not host:
        return ""
    if host.startswith("https://") or host.startswith("http://"):
        return host

    # A raw address means DNS was disabled for this box. The rendered Caddyfile
    # serves those on :80 with no certificate, so https would just fail to
    # connect -- there is no cert for an IP literal.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"https://{host}"
    return f"http://[{host}]" if address.version == 6 else f"http://{host}"


def resolve_console_url(deployment_id: str, runs, base_domain: str) -> str:
    """Best public console URL for one deployment, or "" when unknown.

    ``runs`` is that deployment's provisioning runs, newest first. A recorded
    hostname wins over the derived one because it is what was actually created:
    it stays correct for a box provisioned while DNS was disabled, which serves
    on a raw IP that no naming rule could reproduce.
    """
    for run in runs:
        if run.status in _DEAD_RUN_STATUSES:
            continue
        if run.external_run_url:
            return console_url(run.external_run_url)
    return console_url(deployment_fqdn(deployment_id, base_domain))
