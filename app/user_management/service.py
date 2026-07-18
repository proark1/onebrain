"""Customer-side typed user lifecycle operations."""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from app.auth.passwords import hash_password
from app.auth.roles import LOCATIONS, ROLES
from app.platform.base import AuditEvent, PRIVATE_SPACE_KINDS
from app.user_management.base import SAFE_ERROR_CODES, UserManagementReceipt
from app.user_management.crypto import UserManagementCommand, encrypt_result, result_aad
from app.users.base import User


HUMAN_ROLE_IDS = tuple(role_id for role_id in ROLES if role_id != "public")

PAYLOAD_FIELDS = {
    "directory.snapshot": frozenset({"include_deleted"}),
    "user.create": frozenset({"display_name", "email", "role_id", "location"}),
    "user.password.reset": frozenset({"user_id"}),
    "user.disable": frozenset({"user_id"}),
    "user.enable": frozenset({"user_id"}),
    "user.delete": frozenset({"user_id"}),
}


class UserManagementError(ValueError):
    def __init__(self, code: str, *, details: dict | None = None):
        super().__init__(code)
        self.code = code if code in SAFE_ERROR_CODES else "internal_failure"
        self.details = details or {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def generate_one_time_password() -> str:
    # 24 URL-safe characters provide well above the existing 12-character
    # minimum without punctuation that is commonly mangled by support channels.
    return secrets.token_urlsafe(18)


def _normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized or len(normalized) > 320 or "@" not in normalized or normalized.startswith("@"):
        raise UserManagementError("duplicate_email")
    return normalized


class CustomerUserManagementService:
    def __init__(self, *, users, sessions, platform, receipts, tenant_id: str):
        self.users = users
        self.sessions = sessions
        self.platform = platform
        self.receipts = receipts
        self.tenant_id = tenant_id

    def execute(self, command: UserManagementCommand) -> dict[str, str]:
        self.receipts.purge(now_iso=now_iso())
        existing = self.receipts.get(command.command_id)
        if existing:
            return {
                "sender_public_key": existing.sender_public_key,
                "nonce": existing.nonce,
                "ciphertext": existing.ciphertext,
            }
        expected = PAYLOAD_FIELDS.get(command.action)
        if expected is None or frozenset(command.payload) != expected:
            raise UserManagementError("internal_failure")

        try:
            result = self._dispatch(command)
        except UserManagementError as exc:
            self._audit(
                command,
                str(command.payload.get("user_id", "directory")),
                "denied",
                error_code=exc.code,
            )
            raise
        aad = result_aad(
            command_id=command.command_id,
            deployment_id=command.deployment_id,
            action=command.action,
        )
        encrypted = encrypt_result({"ok": True, "data": result}, command.result_public_key, aad=aad)
        receipt = UserManagementReceipt(
            command_id=command.command_id,
            action=command.action,
            sender_public_key=encrypted["sender_public_key"],
            nonce=encrypted["nonce"],
            ciphertext=encrypted["ciphertext"],
            created_at=now_iso(),
            expires_at=_expiry(),
        )
        saved = self.receipts.put(receipt)
        return {
            "sender_public_key": saved.sender_public_key,
            "nonce": saved.nonce,
            "ciphertext": saved.ciphertext,
        }

    def _dispatch(self, command: UserManagementCommand) -> dict:
        action = command.action
        payload = command.payload
        if action == "directory.snapshot":
            result = self._directory(bool(payload["include_deleted"]))
            self._audit(command, "directory", "allowed")
            return result
        if action == "user.create":
            result = self._create(payload)
        else:
            user = self._target(payload["user_id"])
            if action == "user.password.reset":
                result = self._reset(user)
            elif action == "user.disable":
                result = self._disable(user)
            elif action == "user.enable":
                result = self._enable(user)
            elif action == "user.delete":
                result = self._delete(user)
            else:
                raise UserManagementError("internal_failure")
        self._audit(command, result["user"]["id"], "allowed")
        return result

    def _target(self, user_id: str) -> User:
        user = self.users.get(str(user_id))
        if not user or user.tenant_id != self.tenant_id or user.status == "deleted":
            raise UserManagementError("user_not_found")
        return user

    def _roles(self) -> list[dict]:
        return [
            {"id": role_id, "label": ROLES[role_id].label, "scope": ROLES[role_id].scope}
            for role_id in HUMAN_ROLE_IDS
        ]

    def _blockers(self, user_id: str) -> list[dict]:
        blockers: list[dict] = []
        for account in self.platform.list_accounts():
            if account.owner_user_id == user_id:
                blockers.append({"type": "account_owner", "resource_id": account.id})
            memberships = self.platform.list_memberships(account.id)
            private_spaces = {space.id for space in self.platform.list_spaces(account.id) if space.kind in PRIVATE_SPACE_KINDS}
            for membership in memberships:
                if membership.user_id == user_id and membership.space_id in private_spaces and membership.status == "active":
                    blockers.append({"type": "private_space_owner", "resource_id": membership.space_id})
        return blockers

    def _user_out(self, user: User) -> dict:
        return {
            "id": user.id,
            "display_name": user.display_name,
            "email": user.email,
            "role_id": user.role_id,
            "location": user.location,
            "status": user.status,
            "must_change_password": user.must_change_password,
            "created_at": user.created_at,
            "deletion_blocked": bool(self._blockers(user.id)) if user.status != "deleted" else False,
        }

    def _directory(self, include_deleted: bool) -> dict:
        users = self.users.list_by_tenant(self.tenant_id)
        if not include_deleted:
            users = [user for user in users if user.status != "deleted"]
        return {
            "users": [self._user_out(user) for user in sorted(users, key=lambda item: item.email)],
            "roles": self._roles(),
            "locations": list(LOCATIONS),
        }

    def _validate_role_location(self, role_id: str, location: str) -> tuple[str, str]:
        if role_id not in HUMAN_ROLE_IDS:
            raise UserManagementError("invalid_role")
        role = ROLES[role_id]
        location = location.strip().lower()
        if role.scope == "location":
            if location not in LOCATIONS:
                raise UserManagementError("invalid_location")
        else:
            location = "all"
        return role_id, location

    def _create(self, payload: dict) -> dict:
        email = _normalize_email(str(payload["email"]))
        if self.users.get_by_email(email):
            raise UserManagementError("duplicate_email")
        display_name = str(payload["display_name"]).strip()
        if not display_name or len(display_name) > 200:
            raise UserManagementError("internal_failure")
        role_id, location = self._validate_role_location(str(payload["role_id"]), str(payload["location"]))
        password = generate_one_time_password()
        try:
            user = self.users.create(User(
                id=uuid.uuid4().hex,
                email=email,
                display_name=display_name,
                password_hash=hash_password(password),
                tenant_id=self.tenant_id,
                role_id=role_id,
                location=location,
                status="active",
                must_change_password=True,
            ))
        except ValueError as exc:
            if str(exc) == "duplicate_email":
                raise UserManagementError("duplicate_email") from exc
            raise
        return {"user": self._user_out(user), "one_time_password": password}

    def _reset(self, user: User) -> dict:
        password = generate_one_time_password()
        updated = self.users.update_password(user.id, hash_password(password), must_change_password=True)
        revoked = self.sessions.revoke_all_for_user(user.id)
        return {"user": self._user_out(updated), "one_time_password": password, "sessions_revoked": revoked}

    def _disable(self, user: User) -> dict:
        if user.status != "active":
            raise UserManagementError("invalid_state_transition")
        if user.role_id == "admin":
            active_admins = [
                row for row in self.users.list_by_tenant(self.tenant_id)
                if row.status == "active" and row.role_id == "admin"
            ]
            if len(active_admins) <= 1:
                raise UserManagementError("last_active_admin")
        updated = self.users.update_status(user.id, "disabled")
        revoked = self.sessions.revoke_all_for_user(user.id)
        return {"user": self._user_out(updated), "sessions_revoked": revoked}

    def _enable(self, user: User) -> dict:
        if user.status != "disabled":
            raise UserManagementError("invalid_state_transition")
        updated = self.users.update_status(user.id, "active")
        return {"user": self._user_out(updated)}

    def _delete(self, user: User) -> dict:
        if user.status != "disabled":
            raise UserManagementError("invalid_state_transition")
        blockers = self._blockers(user.id)
        if blockers:
            raise UserManagementError("ownership_reassignment_required", details={"blockers": blockers})
        self.sessions.revoke_all_for_user(user.id)
        deleted = self.users.anonymize(
            user.id,
            email=f"deleted+{user.id}@invalid.onebrain",
            password_hash=hash_password(secrets.token_urlsafe(48)),
        )
        return {"user": self._user_out(deleted)}

    def _audit(
        self,
        command: UserManagementCommand,
        target_user_id: str,
        decision: str,
        *,
        error_code: str = "",
    ) -> None:
        accounts = self.platform.list_accounts()
        if not accounts:
            return
        account = accounts[0]
        self.platform.record_audit(AuditEvent(
            id=uuid.uuid4().hex,
            account_id=account.id,
            actor_id="mission-control",
            actor_type="service",
            action=f"user_management.{command.action}",
            target_type="user",
            target_id=target_user_id,
            decision=decision,
            meta={
                "command_id": command.command_id,
                "deployment_id": command.deployment_id,
                "error_code": error_code,
            },
        ))


class PostgresCustomerUserManagementService:
    """Production executor with mutation, revocation, audit, and receipt in one transaction."""

    _USER_COLS = (
        "id, email, display_name, password_hash, tenant_id, role_id, location, "
        "status, created_at, must_change_password"
    )

    def __init__(self, *, dsn: str, tenant_id: str):
        import psycopg

        self._psycopg = psycopg
        self.dsn = dsn
        self.tenant_id = tenant_id

    @staticmethod
    def _user(row) -> User:
        return User(
            id=row[0], email=row[1], display_name=row[2], password_hash=row[3],
            tenant_id=row[4], role_id=row[5], location=row[6], status=row[7],
            created_at=row[8].isoformat() if row[8] else "", must_change_password=bool(row[9]),
        )

    def execute(self, command: UserManagementCommand) -> dict[str, str]:
        expected = PAYLOAD_FIELDS.get(command.action)
        if expected is None or frozenset(command.payload) != expected:
            raise UserManagementError("internal_failure")
        with self._psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            # Governance tables are protected by forced RLS. The bootstrap
            # account identifier is also this command executor's tenant scope.
            cur.execute(
                "SELECT set_config('app.tenant_id', %s, true), "
                "set_config('app.account_id', %s, true), "
                "set_config('app.space_id', '', true)",
                (self.tenant_id, self.tenant_id),
            )
            cur.execute("DELETE FROM user_management_receipts WHERE expires_at <= now()")
            cur.execute(
                "SELECT sender_public_key, nonce, ciphertext FROM user_management_receipts WHERE command_id = %s",
                (command.command_id,),
            )
            existing = cur.fetchone()
            if existing:
                return {"sender_public_key": existing[0], "nonce": existing[1], "ciphertext": existing[2]}
            try:
                result, target_id = self._dispatch(cur, command.action, command.payload)
                encrypted = encrypt_result(
                    {"ok": True, "data": result},
                    command.result_public_key,
                    aad=result_aad(
                        command_id=command.command_id,
                        deployment_id=command.deployment_id,
                        action=command.action,
                    ),
                )
                cur.execute(
                    "INSERT INTO user_management_receipts "
                    "(command_id, action, sender_public_key, nonce, ciphertext, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, now(), now() + interval '24 hours')",
                    (command.command_id, command.action, encrypted["sender_public_key"],
                     encrypted["nonce"], encrypted["ciphertext"]),
                )
                self._audit(cur, command, target_id, "allowed")
                conn.commit()
                return encrypted
            except UserManagementError as exc:
                conn.rollback()
                self._audit_denial(
                    command,
                    str(command.payload.get("user_id", "directory")),
                    exc.code,
                )
                raise

    def _dispatch(self, cur, action: str, payload: dict) -> tuple[dict, str]:
        if action == "directory.snapshot":
            return self._directory(cur, bool(payload["include_deleted"])), "directory"
        if action == "user.create":
            result = self._create(cur, payload)
            return result, result["user"]["id"]
        cur.execute(
            f"SELECT {self._USER_COLS} FROM users WHERE id = %s AND tenant_id = %s FOR UPDATE",
            (str(payload["user_id"]), self.tenant_id),
        )
        row = cur.fetchone()
        if not row or row[7] == "deleted":
            raise UserManagementError("user_not_found")
        user = self._user(row)
        if action == "user.password.reset":
            result = self._reset(cur, user)
        elif action == "user.disable":
            result = self._disable(cur, user)
        elif action == "user.enable":
            result = self._enable(cur, user)
        elif action == "user.delete":
            result = self._delete(cur, user)
        else:
            raise UserManagementError("internal_failure")
        return result, user.id

    def _roles(self) -> list[dict]:
        return [{"id": key, "label": ROLES[key].label, "scope": ROLES[key].scope} for key in HUMAN_ROLE_IDS]

    def _blockers(self, cur, user_id: str) -> list[dict]:
        cur.execute("SELECT id FROM platform_accounts WHERE owner_user_id = %s", (user_id,))
        blockers = [{"type": "account_owner", "resource_id": row[0]} for row in cur.fetchall()]
        cur.execute(
            "SELECT memberships.space_id FROM platform_memberships memberships "
            "JOIN platform_spaces spaces ON spaces.id = memberships.space_id "
            "WHERE memberships.user_id = %s AND memberships.status = 'active' "
            "AND spaces.kind IN ('personal', 'family')",
            (user_id,),
        )
        blockers.extend({"type": "private_space_owner", "resource_id": row[0]} for row in cur.fetchall())
        return blockers

    def _user_out(self, cur, user: User) -> dict:
        return {
            "id": user.id, "display_name": user.display_name, "email": user.email,
            "role_id": user.role_id, "location": user.location, "status": user.status,
            "must_change_password": user.must_change_password, "created_at": user.created_at,
            "deletion_blocked": bool(self._blockers(cur, user.id)) if user.status != "deleted" else False,
        }

    def _directory(self, cur, include_deleted: bool) -> dict:
        clause = "" if include_deleted else "AND status <> 'deleted'"
        cur.execute(
            f"SELECT {self._USER_COLS} FROM users WHERE tenant_id = %s {clause} ORDER BY email",
            (self.tenant_id,),
        )
        return {
            "users": [self._user_out(cur, self._user(row)) for row in cur.fetchall()],
            "roles": self._roles(),
            "locations": list(LOCATIONS),
        }

    def _validate_role_location(self, role_id: str, location: str) -> tuple[str, str]:
        if role_id not in HUMAN_ROLE_IDS:
            raise UserManagementError("invalid_role")
        role = ROLES[role_id]
        location = location.strip().lower()
        if role.scope == "location" and location not in LOCATIONS:
            raise UserManagementError("invalid_location")
        return role_id, location if role.scope == "location" else "all"

    def _create(self, cur, payload: dict) -> dict:
        email = _normalize_email(str(payload["email"]))
        display_name = str(payload["display_name"]).strip()
        if not display_name or len(display_name) > 200:
            raise UserManagementError("internal_failure")
        role_id, location = self._validate_role_location(str(payload["role_id"]), str(payload["location"]))
        password = generate_one_time_password()
        try:
            cur.execute(
                f"INSERT INTO users (id, email, display_name, password_hash, tenant_id, role_id, location, "
                "status, must_change_password) VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', true) "
                f"RETURNING {self._USER_COLS}",
                (uuid.uuid4().hex, email, display_name, hash_password(password), self.tenant_id, role_id, location),
            )
        except self._psycopg.errors.UniqueViolation as exc:
            raise UserManagementError("duplicate_email") from exc
        user = self._user(cur.fetchone())
        return {"user": self._user_out(cur, user), "one_time_password": password}

    def _reset(self, cur, user: User) -> dict:
        password = generate_one_time_password()
        cur.execute(
            f"UPDATE users SET password_hash = %s, must_change_password = true WHERE id = %s "
            f"RETURNING {self._USER_COLS}",
            (hash_password(password), user.id),
        )
        updated = self._user(cur.fetchone())
        cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user.id,))
        return {"user": self._user_out(cur, updated), "one_time_password": password, "sessions_revoked": cur.rowcount}

    def _disable(self, cur, user: User) -> dict:
        if user.status != "active":
            raise UserManagementError("invalid_state_transition")
        if user.role_id == "admin":
            cur.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND role_id = 'admin' AND status = 'active' FOR UPDATE",
                (self.tenant_id,),
            )
            if len(cur.fetchall()) <= 1:
                raise UserManagementError("last_active_admin")
        cur.execute(f"UPDATE users SET status = 'disabled' WHERE id = %s RETURNING {self._USER_COLS}", (user.id,))
        updated = self._user(cur.fetchone())
        cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user.id,))
        return {"user": self._user_out(cur, updated), "sessions_revoked": cur.rowcount}

    def _enable(self, cur, user: User) -> dict:
        if user.status != "disabled":
            raise UserManagementError("invalid_state_transition")
        cur.execute(f"UPDATE users SET status = 'active' WHERE id = %s RETURNING {self._USER_COLS}", (user.id,))
        return {"user": self._user_out(cur, self._user(cur.fetchone()))}

    def _delete(self, cur, user: User) -> dict:
        if user.status != "disabled":
            raise UserManagementError("invalid_state_transition")
        blockers = self._blockers(cur, user.id)
        if blockers:
            raise UserManagementError("ownership_reassignment_required", details={"blockers": blockers})
        cur.execute("UPDATE auth_sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user.id,))
        cur.execute(
            f"UPDATE users SET email = %s, display_name = 'Deleted user', password_hash = %s, "
            "role_id = 'public', location = '', status = 'deleted', must_change_password = false "
            f"WHERE id = %s RETURNING {self._USER_COLS}",
            (f"deleted+{user.id}@invalid.onebrain", hash_password(secrets.token_urlsafe(48)), user.id),
        )
        return {"user": self._user_out(cur, self._user(cur.fetchone()))}

    def _audit(
        self,
        cur,
        command: UserManagementCommand,
        target_id: str,
        decision: str,
        *,
        error_code: str = "",
    ) -> None:
        cur.execute("SELECT id FROM platform_accounts WHERE id = %s", (self.tenant_id,))
        row = cur.fetchone()
        if not row:
            raise UserManagementError("internal_failure")
        account_id = row[0]
        cur.execute(
            "INSERT INTO platform_audit_events "
            "(id, account_id, actor_id, actor_type, action, target_type, target_id, decision, meta) "
            "VALUES (%s, %s, 'mission-control', 'service', %s, 'user', %s, %s, %s)",
            (
                uuid.uuid4().hex,
                account_id,
                f"user_management.{command.action}",
                target_id,
                decision,
                json.dumps(
                    {
                        "command_id": command.command_id,
                        "deployment_id": command.deployment_id,
                        "error_code": error_code,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    def _audit_denial(self, command: UserManagementCommand, target_id: str, code: str) -> None:
        try:
            with self._psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.tenant_id', %s, true), "
                    "set_config('app.account_id', %s, true), "
                    "set_config('app.space_id', '', true)",
                    (self.tenant_id, self.tenant_id),
                )
                self._audit(cur, command, target_id, "denied", error_code=code)
                conn.commit()
        except Exception:
            pass
