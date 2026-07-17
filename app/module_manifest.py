"""Per-module deployment contracts: the env vars the provisioner must deliver and
the health probe the ground-truth reporter uses. Keyed by MODULE_IDS — the single
module vocabulary. Ports are the Dockerfiles' OWN defaults (verified in-repo),
never the retired workflow's port-masked :8080 wiring:
  onebrain Dockerfile EXPOSE 8000 · onebrain-web/Dockerfile EXPOSE 3000
  personalasisstant assistant-runtime Dockerfile EXPOSE 8000 (/health/ready)
  comm scripts/healthcheck.mjs: api 4000 · widget 5174 · voice 4100 · workers 4200
GROUND-TRUTH CAVEAT (C6): the real sources of truth live in three repos, two of
them not this one. Ports verified at build time against:
  onebrain                    b016c2e5a7e42901c20cb57c2cd13c75ebb69b44 (in-tree Dockerfiles)
  assaddar-ai-communication   9593d38bf9e0800806a42987af094e94ab752276 (scripts/healthcheck.mjs)
  personalasisstant           470e15b432576fa1f774b4014476aea4d3f91c88 (services/assistant-runtime/Dockerfile)
The in-repo test is a PIN against accidental edits, not a proof; the P1
provisioner checklist re-verifies ports against the built images.
Probe hosts default to the module id — the P1 compose files MUST name each
service after its module id (decided here; the provisioner enforces it)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

from app.controlplane.base import MODULE_IDS


@dataclass(frozen=True)
class HealthProbe:
    module_id: str
    kind: str                 # "http" | "none"
    port: int = 0
    path: str = ""
    host: str = ""            # "" -> the module id (compose service-name convention)
    # comm workers' liveness listener is deliberately fail-open (a refused
    # connection means "no listener", not "worker dead") — mirror that policy.
    fail_open_on_connection_refused: bool = False


MODULE_HEALTH_PROBES: Dict[str, HealthProbe] = {
    "onebrain-api":          HealthProbe("onebrain-api", "http", 8000, "/health"),
    "onebrain-admin-ui":     HealthProbe("onebrain-admin-ui", "http", 3000, "/"),
    "onebrain-workers":      HealthProbe("onebrain-workers", "none"),   # no listener: no claim is made
    "assistant-service":     HealthProbe("assistant-service", "http", 8000, "/health/ready"),
    "communication-api":     HealthProbe("communication-api", "http", 4000, "/health"),
    "communication-widget":  HealthProbe("communication-widget", "http", 5174, "/health"),
    "communication-voice":   HealthProbe("communication-voice", "http", 4100, "/health"),
    "communication-workers": HealthProbe("communication-workers", "http", 4200, "/health",
                                         fail_open_on_connection_refused=True),
}

# Env vars the provisioner MUST deliver per module (names verified in each repo;
# comm reads ONEBRAIN_API_BASE_URL / ONEBRAIN_SERVICE_KEY / ONEBRAIN_SPACE_ID /
# ONEBRAIN_ACCOUNT_ID — the SERVICE_KEY+SPACE_ID pair is what the retired workflow
# never set, letting comm silently run in local-brain fallback; PA reads
# ONEBRAIN_API_BASE_URL + ONEBRAIN_SERVICE_KEY). Aggregate counts of names only.
MODULE_ENV_REQUIREMENTS: Dict[str, Tuple[str, ...]] = {
    # Fleet credentials are host-agent inputs, never application-container
    # requirements. The deployment id remains customer-visible metadata.
    "onebrain-api":          ("ONEBRAIN_VECTOR_STORE", "ONEBRAIN_DATABASE_URL", "ONEBRAIN_DATA_DIR",
                              "ONEBRAIN_DEPLOYMENT_ID", "ONEBRAIN_POSTGRES_APP_ROLE",
                              "ONEBRAIN_POSTGRES_WORKER_ROLE"),
    "onebrain-workers":      ("ONEBRAIN_VECTOR_STORE", "ONEBRAIN_DATABASE_URL", "ONEBRAIN_DATA_DIR",
                              "ONEBRAIN_WORKER_DATABASE_URL", "ONEBRAIN_POSTGRES_APP_ROLE",
                              "ONEBRAIN_POSTGRES_WORKER_ROLE"),
    "onebrain-admin-ui":     (),
    "assistant-service":     ("ONEBRAIN_API_BASE_URL", "ONEBRAIN_SERVICE_KEY", "DATABASE_URL", "REDIS_URL"),
    "communication-api":     ("ONEBRAIN_API_BASE_URL", "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_SPACE_ID",
                              "ONEBRAIN_ACCOUNT_ID", "DATABASE_URL", "REDIS_URL"),
    "communication-workers": ("ONEBRAIN_API_BASE_URL", "ONEBRAIN_SERVICE_KEY", "ONEBRAIN_SPACE_ID",
                              "DATABASE_URL", "REDIS_URL"),
    "communication-voice":   ("DATABASE_URL", "REDIS_URL"),
    "communication-widget":  (),
}


def validate_module_env(module_id: str, env: Mapping[str, str]) -> list[str]:
    """Names that are missing or empty for module_id (empty list = satisfied).
    Raises KeyError on an unknown module id (caller bug, fail loud)."""
    required = MODULE_ENV_REQUIREMENTS[module_id]
    return [name for name in required if not (env.get(name) or "").strip()]


def parse_local_modules(csv_value: str) -> list[str]:
    """ONEBRAIN_LOCAL_MODULES csv -> known module ids, order preserved, unknowns
    dropped (never raises — reporter input)."""
    out: list[str] = []
    for part in (csv_value or "").split(","):
        module_id = part.strip()
        if module_id in MODULE_IDS:
            out.append(module_id)
    return out
