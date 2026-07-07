"""User account types and store interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol


@dataclass
class User:
    id: str
    email: str
    display_name: str
    password_hash: str
    tenant_id: str
    role_id: str
    location: str
    status: str = "active"          # active | disabled
    created_at: str = ""


class UserStore(Protocol):
    def get(self, user_id: str) -> Optional[User]: ...

    def get_by_email(self, email: str) -> Optional[User]: ...

    def create(self, user: User) -> User: ...

    def delete_by_email(self, email: str) -> bool: ...

    def count(self) -> int: ...

    def list_by_tenant(self, tenant_id: str) -> List[User]: ...
