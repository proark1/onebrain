"""Retention enforcement over account/space-scoped stores.

Deletes only records OLDER than a policy's `duration_days`, never the whole scope.
Where a domain has more than one active policy, the LONGEST duration wins — the
conservative choice that never deletes data another policy wants kept (a stricter
"shortest-wins" storage-limitation mode is a future option). Legal hold beats
retention: a held scope is counted but never deleted. Every non-dry sweep is
recorded in `retention_runs`.

Age is read from a per-record timestamp: conversations and intake records carry
`created_at`; document chunks carry it in meta from ingest. A record with no
timestamp is never aged out — retention can only delete what it can prove is old
enough. The `governance` domain is not age-filtered (its records are membership /
consent / policy metadata, not time-series data); a governance policy still tears
down the whole governance scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4


SUPPORTED_DOMAINS = ("documents", "conversations", "intake", "governance")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _doc_created_at(doc: dict) -> str:
    chunks = doc.get("chunks") or []
    return chunks[0].get("meta", {}).get("created_at", "") if chunks else ""


def _older(created_at: str, cutoff: str) -> bool:
    return bool(cutoff) and bool(created_at) and created_at < cutoff


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

    held = scope_is_held(platform.list_legal_holds(account_id), space_id)
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
