"""Opaque, encrypted credential storage for connector tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from app.ai_employees.contracts import assert_no_raw_secrets


def _fernet_key(value: str) -> bytes:
    value = (value or "").strip()
    if not value:
        raise ValueError("Connector secret encryption key is required.")
    try:
        decoded = base64.urlsafe_b64decode(value.encode("utf-8"))
        if len(decoded) == 32:
            return value.encode("utf-8")
    except Exception:
        pass
    try:
        decoded = bytes.fromhex(value)
        if len(decoded) == 32:
            return base64.urlsafe_b64encode(decoded)
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode("utf-8")).digest())


class MemoryConnectorSecretStore:
    def __init__(self):
        self._values: dict[str, dict] = {}

    def put(self, *, provider: str, account_id: str, value: dict) -> str:
        ref = f"secret://ai-employees/{provider}/{account_id}/{uuid4().hex}"
        self._values[ref] = dict(value)
        return ref

    def get(self, reference: str) -> dict:
        value = self._values.get(reference)
        if value is None:
            raise KeyError("Connector credential is unavailable.")
        return dict(value)

    def update(self, reference: str, value: dict) -> None:
        if reference not in self._values:
            raise KeyError("Connector credential is unavailable.")
        self._values[reference] = dict(value)

    def delete(self, reference: str) -> None:
        self._values.pop(reference, None)

    def delete_scope(
        self,
        *,
        account_id: str,
        space_id: str = "",
        references: tuple[str, ...] = (),
    ) -> int:
        """Erase credentials and pending OAuth state for one governed scope."""
        prefix = "secret://ai-employees/"
        account_marker = f"/{account_id}/"
        explicit = set(references)
        deleted = 0
        for reference, value in list(self._values.items()):
            in_account = reference.startswith(prefix) and account_marker in reference
            matches_scope = not space_id or value.get("space_id") == space_id
            if reference in explicit or (in_account and matches_scope):
                self._values.pop(reference, None)
                deleted += 1
        return deleted


class EncryptedFileConnectorSecretStore:
    """Encrypted-at-rest local secret backend for single-deployment installations."""

    def __init__(self, *, path: str, encryption_key: str):
        self._path = Path(path)
        self._fernet = Fernet(_fernet_key(encryption_key))
        self._lock = threading.RLock()

    def put(self, *, provider: str, account_id: str, value: dict) -> str:
        ref = f"secret://ai-employees/{provider}/{account_id}/{uuid4().hex}"
        with self._lock:
            rows = self._load()
            rows[ref] = self._seal(value)
            self._save(rows)
        return ref

    def get(self, reference: str) -> dict:
        with self._lock:
            ciphertext = self._load().get(reference)
        if not ciphertext:
            raise KeyError("Connector credential is unavailable.")
        try:
            raw = self._fernet.decrypt(ciphertext.encode("utf-8"))
            value = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Connector credential could not be decrypted.") from exc
        if not isinstance(value, dict):
            raise ValueError("Connector credential is malformed.")
        return value

    def update(self, reference: str, value: dict) -> None:
        with self._lock:
            rows = self._load()
            if reference not in rows:
                raise KeyError("Connector credential is unavailable.")
            rows[reference] = self._seal(value)
            self._save(rows)

    def delete(self, reference: str) -> None:
        with self._lock:
            rows = self._load()
            if rows.pop(reference, None) is not None:
                self._save(rows)

    def delete_scope(
        self,
        *,
        account_id: str,
        space_id: str = "",
        references: tuple[str, ...] = (),
    ) -> int:
        """Erase encrypted credentials and pending OAuth state for one scope."""
        account_marker = f"/{account_id}/"
        explicit = set(references)
        deleted = 0
        with self._lock:
            rows = self._load()
            for reference, ciphertext in list(rows.items()):
                in_account = (
                    reference.startswith("secret://ai-employees/")
                    and account_marker in reference
                )
                if not in_account and reference not in explicit:
                    continue
                matches_scope = not space_id
                if space_id and reference not in explicit:
                    value = self._unseal(ciphertext)
                    matches_scope = value.get("space_id") == space_id
                if reference in explicit or matches_scope:
                    rows.pop(reference, None)
                    deleted += 1
            if deleted:
                self._save(rows)
        return deleted

    def _seal(self, value: dict) -> str:
        if not isinstance(value, dict):
            raise ValueError("Connector credential must be an object.")
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw).decode("utf-8")

    def _unseal(self, ciphertext: str) -> dict:
        raw = self._fernet.decrypt(ciphertext.encode("utf-8"))
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Connector credential is malformed.")
        return value

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Connector secret store could not be read.") from exc
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise ValueError("Connector secret store is malformed.")
        return value

    def _save(self, value: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(f"{self._path.suffix}.{uuid4().hex}.tmp")
        temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        os.replace(temp, self._path)


def assert_opaque_credential_reference(reference: str) -> None:
    if not (reference or "").startswith("secret://ai-employees/"):
        raise ValueError("Connector credential must use an opaque AI Employees secret reference.")
    assert_no_raw_secrets({"credential_ref": reference}, "connector_binding")
