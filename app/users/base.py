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
    # H-10: a freshly-minted owner (provisioning OTP) must rotate the credential
    # before any privileged call. Default false keeps every existing row/seed inert.
    must_change_password: bool = False


class UserStore(Protocol):
    def get(self, user_id: str) -> Optional[User]: ...

    def get_by_email(self, email: str) -> Optional[User]: ...

    def create(self, user: User) -> User: ...

    def update_password(self, user_id: str, password_hash: str, *, must_change_password: bool) -> User: ...

    def update_scope(self, user_id: str, *, tenant_id: str, role_id: str, location: str) -> User: ...

    def update_status(self, user_id: str, status: str) -> User: ...

    def anonymize(self, user_id: str, *, email: str, password_hash: str) -> User: ...

    def delete_by_email(self, email: str) -> bool: ...

    def count(self) -> int: ...

    def list_by_tenant(self, tenant_id: str) -> List[User]: ...
