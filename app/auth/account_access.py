"""Account-scoped authorization for human admin / DPO operations.

Holding the `admin` role is NOT enough to touch an account. The admin must also
be authorized *for that specific account* — either as its owner or through an
active admin-capable membership. This is the boundary that stops an admin of one
account (e.g. nft_gym) from reaching another account's (e.g. Communication) apps,
consent, credentials, retention, audit, or GDPR export/erase.

To avoid leaking which accounts exist, "account not found" and "you are not
authorized for this account" return the SAME 404 — an admin cannot enumerate
other accounts by probing ids.
"""

from __future__ import annotations

from fastapi import HTTPException

from app.auth.principal import Principal
from app.platform.base import Account

# Human roles allowed to administer accounts at all (the platform-level gate).
ADMIN_ROLE_IDS = frozenset({"admin"})
# Membership roles that convey account-admin authority over a specific account.
ADMIN_MEMBERSHIP_ROLE_IDS = frozenset({"admin", "owner", "dpo"})


def _is_admin_principal(principal: Principal) -> bool:
    return principal.principal_type == "human" and principal.role_id in ADMIN_ROLE_IDS


def is_account_admin(principal: Principal, account: Account | None, store) -> bool:
    """True when this human admin owns or has an active admin membership in `account`."""
    if account is None or not _is_admin_principal(principal):
        return False
    if account.owner_user_id and account.owner_user_id == principal.user_id:
        return True
    return any(
        m.user_id == principal.user_id
        and m.status == "active"
        and m.role_id in ADMIN_MEMBERSHIP_ROLE_IDS
        for m in store.list_memberships(account.id)
    )


def authorize_account_admin(principal: Principal, account_id: str, store) -> Account:
    """Enforce account-scoped admin authorization; return the account or raise.

    - 403 if the caller is not an admin at all.
    - 404 if the account does not exist OR the admin is not authorized for it
      (identical response so account existence cannot be probed).
    """
    if not _is_admin_principal(principal):
        raise HTTPException(status_code=403, detail="Admin role required.")
    account = store.get_account((account_id or "").strip())
    if not is_account_admin(principal, account, store):
        raise HTTPException(status_code=404, detail="Account not found.")
    return account


def authorized_account_ids(principal: Principal, store) -> set[str]:
    """The account ids this admin may see — those they own or admin via membership."""
    if not _is_admin_principal(principal):
        return set()
    return {a.id for a in store.list_accounts() if is_account_admin(principal, a, store)}
