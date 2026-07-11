"""Retention enforcement over account/space-scoped stores.

Legal hold beats retention: a scope under an active hold is still counted (so a
dry run shows what *would* go) but never deleted. Every non-dry sweep is recorded
in `retention_runs` for an audit trail.

NOTE (Phase 1b, tracked in docs/deletion-tombstone-contract.md §5.1): the delete
methods here still remove the *whole* scope rather than only records older than
`policy.duration_days`. Age-aware deletion lands with the store-level cutoff
support; until then the retention job must NOT be scheduled unattended.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4


SUPPORTED_DOMAINS = ("documents", "conversations", "intake", "governance")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_retention(*, account_id: str, space_id: str = "", domain: str = "", dry_run: bool = True) -> dict:
    from app.deps import get_conversation_store, get_intake_store, get_platform_store, get_store
    from app.platform.base import RetentionRun, scope_is_held

    platform = get_platform_store()
    policies = [
        policy for policy in platform.list_retention_policies(account_id, space_id)
        if policy.status == "active" and (not domain or policy.domain == domain)
    ]
    active_domains = {policy.domain for policy in policies}
    if domain:
        active_domains &= {domain}
    active_domains &= set(SUPPORTED_DOMAINS)

    # Legal hold beats retention. A held scope is counted but never deleted.
    held = scope_is_held(platform.list_legal_holds(account_id), space_id)
    delete_ok = not dry_run and not held

    result = {
        "account_id": account_id,
        "space_id": space_id,
        "domain": domain,
        "dry_run": dry_run,
        "policies": len(policies),
        "legal_hold": held,
        "counts": {},
    }
    if not active_domains:
        return result

    if "documents" in active_domains:
        docs = get_store().export_documents(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["documents"] = len(docs)
        result["counts"]["chunks"] = sum(len(doc.get("chunks", [])) for doc in docs)
        if delete_ok:
            deleted = get_store().delete_documents_by_scope(account_id, account_id=account_id, space_id=space_id)
            result["counts"]["documents_deleted"] = deleted["documents"]
            result["counts"]["chunks_deleted"] = deleted["chunks"]

    if "conversations" in active_domains:
        conversations = get_conversation_store().export_scope(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["conversations"] = len(conversations)
        if delete_ok:
            result["counts"]["conversations_deleted"] = get_conversation_store().delete_scope(
                account_id, account_id=account_id, space_id=space_id,
            )

    if "intake" in active_domains:
        records = get_intake_store().export_records(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["intake_records"] = len(records)
        if delete_ok:
            result["counts"]["intake_records_deleted"] = get_intake_store().delete_records_by_scope(
                account_id, account_id=account_id, space_id=space_id,
            )

    if "governance" in active_domains:
        governance = {
            "consent_records": len(platform.list_consent_records(account_id, space_id)),
            "data_access_events": len(platform.list_data_access_events(account_id, space_id)),
        }
        result["counts"]["governance"] = governance
        if delete_ok:
            result["counts"]["governance_deleted"] = platform.delete_governance_by_scope(account_id, space_id)

    # Record every real sweep (including a hold-skipped one) for the audit trail.
    if not dry_run:
        platform.record_retention_run(RetentionRun(
            id=f"ret_{uuid4().hex}",
            account_id=account_id,
            space_id=space_id,
            domain=domain,
            dry_run=False,
            status="skipped_legal_hold" if held else "completed",
            result=result,
            created_at=_now(),
            completed_at=_now(),
        ))

    return result
