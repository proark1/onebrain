"""Retention enforcement over account/space-scoped stores.

Deletes only records OLDER than a policy's `duration_days`, never the whole scope.
Where a domain has more than one active policy, the LONGEST duration wins — the
conservative choice that never deletes data another policy wants kept (a stricter
"shortest-wins" storage-limitation mode is a future option). Legal hold beats
retention: a held scope is counted but never deleted. Every non-dry sweep is
recorded in `retention_runs`.

Age is read from a per-record timestamp: conversations and intake records carry
`created_at`, document chunks carry it in meta from ingest, and KPI observations
use the server-controlled `received_at`. A record with no
timestamp is never aged out — retention can only delete what it can prove is old
enough. The `governance` domain is not age-filtered (its records are membership /
consent / policy metadata, not time-series data); a governance policy still tears
down the whole governance scope.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4


SUPPORTED_DOMAINS = ("documents", "drive", "conversations", "intake", "kpis", "governance")


_retention_run_timestamp_lock = Lock()
_last_retention_run_timestamp: datetime | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recorded_run_timestamp() -> str:
    """Return a locally monotonic timestamp for the append-only run ledger.

    Retention sweeps may run inside a frozen clock (or faster than the clock's
    resolution).  Advancing tied values by one microsecond keeps the audit
    record's causal order intact without changing the retention cutoff clock.
    """
    global _last_retention_run_timestamp
    with _retention_run_timestamp_lock:
        recorded_at = datetime.now(timezone.utc)
        if _last_retention_run_timestamp and recorded_at <= _last_retention_run_timestamp:
            recorded_at = _last_retention_run_timestamp + timedelta(microseconds=1)
        _last_retention_run_timestamp = recorded_at
        return recorded_at.isoformat()


def _doc_created_at(doc: dict) -> str:
    chunks = doc.get("chunks") or []
    return chunks[0].get("meta", {}).get("created_at", "") if chunks else ""


def _older(created_at: str, cutoff: str) -> bool:
    return bool(cutoff) and bool(created_at) and created_at < cutoff


def run_retention(*, account_id: str, space_id: str = "", domain: str = "", dry_run: bool = True) -> dict:
    """Run one sweep, serializing destructive work with legal-hold creation."""

    from app.deps import get_platform_store

    platform = get_platform_store()
    guard_factory = getattr(platform, "deletion_guard", None)
    guard = (
        guard_factory(account_id, space_id)
        if not dry_run and callable(guard_factory)
        else nullcontext()
    )
    with guard:
        return _run_retention_guarded(
            account_id=account_id,
            space_id=space_id,
            domain=domain,
            dry_run=dry_run,
            platform=platform,
        )


def _run_retention_guarded(
    *,
    account_id: str,
    space_id: str,
    domain: str,
    dry_run: bool,
    platform,
) -> dict:
    from app.deps import (
        get_conversation_store,
        get_drive_blob_store,
        get_drive_store,
        get_intake_store,
        get_kpi_store,
        get_store,
    )
    from app.drive.blobs import drive_scope_prefix
    from app.platform.base import AuditEvent, RetentionRun, Tombstone, scope_is_held, target_is_held

    policies = [
        policy for policy in platform.list_retention_policies(account_id, space_id)
        if policy.status == "active" and (not domain or policy.domain == domain)
    ]
    active_domains = {policy.domain for policy in policies}
    if domain:
        active_domains &= {domain}
    active_domains &= set(SUPPORTED_DOMAINS)

    active_holds = platform.list_legal_holds(account_id)
    held = scope_is_held(active_holds, space_id)
    delete_ok = not dry_run and not held
    now = datetime.now(timezone.utc)

    def cutoff_for(target_domain: str) -> str:
        durations = [p.duration_days for p in policies if p.domain == target_domain]
        if not durations:
            return ""
        return (now - timedelta(days=max(durations))).isoformat()

    result = {
        "account_id": account_id,
        "space_id": space_id,
        "domain": domain,
        "dry_run": dry_run,
        "policies": len(policies),
        "legal_hold": held,
        "cutoffs": {},
        "counts": {},
    }
    if not active_domains:
        return result

    if "documents" in active_domains:
        cutoff = cutoff_for("documents")
        result["cutoffs"]["documents"] = cutoff
        docs = get_store().export_documents(account_id, account_id=account_id, space_id=space_id)
        eligible = [d for d in docs if _older(_doc_created_at(d), cutoff)]
        result["counts"]["documents"] = len(eligible)
        result["counts"]["chunks"] = sum(len(d.get("chunks", [])) for d in eligible)
        if delete_ok:
            deleted = get_store().delete_documents_by_scope(
                account_id, account_id=account_id, space_id=space_id, older_than=cutoff,
            )
            result["counts"]["documents_deleted"] = deleted["documents"]
            result["counts"]["chunks_deleted"] = deleted["chunks"]

    if "drive" in active_domains:
        cutoff = cutoff_for("drive")
        result["cutoffs"]["drive"] = cutoff
        drive_store = get_drive_store()
        drive_scope = drive_store.export_scope(
            tenant_id=account_id, account_id=account_id, space_id=space_id,
        )
        eligible = [row for row in drive_scope.get("files", []) if _older(row.get("created_at", ""), cutoff)]
        result["counts"]["drive_files"] = len(eligible)
        result["counts"]["drive_revisions"] = sum(
            1 for row in drive_scope.get("revisions", [])
            if row.get("file_id") in {file["id"] for file in eligible}
        )
        revisions_by_file: dict[str, list[dict]] = {}
        for revision in drive_scope.get("revisions", []):
            revisions_by_file.setdefault(revision.get("file_id", ""), []).append(revision)
        held_file_ids = {
            file["id"]
            for file in eligible
            if target_is_held(
                active_holds,
                space_id=file["space_id"],
                target_refs={
                    file["id"],
                    f"drive_file:{file['id']}",
                    *{
                        value
                        for revision in revisions_by_file.get(file["id"], [])
                        for value in (
                            revision["id"],
                            f"drive_revision:{revision['id']}",
                        )
                    },
                },
            )
        }
        result["counts"]["drive_files_held"] = len(held_file_ids)
        if delete_ok:
            blobs = get_drive_blob_store()
            deleted_files = deleted_revisions = deleted_chunks = deleted_blobs = 0
            for file in eligible:
                if file["id"] in held_file_ids:
                    continue
                file_prefix = "/".join((
                    drive_scope_prefix(
                        file.get("tenant_id") or account_id,
                        file.get("account_id") or account_id,
                        file["space_id"],
                    ),
                    file["id"],
                ))
                deleted_blobs += blobs.delete_prefix(file_prefix)
                if blobs.delete_prefix(file_prefix):
                    raise RuntimeError(
                        f"Drive retention verification found residual objects for {file['id']}."
                    )
                deleted = drive_store.delete_file(
                    file_id=file["id"], account_id=account_id, space_id=file["space_id"],
                )
                deleted_files += deleted["files"]
                deleted_revisions += deleted["revisions"]
                deleted_chunks += deleted["chunks"]
                platform.create_tombstone(Tombstone(
                    id=f"tomb_{uuid4().hex}", account_id=account_id, space_id=file["space_id"],
                    target_type="subject", target_ref=f"drive_file:{file['id']}",
                    reason="drive_retention", created_by="system:retention", created_at=_now(),
                ))
                platform.record_audit(AuditEvent(
                    id=f"aud_{uuid4().hex}", account_id=account_id, space_id=file["space_id"],
                    actor_id="system:retention", actor_type="system", action="drive.file.retained_delete",
                    target_type="drive_file", target_id=file["id"], app_id="onebrain_core",
                    purpose="knowledge_management", decision="completed",
                    meta={"revisions": deleted["revisions"], "chunks": deleted["chunks"]},
                ))
            result["counts"].update({
                "drive_files_deleted": deleted_files,
                "drive_revisions_deleted": deleted_revisions,
                "drive_chunks_deleted": deleted_chunks,
                "drive_blobs_deleted": deleted_blobs,
            })

    if "conversations" in active_domains:
        cutoff = cutoff_for("conversations")
        result["cutoffs"]["conversations"] = cutoff
        conversations = get_conversation_store().export_scope(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["conversations"] = sum(1 for c in conversations if _older(c.get("created_at", ""), cutoff))
        if delete_ok:
            result["counts"]["conversations_deleted"] = get_conversation_store().delete_scope(
                account_id, account_id=account_id, space_id=space_id, older_than=cutoff,
            )

    if "intake" in active_domains:
        cutoff = cutoff_for("intake")
        result["cutoffs"]["intake"] = cutoff
        records = get_intake_store().export_records(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["intake_records"] = sum(1 for r in records if _older(r.get("created_at", ""), cutoff))
        if delete_ok:
            result["counts"]["intake_records_deleted"] = get_intake_store().delete_records_by_scope(
                account_id, account_id=account_id, space_id=space_id, older_than=cutoff,
            )

    if "kpis" in active_domains:
        cutoff = cutoff_for("kpis")
        result["cutoffs"]["kpis"] = cutoff
        result["counts"]["kpis"] = get_kpi_store().retention_scope(
            account_id, space_id, older_than=cutoff, delete=delete_ok,
        )

    if "governance" in active_domains:
        # Governance records are metadata, not time-series; a governance policy
        # tears down the whole governance scope (not age-filtered).
        governance = {
            "consent_records": len(platform.list_consent_records(account_id, space_id)),
            "data_access_events": len(platform.list_data_access_events(account_id, space_id)),
        }
        result["counts"]["governance"] = governance
        if delete_ok:
            result["counts"]["governance_deleted"] = platform.delete_governance_by_scope(account_id, space_id)

    if not dry_run:
        recorded_at = _recorded_run_timestamp()
        previous_timestamps = [
            row.created_at
            for row in platform.list_retention_runs(account_id, space_id)
            if row.created_at
        ]
        if previous_timestamps and recorded_at <= max(previous_timestamps):
            latest = datetime.fromisoformat(max(previous_timestamps).replace("Z", "+00:00"))
            recorded_at = (latest + timedelta(microseconds=1)).isoformat()
        platform.record_retention_run(RetentionRun(
            id=f"ret_{uuid4().hex}",
            account_id=account_id,
            space_id=space_id,
            domain=domain,
            dry_run=False,
            status="skipped_legal_hold" if held else "completed",
            result=result,
            created_at=recorded_at,
            completed_at=recorded_at,
        ))

    return result
