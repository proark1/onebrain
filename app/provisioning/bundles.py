"""Server-owned Core-plus-modules catalogue for customer provisioning.

The browser selects only optional product modules.  OneBrain Core is resolved on
every request by this module, so spaces, app installations, and deployable
services can never be chosen independently by a client.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from app.assistant.contracts import ASSISTANT_PURPOSES as ASSISTANT_CONTRACT_PURPOSES
from app.assistant.employees import AI_EMPLOYEE_PURPOSES as AI_EMPLOYEE_CONTRACT_PURPOSES


# Deployable container services.  KPI Dashboard and AI Employees are product
# modules, but currently run inside the Core services and therefore add no
# separate container service IDs.
CORE_MODULES = ("onebrain-api", "onebrain-admin-ui", "onebrain-workers")
ASSISTANT_MODULES = ("assistant-service",)
COMMUNICATION_MODULES = (
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
)

CORE_MODULE_ID = "onebrain_core"
OPTIONAL_MODULE_IDS = ("assistant", "kpi_dashboard", "ai_employees", "communication")


@dataclass(frozen=True)
class SpaceTemplate:
    key: str
    kind: str
    name: str


@dataclass(frozen=True)
class AppTemplate:
    app_id: str
    space_keys: Tuple[str, ...]
    purposes: Tuple[str, ...]
    display_name: str


@dataclass(frozen=True)
class ProvisioningModule:
    """One product module exposed in the provisioning catalogue.

    ``modules`` contains deployable container-service IDs, while the module ID
    itself represents the product choice persisted on a deployment/run.
    """

    id: str
    label: str
    description: str
    spaces: Tuple[SpaceTemplate, ...]
    apps: Tuple[AppTemplate, ...]
    modules: Tuple[str, ...]


@dataclass(frozen=True)
class ModuleComposition:
    """The complete server-resolved installation for one customer."""

    selected_module_ids: Tuple[str, ...]
    spaces: Tuple[SpaceTemplate, ...]
    apps: Tuple[AppTemplate, ...]
    modules: Tuple[str, ...]


CORE_PURPOSES = ("knowledge_management", "admin_management", "gdpr_export", "gdpr_delete")
ASSISTANT_PURPOSES = tuple(sorted(ASSISTANT_CONTRACT_PURPOSES))
COMMUNICATION_PURPOSES = ("customer_service_answer", "customer_service_inbox")
KPI_PURPOSES = ("kpi_read", "kpi_configure", "kpi_snapshot_write")
AI_EMPLOYEE_PURPOSES = tuple(sorted(AI_EMPLOYEE_CONTRACT_PURPOSES))


BUSINESS_SPACES = (
    SpaceTemplate("business", "business", "Business"),
    SpaceTemplate("shared", "shared", "Shared"),
)
ASSISTANT_SPACES = (
    SpaceTemplate("personal", "personal", "Personal"),
    SpaceTemplate("business", "business", "Business"),
    SpaceTemplate("shared", "shared", "Shared"),
    SpaceTemplate("family", "family", "Family"),
)
COMMUNICATION_SPACES = (
    SpaceTemplate("business", "business", "Business"),
    SpaceTemplate("customer_service", "customer_service", "Customer service"),
    SpaceTemplate("shared", "shared", "Shared"),
)


def _core_app(space_keys: Tuple[str, ...]) -> AppTemplate:
    return AppTemplate(CORE_MODULE_ID, space_keys, CORE_PURPOSES, "OneBrain Core")


def _assistant_app(space_keys: Tuple[str, ...]) -> AppTemplate:
    return AppTemplate("assistant", space_keys, ASSISTANT_PURPOSES, "AI Assistant")


COMMUNICATION_APP = AppTemplate(
    "communication",
    ("customer_service", "shared"),
    COMMUNICATION_PURPOSES,
    "AI Communication",
)
KPI_APP = AppTemplate(
    "kpi_dashboard",
    ("business", "shared"),
    KPI_PURPOSES,
    "KPI Dashboard",
)
AI_EMPLOYEES_APP = AppTemplate(
    "ai_employees",
    ("business", "shared"),
    AI_EMPLOYEE_PURPOSES,
    "AI Employees",
)


# The Core app's enabled spaces are derived from the composition.  The catalogue
# still advertises its baseline app so clients can explain what Core provides.
CORE_MODULE = ProvisioningModule(
    id=CORE_MODULE_ID,
    label="OneBrain Core",
    description="Required knowledge, administration, worker, and GDPR capabilities.",
    spaces=BUSINESS_SPACES,
    apps=(_core_app(tuple(space.key for space in BUSINESS_SPACES)),),
    modules=CORE_MODULES,
)

OPTIONAL_MODULES = (
    ProvisioningModule(
        id="assistant",
        label="Assistant",
        description="A governed personal and business AI assistant.",
        spaces=ASSISTANT_SPACES,
        apps=(_assistant_app(tuple(space.key for space in ASSISTANT_SPACES)),),
        modules=ASSISTANT_MODULES,
    ),
    ProvisioningModule(
        id="kpi_dashboard",
        label="KPI Dashboard",
        description="Governed KPI definitions, snapshots, and business dashboards.",
        spaces=BUSINESS_SPACES,
        apps=(KPI_APP,),
        modules=(),
    ),
    ProvisioningModule(
        id="ai_employees",
        label="AI Employees",
        description="Approval-gated AI employees for proactive draft work.",
        spaces=BUSINESS_SPACES,
        apps=(AI_EMPLOYEES_APP,),
        modules=(),
    ),
    ProvisioningModule(
        id="communication",
        label="Communication",
        description="Customer-service chat, messaging, voice, and worker channels.",
        spaces=COMMUNICATION_SPACES,
        apps=(COMMUNICATION_APP,),
        modules=COMMUNICATION_MODULES,
    ),
)

_OPTIONAL_BY_ID = {module.id: module for module in OPTIONAL_MODULES}


def resolve_module_composition(module_ids: Iterable[str] | None = None) -> ModuleComposition:
    """Validate optional product choices and return one deterministic install plan.

    Empty selection is valid and provisions Core only.  This intentionally
    rejects duplicate or unknown IDs instead of attempting to make a browser
    payload "helpful"; the selected IDs are audit and retry inputs.
    """

    raw_module_ids = tuple(module_ids or ())
    if any(not isinstance(module_id, str) for module_id in raw_module_ids):
        raise ValueError("Optional module ids must be strings.")
    requested = tuple(module_id.strip() for module_id in raw_module_ids)
    duplicates = sorted({module_id for module_id in requested if requested.count(module_id) > 1})
    if duplicates:
        raise ValueError(f"Duplicate optional module ids: {duplicates}")
    unknown = sorted(set(requested) - set(_OPTIONAL_BY_ID))
    if unknown:
        raise ValueError(f"Unknown optional module ids: {unknown}")

    selected = tuple(module for module in OPTIONAL_MODULES if module.id in requested)
    selected_ids = tuple(module.id for module in selected)

    spaces_by_key: dict[str, SpaceTemplate] = {}
    for module in (CORE_MODULE, *selected):
        for space in module.spaces:
            spaces_by_key.setdefault(space.key, space)
    spaces = tuple(spaces_by_key.values())
    all_space_keys = tuple(space.key for space in spaces)

    apps: list[AppTemplate] = [_core_app(all_space_keys)]
    modules: list[str] = list(CORE_MODULE.modules)
    for module in selected:
        if module.id == "assistant":
            apps.append(_assistant_app(all_space_keys))
        else:
            apps.extend(module.apps)
        modules.extend(module.modules)

    return ModuleComposition(
        selected_module_ids=selected_ids,
        spaces=spaces,
        apps=tuple(apps),
        modules=tuple(dict.fromkeys(modules)),
    )
