"""Drive domain records, validation, and replaceable store contracts."""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterable, Mapping, Optional, Protocol, Sequence

from app.drive.malware.base import (
    ScanVerdict,
    validate_malware_code,
    validate_scanner_identifier,
)
from app.store.base import Chunk


DRIVE_INDEX_STATUSES = frozenset({
    "not_indexed", "queued", "extracting", "awaiting_review", "indexing",
    "indexed", "blocked", "unsupported", "failed", "stale", "deleting", "awaiting_scan",
})
DRIVE_APPROVAL_STATUSES = frozenset({"not_required", "pending", "approved", "rejected"})
DRIVE_UPLOAD_STATUSES = frozenset({
    "created", "uploading", "uploaded", "completing", "completed", "failed", "expired",
})
DRIVE_MALWARE_POLICY_EPOCH = 1
DEFAULT_DRIVE_QUARANTINE_LIMIT_BYTES = 5 * 1024 * 1024 * 1024
DRIVE_MALWARE_STATUSES = frozenset({
    "pending", "scanning", "clean", "infected", "scan_error", "rescan_required",
})
DRIVE_MALWARE_ORIGINS = frozenset({"upload", "rescan", "legacy_backfill"})
DRIVE_QUARANTINE_RESERVATION_STATES = frozenset({"reserved", "transferred", "released"})
DRIVE_MALWARE_ACTIVATION_STATES = frozenset({"pending", "activating", "active", "failed"})
DRIVE_SCANNER_READINESS_STATES = frozenset({"ready", "degraded", "unknown"})
DRIVE_ROOT_KINDS = frozenset({"personal", "space"})
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 250
MAX_FILE_LIST_DETAIL_BATCH = MAX_PAGE_SIZE
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


class DriveQuarantineCapacityError(DriveLimitError):
    """The deployment-wide fail-closed quarantine reservation is full."""

    code = "drive_quarantine_capacity_exhausted"

    def __init__(self):
        super().__init__(self.code)


class DriveNotFoundError(KeyError):
    """A Drive record does not exist in the authorized scope."""


class DriveQuarantineLockedError(PermissionError):
    """Authorized metadata exists, but revision bytes remain quarantined."""

    code = "drive_revision_quarantined"

    def __init__(self):
        super().__init__(self.code)


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
    quarantine_reserved_bytes: int = 0
    reservation_state: str = "released"
    reservation_expires_at: str = ""
    expires_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DriveFileListDetail:
    """Exact current revision and current-policy malware evidence for one list row."""

    revision: Optional[DriveRevision] = None
    malware_scan: Optional["DriveMalwareScan"] = None


@dataclass(frozen=True)
class DriveEntryPage:
    folders: tuple[DriveFolder, ...] = field(default_factory=tuple)
    files: tuple[DriveFile, ...] = field(default_factory=tuple)
    next_cursor: str = ""
    file_details: Mapping[str, DriveFileListDetail] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "file_details",
            MappingProxyType(dict(self.file_details)),
        )


@dataclass(frozen=True)
class DriveProjectionResult:
    file: DriveFile
    chunks: int


@dataclass(frozen=True)
class DriveTreeMutationResult:
    root: DriveFolder
    files: tuple[DriveFile, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DriveMalwareScan:
    """Append-only attempt identity and its monotonic scan evidence."""

    id: str
    tenant_id: str
    account_id: str
    space_id: str
    file_id: str
    revision_id: str
    revision_sha256: str
    revision_size_bytes: int
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH
    status: str = "pending"
    origin: str = "upload"
    attempt_sequence: int = 1
    consecutive_failures: int = 0
    job_id: str = ""
    next_attempt_at: str = ""
    attempt_fence: str = ""
    lease_expires_at: str = ""
    scanner_engine: str = ""
    scanner_engine_version: str = ""
    definition_version: str = ""
    definition_timestamp: str = ""
    threat_code: str = ""
    error_code: str = ""
    started_at: str = ""
    completed_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ScannerRuntimeStatus:
    tenant_id: str
    worker_id: str
    readiness: str = "unknown"
    scanner_engine: str = ""
    scanner_engine_version: str = ""
    definition_version: str = ""
    definition_timestamp: str = ""
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH
    last_successful_refresh_at: str = ""
    last_successful_scan_at: str = ""
    pending_count: int = 0
    recent_error_counts: Mapping[str, int] = field(default_factory=dict)
    heartbeat_at: str = ""
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MalwareActivationState:
    singleton_id: bool = True
    schema_revision: str = "0034_drive_malware_quarantine"
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH
    state: str = "pending"
    cursor: str = ""
    total_revisions: int = 0
    processed_revisions: int = 0
    legacy_bytes: int = 0
    quarantine_bytes: int = 0
    activated_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class DriveMalwareJobSpec:
    """Durable content-free outbox entry used by the memory implementation."""

    job_id: str
    tenant_id: str
    account_id: str
    space_id: str
    scan_id: str
    revision_id: str
    origin: str
    requested_by: str
    max_attempts: int = 5
    run_after: str = ""
    idempotency_key: str = ""


@dataclass(frozen=True)
class DriveQuarantinedCompletion:
    upload: DriveUploadSession
    file: DriveFile
    revision: DriveRevision
    scan: DriveMalwareScan
    scan_job_id: str


@dataclass(frozen=True)
class DriveMalwareCompletion:
    scan: DriveMalwareScan
    file: Optional[DriveFile] = None
    ingestion_job_id: str = ""
    applied: bool = True


@dataclass(frozen=True)
class DriveMalwareReconcileResult:
    recovered_attempts: int = 0
    created_attempts: int = 0
    enqueued_jobs: int = 0


@dataclass(frozen=True)
class DriveQuarantineUsage:
    usage_bytes: int = 0
    reserved_bytes: int = 0
    quarantined_bytes: int = 0


@dataclass(frozen=True)
class DriveMalwareOperationalCounts:
    pending_count: int = 0
    quarantine_usage_bytes: int = 0
    quarantine_reserved_bytes: int = 0
    quarantined_revision_bytes: int = 0


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
    def get_file_list_details(
        self, *, account_id: str, space_id: str, revision_ids: Sequence[str],
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Mapping[str, DriveFileListDetail]: ...
    def list_revisions(self, file_id: str, *, account_id: str, space_id: str) -> list[DriveRevision]: ...
    def create_upload(self, upload: DriveUploadSession) -> DriveUploadSession: ...
    def reserve_upload(self, upload: DriveUploadSession) -> DriveUploadSession: ...
    def get_upload(self, upload_id: str, *, tenant_id: str = "") -> Optional[DriveUploadSession]: ...
    def get_upload_by_idempotency(
        self, *, account_id: str, space_id: str, created_by: str, idempotency_key: str,
    ) -> Optional[DriveUploadSession]: ...
    def update_upload(self, upload: DriveUploadSession) -> DriveUploadSession: ...
    def release_upload_reservation(
        self, upload_id: str, *, tenant_id: str, account_id: str, space_id: str,
    ) -> DriveUploadSession: ...
    def list_expired_uploads(
        self, *, tenant_id: str, account_id: str, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]: ...
    def complete_upload_quarantined(
        self, *, upload: DriveUploadSession, file: DriveFile, revision: DriveRevision,
        scan: DriveMalwareScan, scan_job_id: str, scan_job_max_attempts: int = 5,
    ) -> DriveQuarantinedCompletion: ...
    def create_malware_scan(self, scan: DriveMalwareScan) -> DriveMalwareScan: ...
    def get_malware_scan(
        self, scan_id: str, *, account_id: str, space_id: str,
    ) -> Optional[DriveMalwareScan]: ...
    def list_malware_scans(
        self, revision_id: str, *, account_id: str, space_id: str,
    ) -> list[DriveMalwareScan]: ...
    def get_authoritative_malware_scan(
        self, revision_id: str, *, account_id: str, space_id: str,
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
    ) -> Optional[DriveMalwareScan]: ...
    def malware_scan_job_status(self, scan: DriveMalwareScan) -> str: ...
    def update_malware_scan(
        self, scan: DriveMalwareScan, *, expected_status: str,
    ) -> DriveMalwareScan: ...
    def list_scanner_runtime_status(self, *, tenant_id: str) -> list[ScannerRuntimeStatus]: ...
    def get_malware_activation_state(self) -> MalwareActivationState: ...
    def quarantine_limit_bytes(self) -> int: ...
    def malware_operational_counts(self, *, tenant_id: str) -> DriveMalwareOperationalCounts: ...
    def list_pending_malware_job_specs(self, *, limit: int = 100) -> list[DriveMalwareJobSpec]: ...
    def acknowledge_malware_job_spec(self, job_id: str) -> None: ...
    def request_malware_rescan(
        self, *, file_id: str, account_id: str, space_id: str,
        expected_generation: int, requested_by: str, scan_id: str, scan_job_id: str,
        idempotency_key: str = "",
        policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH, scan_job_max_attempts: int = 5,
    ) -> tuple[DriveFile, DriveMalwareScan]: ...
    def publish_projection(
        self, *, file_id: str, revision_id: str, generation: int,
        account_id: str, space_id: str, chunks: Sequence[Chunk],
    ) -> DriveProjectionResult: ...
    def unpublish(self, *, file_id: str, account_id: str, space_id: str, generation: int) -> DriveFile: ...
    def list_pending_review(self, *, account_id: str, space_id: str = "") -> list[DriveFile]: ...
    def delete_file(self, *, file_id: str, account_id: str, space_id: str) -> dict[str, int]: ...
    def export_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict: ...
    def delete_scope(self, *, tenant_id: str, account_id: str, space_id: str = "") -> dict[str, int]: ...


class DriveMalwareWorkerStore(DriveStore, Protocol):
    """Drive persistence capabilities reserved for the malware worker.

    API request handlers receive only ``DriveStore``.  This extended protocol
    makes fenced evidence transitions, cross-account maintenance, and global
    quarantine accounting explicit at the composition boundary.
    """

    def list_expired_uploads_for_maintenance(
        self, *, before: str, limit: int = 500,
    ) -> list[DriveUploadSession]: ...
    def upsert_scanner_runtime_status(
        self, status: ScannerRuntimeStatus,
    ) -> ScannerRuntimeStatus: ...
    def quarantine_usage_bytes(self) -> int: ...
    def reconcile_quarantine_capacity(self) -> DriveQuarantineUsage: ...
    def list_malware_tenant_ids(
        self, *, after: str = "", limit: int = 1_000,
    ) -> list[str]: ...
    def begin_malware_scan(
        self, *, job_id: str, lease_token: str, lease_expires_at: str,
        scan_id: str, attempt_fence: str,
    ) -> DriveMalwareScan: ...
    def complete_malware_scan(
        self, *, job_id: str, lease_token: str, scan_id: str, attempt_fence: str,
        verdict: ScanVerdict, next_attempt_at: str = "", consecutive_failures: int = 0,
    ) -> DriveMalwareCompletion: ...
    def reconcile_malware_scans(self, *, limit: int = 100) -> DriveMalwareReconcileResult: ...
    def wake_retryable_malware_scans(self, *, limit: int = 100) -> int: ...


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def drive_ingest_idempotency_key(
    file_id: str,
    revision_id: str,
    generation: int,
) -> str:
    """Return the canonical identity shared by scan completion and repair."""

    file_id = (file_id or "").strip()
    revision_id = (revision_id or "").strip()
    generation = int(generation)
    if not file_id or not revision_id or generation < 0:
        raise ValueError("Drive ingestion identity is invalid.")
    return f"drive-ingest:{file_id}:{revision_id}:{generation}"


def drive_ingest_job_id(
    file_id: str,
    revision_id: str,
    generation: int,
) -> str:
    """Derive the durable queue id without relying on enqueue ordering.

    PostgreSQL uses the equivalent built-in ``md5(text)`` expression inside
    the scan-completion transaction. The digest is an opaque identity, not a
    security checksum.
    """

    key = drive_ingest_idempotency_key(file_id, revision_id, generation)
    digest = hashlib.md5(  # noqa: S324 - deterministic identifier, not security
        f"onebrain:{key}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"job_{digest}"


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


def normalize_file_list_detail_revision_ids(revision_ids: Sequence[str]) -> tuple[str, ...]:
    """Validate, bound, and stably deduplicate one list-detail request."""

    if isinstance(revision_ids, (str, bytes)):
        raise ValueError("Drive list-detail revision ids must be a sequence of ids.")
    values = tuple(revision_ids)
    if len(values) > MAX_FILE_LIST_DETAIL_BATCH:
        raise DriveLimitError(
            f"Drive list-detail batches cannot exceed {MAX_FILE_LIST_DETAIL_BATCH} revisions."
        )
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Drive list-detail revision ids must be strings.")
        revision_id = validate_opaque_id(value, field_name="revision_id")
        if revision_id not in seen:
            seen.add(revision_id)
            unique.append(revision_id)
    return tuple(unique)


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
    if upload.quarantine_reserved_bytes < 0:
        raise ValueError("Upload quarantine reservation cannot be negative.")
    if upload.quarantine_reserved_bytes > upload.size_bytes:
        raise ValueError("Upload quarantine reservation cannot exceed its declared size.")
    if upload.reservation_state not in DRIVE_QUARANTINE_RESERVATION_STATES:
        raise ValueError("Unknown Drive quarantine reservation state.")
    if upload.reservation_state == "reserved" and upload.quarantine_reserved_bytes <= 0:
        raise ValueError("A reserved upload requires positive quarantine bytes.")


def validate_malware_scan(scan: DriveMalwareScan) -> None:
    _validate_scope(scan.tenant_id, scan.account_id, scan.space_id)
    for field_name in ("id", "file_id", "revision_id"):
        validate_opaque_id(getattr(scan, field_name), field_name=field_name)
    if not re.fullmatch(r"[0-9a-f]{64}", scan.revision_sha256 or ""):
        raise ValueError("Malware scan revision sha256 is invalid.")
    if scan.revision_size_bytes < 0:
        raise ValueError("Malware scan revision size must be non-negative.")
    if scan.policy_epoch < 1 or scan.attempt_sequence < 1:
        raise ValueError("Malware policy epoch and attempt sequence must be positive.")
    if scan.consecutive_failures < 0:
        raise ValueError("Consecutive malware scan failures cannot be negative.")
    if scan.status not in DRIVE_MALWARE_STATUSES:
        raise ValueError("Unknown Drive malware status.")
    if scan.origin not in DRIVE_MALWARE_ORIGINS:
        raise ValueError("Unknown Drive malware scan origin.")
    validate_opaque_id(scan.job_id, field_name="job_id", allow_empty=True)
    validate_opaque_id(scan.attempt_fence, field_name="attempt_fence", allow_empty=True)
    for field_name in ("scanner_engine", "scanner_engine_version", "definition_version"):
        validate_scanner_identifier(
            getattr(scan, field_name), field_name=field_name, allow_empty=True
        )
    validate_malware_code(scan.threat_code, field_name="threat_code")
    validate_malware_code(scan.error_code, field_name="error_code")
    if scan.status == "clean":
        if not (
            scan.scanner_engine
            and scan.scanner_engine_version
            and scan.definition_version
            and scan.definition_timestamp
            and scan.completed_at
        ):
            raise ValueError("A clean malware attempt requires complete scanner evidence.")
        if scan.threat_code or scan.error_code:
            raise ValueError("A clean malware attempt cannot contain threat or error codes.")
    elif scan.status == "infected":
        if not scan.threat_code or scan.error_code:
            raise ValueError("An infected malware attempt requires only a threat code.")
    elif scan.status == "scan_error":
        if not scan.error_code or scan.threat_code:
            raise ValueError("A failed malware attempt requires only an error code.")


def validate_scanner_runtime_status(status: ScannerRuntimeStatus) -> None:
    if not status.tenant_id.strip():
        raise ValueError("Scanner runtime tenant_id is required.")
    validate_opaque_id(status.worker_id, field_name="worker_id")
    if status.readiness not in DRIVE_SCANNER_READINESS_STATES:
        raise ValueError("Unknown scanner runtime readiness.")
    if status.policy_epoch < 1 or status.pending_count < 0:
        raise ValueError("Scanner runtime counters and policy epoch must be non-negative.")
    for field_name in ("scanner_engine", "scanner_engine_version", "definition_version"):
        validate_scanner_identifier(
            getattr(status, field_name), field_name=field_name, allow_empty=True
        )
    for code, count in status.recent_error_counts.items():
        validate_malware_code(str(code), field_name="runtime_error_code", allow_empty=False)
        if int(count) < 0:
            raise ValueError("Scanner runtime error counts cannot be negative.")


def validate_malware_activation_state(state: MalwareActivationState) -> None:
    if state.singleton_id is not True:
        raise ValueError("Malware activation state must use the singleton identity.")
    if state.policy_epoch < 1 or state.state not in DRIVE_MALWARE_ACTIVATION_STATES:
        raise ValueError("Malware activation state is invalid.")
    for value in (
        state.total_revisions, state.processed_revisions, state.legacy_bytes,
        state.quarantine_bytes,
    ):
        if value < 0:
            raise ValueError("Malware activation counts cannot be negative.")
    if state.processed_revisions > state.total_revisions:
        raise ValueError("Malware activation progress cannot exceed its total.")


def authoritative_malware_scan(
    scans: Iterable[DriveMalwareScan], *, policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
) -> Optional[DriveMalwareScan]:
    """Return the highest append-only attempt for one revision/current epoch."""

    candidates = [scan for scan in scans if scan.policy_epoch == policy_epoch]
    if not candidates:
        return None
    revision_ids = {scan.revision_id for scan in candidates}
    if len(revision_ids) != 1:
        raise ValueError("Authoritative malware attempts must belong to one revision.")
    return max(candidates, key=lambda scan: (scan.attempt_sequence, scan.id))


def is_clean_attestation(
    revision: DriveRevision,
    scan: Optional[DriveMalwareScan],
    *,
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
) -> bool:
    """Whether evidence authorizes consumers to read exact revision bytes."""

    return bool(
        malware_scan_matches_revision(revision, scan, policy_epoch=policy_epoch)
        and scan
        and scan.status == "clean"
        and scan.scanner_engine
        and scan.scanner_engine_version
        and scan.definition_version
        and scan.definition_timestamp
        and scan.completed_at
    )


def malware_scan_matches_revision(
    revision: DriveRevision,
    scan: Optional[DriveMalwareScan],
    *,
    policy_epoch: int = DRIVE_MALWARE_POLICY_EPOCH,
) -> bool:
    """Whether an attempt describes the exact scoped bytes of a revision."""

    return bool(
        scan
        and scan.policy_epoch == policy_epoch
        and scan.revision_id == revision.id
        and scan.file_id == revision.file_id
        and scan.tenant_id == revision.tenant_id
        and scan.account_id == revision.account_id
        and scan.space_id == revision.space_id
        and scan.revision_sha256 == revision.sha256
        and scan.revision_size_bytes == revision.size_bytes
    )


def file_list_detail_matches_file(file: DriveFile, detail: DriveFileListDetail) -> bool:
    """Whether list metadata belongs to the exact file snapshot being rendered."""

    revision = detail.revision
    return bool(
        revision
        and file.current_revision_id
        and revision.id == file.current_revision_id
        and revision.file_id == file.id
        and revision.tenant_id == file.tenant_id
        and revision.account_id == file.account_id
        and revision.space_id == file.space_id
    )


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
