"""Retention enforcement over account/space-scoped stores."""

from __future__ import annotations


SUPPORTED_DOMAINS = ("documents", "conversations", "intake", "governance")


def run_retention(*, account_id: str, space_id: str = "", domain: str = "", dry_run: bool = True) -> dict:
    from app.deps import get_conversation_store, get_intake_store, get_platform_store, get_store

    platform = get_platform_store()
    policies = [
        policy for policy in platform.list_retention_policies(account_id, space_id)
        if policy.status == "active" and (not domain or policy.domain == domain)
    ]
    active_domains = {policy.domain for policy in policies}
    if domain:
        active_domains &= {domain}
    active_domains &= set(SUPPORTED_DOMAINS)

    result = {
        "account_id": account_id,
        "space_id": space_id,
        "domain": domain,
        "dry_run": dry_run,
        "policies": len(policies),
        "counts": {},
    }
    if not active_domains:
        return result

    if "documents" in active_domains:
        docs = get_store().export_documents(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["documents"] = len(docs)
        result["counts"]["chunks"] = sum(len(doc.get("chunks", [])) for doc in docs)
        if not dry_run:
            deleted = get_store().delete_documents_by_scope(account_id, account_id=account_id, space_id=space_id)
            result["counts"]["documents_deleted"] = deleted["documents"]
            result["counts"]["chunks_deleted"] = deleted["chunks"]

    if "conversations" in active_domains:
        conversations = get_conversation_store().export_scope(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["conversations"] = len(conversations)
        if not dry_run:
            result["counts"]["conversations_deleted"] = get_conversation_store().delete_scope(
                account_id, account_id=account_id, space_id=space_id,
            )

    if "intake" in active_domains:
        records = get_intake_store().export_records(account_id, account_id=account_id, space_id=space_id)
        result["counts"]["intake_records"] = len(records)
        if not dry_run:
            result["counts"]["intake_records_deleted"] = get_intake_store().delete_records_by_scope(
                account_id, account_id=account_id, space_id=space_id,
            )

    if "governance" in active_domains:
        governance = {
            "consent_records": len(platform.list_consent_records(account_id, space_id)),
            "data_access_events": len(platform.list_data_access_events(account_id, space_id)),
        }
        result["counts"]["governance"] = governance
        if not dry_run:
            result["counts"]["governance_deleted"] = platform.delete_governance_by_scope(account_id, space_id)

    return result
