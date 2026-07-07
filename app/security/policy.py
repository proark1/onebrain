"""The access-control core.

A document chunk carries three labels: a classification level, a location, and
a category (compartment). A principal carries a clearance level plus the sets of
locations and categories they're entitled to. `AccessFilter` compiles those into
a deterministic rule that is enforced OUTSIDE the language model — both as a
Python predicate (memory store) and as a SQL WHERE clause (pgvector). The model
never sees a chunk the filter rejects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

GLOBAL_LOCATION = "global"      # visible from every location (subject to clearance)
GENERAL_CATEGORY = "general"    # visible to every category (subject to clearance)


class Classification(IntEnum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    RESTRICTED = 3

    @classmethod
    def parse(cls, value) -> "Classification":
        if isinstance(value, cls):
            return value
        if isinstance(value, int):
            return cls(value)
        try:
            return cls[str(value).strip().upper()]
        except KeyError:
            # Fail closed: an unknown label is treated as the most restricted.
            return cls.RESTRICTED


@dataclass(frozen=True)
class AccessFilter:
    tenant_id: str                   # which business this caller belongs to — NEVER wildcard
    clearance: int
    locations: Optional[frozenset]   # None = all locations
    categories: Optional[frozenset]  # None = all categories

    def allows(self, meta: dict) -> bool:
        # Tenant is checked FIRST, unconditionally, with no wildcard and no
        # exception even for admin. A chunk from another tenant — or with no
        # tenant_id at all — is never accessible. This is the hard isolation
        # boundary between the businesses that share this brain.
        if meta.get("tenant_id") != self.tenant_id:
            return False
        if int(meta.get("classification", Classification.RESTRICTED)) > self.clearance:
            return False
        location = meta.get("location", GLOBAL_LOCATION)
        if self.locations is not None and location != GLOBAL_LOCATION and location not in self.locations:
            return False
        category = meta.get("category", GENERAL_CATEGORY)
        if self.categories is not None and category != GENERAL_CATEGORY and category not in self.categories:
            return False
        return True

    def to_sql(self) -> tuple[str, list]:
        """Compile to a parameterised pgvector WHERE clause."""
        # tenant equality is ALWAYS the first clause — never behind an
        # `is not None` guard, never with a wildcard value.
        clauses = ["meta->>'tenant_id' = %s", "(meta->>'classification')::int <= %s"]
        params: list = [self.tenant_id, self.clearance]
        if self.locations is not None:
            clauses.append("(meta->>'location' = 'global' OR meta->>'location' = ANY(%s))")
            params.append(list(self.locations))
        if self.categories is not None:
            clauses.append("(meta->>'category' = 'general' OR meta->>'category' = ANY(%s))")
            params.append(list(self.categories))
        return " AND ".join(clauses), params
