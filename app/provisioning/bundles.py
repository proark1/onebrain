"""Opinionated product bundles for new customer rollouts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from app.assistant.contracts import ASSISTANT_PURPOSES as ASSISTANT_CONTRACT_PURPOSES
from app.assistant.employees import AI_EMPLOYEE_PURPOSES as AI_EMPLOYEE_CONTRACT_PURPOSES


CORE_MODULES = ("onebrain-api", "onebrain-admin-ui", "onebrain-workers")
ASSISTANT_MODULES = ("assistant-service",)
COMMUNICATION_MODULES = (
    "communication-api",
    "communication-widget",
    "communication-voice",
    "communication-workers",
)


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
class ProvisioningBundle:
    id: str
    label: str
    description: str
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
FULL_STACK_SPACES = (
    SpaceTemplate("personal", "personal", "Personal"),
    SpaceTemplate("business", "business", "Business"),
    SpaceTemplate("customer_service", "customer_service", "Customer service"),
    SpaceTemplate("shared", "shared", "Shared"),
    SpaceTemplate("family", "family", "Family"),
)


def _core_app(space_keys: Tuple[str, ...]) -> AppTemplate:
    return AppTemplate("onebrain_core", space_keys, CORE_PURPOSES, "OneBrain Core")


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


BUNDLES: Dict[str, ProvisioningBundle] = {
    "onebrain_only": ProvisioningBundle(
        id="onebrain_only",
        label="OneBrain only",
        description="Core knowledge database, admin UI, workers, GDPR export and delete surfaces.",
        spaces=BUSINESS_SPACES,
        apps=(_core_app(tuple(s.key for s in BUSINESS_SPACES)),),
        modules=CORE_MODULES,
    ),
    "onebrain_assistant": ProvisioningBundle(
        id="onebrain_assistant",
        label="OneBrain + assistant",
        description="Core data layer plus the personal/business assistant module.",
        spaces=ASSISTANT_SPACES,
        apps=(
            _core_app(tuple(s.key for s in ASSISTANT_SPACES)),
            _assistant_app(tuple(s.key for s in ASSISTANT_SPACES)),
        ),
        modules=CORE_MODULES + ASSISTANT_MODULES,
    ),
    "onebrain_kpi_dashboard": ProvisioningBundle(
        id="onebrain_kpi_dashboard",
        label="OneBrain + KPI dashboard",
        description="Core data layer plus governed KPI definitions and snapshot history for business dashboards.",
        spaces=BUSINESS_SPACES,
        apps=(
            _core_app(tuple(s.key for s in BUSINESS_SPACES)),
            KPI_APP,
        ),
        modules=CORE_MODULES,
    ),
    "onebrain_ai_employees": ProvisioningBundle(
        id="onebrain_ai_employees",
        label="OneBrain + AI employees",
        description="Core data layer plus governed AI employees for proactive draft work and approval-gated actions.",
        spaces=BUSINESS_SPACES,
        apps=(
            _core_app(tuple(s.key for s in BUSINESS_SPACES)),
            AI_EMPLOYEES_APP,
        ),
        modules=CORE_MODULES,
    ),
    "onebrain_communication": ProvisioningBundle(
        id="onebrain_communication",
        label="OneBrain + communication",
        description="Core data layer plus customer service channels for chat, messaging and voice.",
        spaces=COMMUNICATION_SPACES,
        apps=(
            _core_app(tuple(s.key for s in COMMUNICATION_SPACES)),
            COMMUNICATION_APP,
        ),
        modules=CORE_MODULES + COMMUNICATION_MODULES,
    ),
    "full_stack": ProvisioningBundle(
        id="full_stack",
        label="Full stack",
        description="OneBrain, assistant, communication, KPI dashboard and AI employee features with separated private, business and service spaces.",
        spaces=FULL_STACK_SPACES,
        apps=(
            _core_app(tuple(s.key for s in FULL_STACK_SPACES)),
            _assistant_app(tuple(s.key for s in FULL_STACK_SPACES)),
            COMMUNICATION_APP,
            KPI_APP,
            AI_EMPLOYEES_APP,
        ),
        modules=CORE_MODULES + ASSISTANT_MODULES + COMMUNICATION_MODULES,
    ),
}


def get_bundle(bundle_id: str) -> ProvisioningBundle:
    bundle = BUNDLES.get((bundle_id or "").strip())
    if not bundle:
        raise ValueError(f"Unknown provisioning bundle: {bundle_id}")
    return bundle
