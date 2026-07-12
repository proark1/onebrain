"""Probe co-located module health endpoints for the heartbeat. Enabled only when
ONEBRAIN_MODULE_PROBES_ENABLED=true AND ONEBRAIN_LOCAL_MODULES names modules —
(A4: the settings field is module_probes_enabled, which env_prefix="ONEBRAIN_"
maps to ONEBRAIN_MODULE_PROBES_ENABLED — the P1 compose convention MUST use
this exact name; probes fail quiet, so a misspelled var silently stays off) —
both unset on Railway, so the fleet's heartbeats gain no module claims until
the compose boxes (P1) set them. Probes address
http://<host or module_id>:<port><path> — cross-container by compose service
name, not 127.0.0.1."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import List, Optional

from app.fleet.heartbeat import ModuleReport
from app.module_manifest import MODULE_HEALTH_PROBES, HealthProbe, parse_local_modules


def _is_connection_refused(exc: Exception) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    # urllib wraps socket errors: URLError(reason=ConnectionRefusedError(...)).
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ConnectionRefusedError)


def probe_module(probe: HealthProbe, *, opener=None, timeout: float = 2.0) -> Optional[ModuleReport]:
    """kind=='none' -> None (no listener: report NOTHING rather than fabricate).
    http: healthy = status < 500; connection-refused with
    fail_open_on_connection_refused -> healthy=True (comm-workers policy);
    any other exception -> healthy=False. version stays '' in P0 (module version
    truth arrives with the P3 update-state channel). opener(request, timeout) is
    injectable (house style: reporter.send_heartbeat)."""
    if probe.kind != "http":
        return None
    host = probe.host or probe.module_id
    request = urllib.request.Request(f"http://{host}:{probe.port}{probe.path}", method="GET")
    do_open = opener or (lambda req, t: urllib.request.urlopen(req, timeout=t))
    try:
        with do_open(request, timeout) as response:
            status = getattr(response, "status", 0) or response.getcode()
    except urllib.error.HTTPError as exc:  # an HTTP answer IS a live listener
        status = exc.code
        exc.close()  # the error carries a live response body — do not leak it
    except Exception as exc:
        if probe.fail_open_on_connection_refused and _is_connection_refused(exc):
            return ModuleReport(module_id=probe.module_id, healthy=True)
        return ModuleReport(module_id=probe.module_id, healthy=False)
    return ModuleReport(module_id=probe.module_id, healthy=status < 500)


def collect_module_reports(settings, *, opener=None) -> List[ModuleReport]:
    """[] unless settings.module_probes_enabled and settings.local_modules;
    resolves probes via app.module_manifest; skips unknown ids; per-module
    isolation (one probe blowing up yields healthy=False for that module only)."""
    if not getattr(settings, "module_probes_enabled", False):
        return []
    module_ids = parse_local_modules(getattr(settings, "local_modules", "") or "")
    if not module_ids:
        return []
    reports: List[ModuleReport] = []
    for module_id in module_ids:
        probe = MODULE_HEALTH_PROBES.get(module_id)
        if probe is None:
            continue
        try:
            report = probe_module(probe, opener=opener)
        except Exception:
            report = ModuleReport(module_id=module_id, healthy=False)
        if report is not None:
            reports.append(report)
    return reports
