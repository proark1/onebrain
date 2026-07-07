"""Role definitions — the NFT Gym access ladder.

Each role maps to a clearance level, a location scope, and a set of categories
(compartments). This is what makes "certain groups get certain data, others
can't" concrete. In production these come from a policy engine (OpenFGA/OPA);
here they're a static table so the boundary is easy to read and test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.security.policy import Classification


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    clearance: Classification
    scope: str                       # none | location | chain
    categories: Optional[frozenset]  # None = all categories


def _cats(*names: str) -> frozenset:
    return frozenset(names)


ROLES: dict[str, Role] = {
    "public": Role("public", "Public / customer", Classification.PUBLIC, "none", _cats("general")),
    "front_desk": Role("front_desk", "Front-desk staff", Classification.INTERNAL, "location", _cats("general", "cs", "ops")),
    "trainer": Role("trainer", "Trainer", Classification.INTERNAL, "location", _cats("general", "ops")),
    "location_manager": Role("location_manager", "Location manager", Classification.CONFIDENTIAL, "location", _cats("general", "cs", "ops", "marketing")),
    "hr": Role("hr", "HR", Classification.RESTRICTED, "chain", _cats("general", "hr")),
    "finance": Role("finance", "Finance", Classification.RESTRICTED, "chain", _cats("general", "finance")),
    "marketing": Role("marketing", "Marketing", Classification.CONFIDENTIAL, "chain", _cats("general", "marketing")),
    "admin": Role("admin", "Admin / DPO", Classification.RESTRICTED, "chain", None),
}

# Fail closed: an unauthenticated / header-less request must land on PUBLIC
# (customer tier), never on an internal role. Do NOT change this to an employee
# role — anonymous callers would silently gain internal access.
DEFAULT_ROLE = "public"
LOCATIONS = ["munich", "berlin", "hamburg"]
