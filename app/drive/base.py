"""Drive domain records, validation, and replaceable store contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from app.store.base import Chunk


DRIVE_INDEX_STATUSES = frozenset({
    "not_indexed", "queued", "extracting", "awaiting_review", "indexing",
    "indexed", "blocked", "unsupported", "failed", "stale", "deleting",
})
DRIVE_APPROVAL_STATUSES = frozenset({"not_required", "pending", "approved", "rejected"})
DRIVE_UPLOAD_STATUSES = frozenset({
    "created", "uploading", "uploaded", "completing", "completed", "failed", "expired",
})
DRIVE_ROOT_KINDS = frozenset({"personal", "space"})
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 250
MAX_FOLDER_DEPTH = 32
MAX_NAME_LENGTH = 255
MAX_PAGE_OFFSET = 10_000_000
PAGE_CURSOR_PREFIX = "offset_"
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class DriveConflictError(ValueError):
    """A name, idempotency key, or generation conflicts with stored state."""


class DriveGenerationConflict(DriveConflictError):
    """The caller attempted to mutate a stale file or folder generation."""


class DriveLimitError(ValueError):
    """A bounded Drive resource limit would be exceeded."""


class DriveNotFoundError(KeyError):
    """A Drive record does not exist in the authorized scope."""


@dataclass(frozen=True)
class DriveRoot:
    id: str
    account_id: str
    space_id: str
    kind: str
    name: str
    owner_user_id: str = ""


@dataclass(frozen=True)
class DriveFolder:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    parent_id: str
    name: str
    default_classification: str = "internal"
    default_location: str = "global"
    default_category: str = "general"
    default_indexed: bool = True
    generation: int = 1
    trashed_at: str = ""
    original_parent_id: str = ""
    trash_operation_id: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DriveFile:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    folder_id: str
    name: str
    classification: str = "internal"
    location: str = "global"
    category: str = "general"
    space_kind: str = ""
    owner_user_id: str = ""
    desired_indexed: bool = True
    approval_status: str = "not_required"
    index_status: str = "not_indexed"
    current_revision_id: str = ""
    active_doc_id: str = ""
    generation: int = 1
    uploaded_by: str = ""
    approved_by: str = ""
    trashed_at: str = ""
    original_folder_id: str = ""
    trash_operation_id: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DriveRevision:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    file_id: str
    upload_session_id: str
    storage_key: str
    sha256: str
    size_bytes: int
    media_type: str
    original_name: str
    created_by: str
    created_at: str = ""


@dataclass(frozen=True)
class DriveUploadSession:
    id: str
    tenant_id: str
    account_id: str
    space_id: str
    folder_id: str
    name: str
    size_bytes: int
    desired_indexed: bool
    classification: str
    location: str
    category: str
    created_by: str
    idempotency_key: str
    staging_key: str
    status: str = "created"
    bytes_received: int = 0
    sha256: str = ""
    media_type: str = "application/octet-stream"
    file_id: str = ""
    revision_id: str = ""
    error: str = ""
    expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DriveEntryPage:
    folders: tuple[DriveFolder, ...] = field(default_factory=tuple)
    files: tuple[DriveFile, ...] = field(default_factory=tuple)
    next_cursor: str = ""


@dataclass(frozen=True)
class DriveProjectionResult:
    file: DriveFile
    chunks: int


@dataclass(frozen=True)
class DriveTreeMutationResult:
    root: DriveFolder
    files: tuple[DriveFile, ...] = field(default_factory=tuple)


class DriveStore(Protocol):
    def create_folder(self, folder: DriveFolder) -> DriveFolder: ...
    def get_folder(self, folder_id: str, *, account_id: str, space_id: str) -> Optional[DriveFolder]: ...
    def list_entries(
        self, *, account_id: str, space_id: str, folder_id: str = "", query: str = "",
        trashed: bool = False, cursor: str = "", limit: int = DEFAULT_PAGE_SIZE,
    ) -> DriveEntryPage: ...
    def breadcrumbs(self, folder_id: str, *, account_id: str, space_id: str) -> list[DriveFolder]: ...
    def update_folder(self, folder: DriveFolder, *, expected_generation: int) -> DriveFolder: ...
    def trash_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        timestamp: str, folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
    ) -> DriveTreeMutationResult: ...
    def restore_folder_tree(
        self, *, root: DriveFolder, expected_generation: int, operation_id: str,
        folder_generations: Mapping[str, int], file_generations: Mapping[str, int],
        indexing_enabled: bool = True,
    ) -> DriveTreeMutationResult: ...
    def create_file(self, file: DriveFile) -> DriveFile: ...
    def get_file(self, file_id: str, *, account_id: str, space_id: str) -> Optional[DriveFile]: ...
    def update_file(self, file: DriveFile, *, expected_generation: int) -> DriveFile: ...
    def create_revision(self, revision: DriveRevision) -> DriveRevision: ...
    def get_revision(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveRevision]: ...
    def list_revisions(self, file_id: str, *, account_id: str, space_id: str) -> list[DriveRevision]: ...
    def create_upload(self, upload: DriveUploadSession) -> DriveUploadSession: ...
    def get_upload(self, upload_id: str, *, tenant_id: str = "") -> Optional[DriveUploadSession]: ...
    def get_upload_by_idempotency(
        self, *, account_id: str, space_id: str, created_by: str, idempotency_key: str,
    ) -> Optional[DriveUploadSession]: ...
    def update_upload(self, upload: DriveUploadSession) -> DriveUploadSession: ...
    def list_expired_uploads(
        self, *, tenant_id: str, account_id: str, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]: ...
    def publish_projection(
        self, *, file_id: str, revision_id: str, generation: int,
        account_id: str, space_id: str, chunks: Sequence[Chunk],
    ) -> DriveProjectionResult: ...
    def unpublish(self, *, file_id: str, account_id: str, space_id: str, generation: int) -> DriveFile: ...
    def list_pending_review(self, *, account_id: str, space_id: str = "") -> list[DriveFile]: ...
    def delete_file(self, *, file_id: str, account_id: str, space_id: str) -> dict[str, int]: ...
    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict: ...
    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_name(value: str, *, field_name: str = "name") -> str:
    name = (value or "").strip().replace("\x00", "")
    if not name or len(name) > MAX_NAME_LENGTH or name in {".", ".."}:
        raise ValueError(f"{field_name} must contain 1 to {MAX_NAME_LENGTH} safe characters.")
    if "/" in name or "\\" in name:
        raise ValueError(f"{field_name} cannot contain path separators.")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"{field_name} cannot contain control characters.")
    return name


def validate_opaque_id(value: str, *, field_name: str = "id", allow_empty: bool = False) -> str:
    item = (value or "").strip()
    if allow_empty and not item:
        return ""
    if not OPAQUE_ID_RE.fullmatch(item):
        raise ValueError(f"{field_name} is invalid.")
    return item


def validate_folder(folder: DriveFolder) -> None:
    _validate_scope(folder.tenant_id, folder.account_id, folder.space_id)
    validate_opaque_id(folder.id)
    validate_opaque_id(folder.parent_id, field_name="parent_id", allow_empty=True)
    normalize_name(folder.name)
    if folder.generation < 1:
        raise ValueError("Folder generation must be positive.")


def validate_file(file: DriveFile) -> None:
    _validate_scope(file.tenant_id, file.account_id, file.space_id)
    validate_opaque_id(file.id)
    validate_opaque_id(file.folder_id, field_name="folder_id", allow_empty=True)
    normalize_name(file.name)
    if file.approval_status not in DRIVE_APPROVAL_STATUSES:
        raise ValueError("Unknown Drive approval status.")
    if file.index_status not in DRIVE_INDEX_STATUSES:
        raise ValueError("Unknown Drive index status.")
    if file.generation < 1:
        raise ValueError("File generation must be positive.")


def validate_revision(revision: DriveRevision) -> None:
    _validate_scope(revision.tenant_id, revision.account_id, revision.space_id)
    for field_name in ("id", "file_id", "upload_session_id"):
        validate_opaque_id(getattr(revision, field_name), field_name=field_name)
    normalize_name(revision.original_name, field_name="original_name")
    if not re.fullmatch(r"[0-9a-f]{64}", revision.sha256 or ""):
        raise ValueError("Revision sha256 is invalid.")
    if revision.size_bytes < 0:
        raise ValueError("Revision size must be non-negative.")
    if not revision.storage_key:
        raise ValueError("Revision storage key is required.")


def validate_upload(upload: DriveUploadSession) -> None:
    _validate_scope(upload.tenant_id, upload.account_id, upload.space_id)
    validate_opaque_id(upload.id)
    validate_opaque_id(upload.folder_id, field_name="folder_id", allow_empty=True)
    normalize_name(upload.name)
    if upload.status not in DRIVE_UPLOAD_STATUSES:
        raise ValueError("Unknown Drive upload status.")
    if upload.size_bytes <= 0:
        raise ValueError("Upload size must be positive.")
    if not upload.idempotency_key or len(upload.idempotency_key) > 128:
        raise ValueError("Upload idempotency key is required and must be at most 128 characters.")


def bounded_page_size(limit: int) -> int:
    return min(max(int(limit), 1), MAX_PAGE_SIZE)


def decode_page_cursor(value: str) -> int:
    cursor = (value or "").strip()
    if not cursor:
        return 0
    if not cursor.startswith(PAGE_CURSOR_PREFIX):
        raise ValueError("Drive page cursor is invalid.")
    raw = cursor[len(PAGE_CURSOR_PREFIX):]
    if not raw.isdigit():
        raise ValueError("Drive page cursor is invalid.")
    offset = int(raw)
    if offset < 0 or offset > MAX_PAGE_OFFSET:
        raise ValueError("Drive page cursor is outside the supported range.")
    return offset


def encode_page_cursor(offset: int) -> str:
    offset = int(offset)
    if offset <= 0 or offset > MAX_PAGE_OFFSET:
        raise ValueError("Drive page offset is outside the supported range.")
    return f"{PAGE_CURSOR_PREFIX}{offset}"


def same_revision_identity(left: DriveRevision, right: DriveRevision) -> bool:
    """Compare immutable revision fields while ignoring server timestamps."""

    return (
        left.id, left.tenant_id, left.account_id, left.space_id, left.file_id,
        left.upload_session_id, left.storage_key, left.sha256, left.size_bytes,
        left.media_type, left.original_name, left.created_by,
    ) == (
        right.id, right.tenant_id, right.account_id, right.space_id, right.file_id,
        right.upload_session_id, right.storage_key, right.sha256, right.size_bytes,
        right.media_type, right.original_name, right.created_by,
    )


def same_folder_identity(left: DriveFolder, right: DriveFolder) -> bool:
    """Compare immutable initial folder fields for idempotent creation."""

    return (
        left.id, left.tenant_id, left.account_id, left.space_id, left.parent_id,
        left.name, left.default_classification, left.default_location,
        left.default_category, left.default_indexed, left.created_by,
    ) == (
        right.id, right.tenant_id, right.account_id, right.space_id, right.parent_id,
        right.name, right.default_classification, right.default_location,
        right.default_category, right.default_indexed, right.created_by,
    )


def same_file_identity(left: DriveFile, right: DriveFile) -> bool:
    """Compare the immutable initial file record for idempotent completion."""

    return (
        left.id, left.tenant_id, left.account_id, left.space_id, left.folder_id,
        left.name, left.classification, left.location, left.category, left.space_kind,
        left.owner_user_id, left.desired_indexed, left.current_revision_id, left.uploaded_by,
    ) == (
        right.id, right.tenant_id, right.account_id, right.space_id, right.folder_id,
        right.name, right.classification, right.location, right.category, right.space_kind,
        right.owner_user_id, right.desired_indexed, right.current_revision_id, right.uploaded_by,
    )


def ensure_unique_scope(rows: Iterable[object]) -> tuple[str, str, str]:
    scopes = {(getattr(row, "tenant_id"), getattr(row, "account_id"), getattr(row, "space_id")) for row in rows}
    if len(scopes) != 1:
        raise ValueError("Drive records must use one tenant, account, and space.")
    return next(iter(scopes))


def _validate_scope(tenant_id: str, account_id: str, space_id: str) -> None:
    if not (tenant_id or "").strip() or not (account_id or "").strip() or not (space_id or "").strip():
        raise ValueError("tenant_id, account_id, and space_id are required.")
