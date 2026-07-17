"""Login / logout — issues, revokes, and force-revokes server-side sessions."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.auth.login_limits import client_ip_from_request
from app.auth.passwords import DUMMY_HASH, hash_password, verify_password
from app.auth.principal import SESSION_COOKIE, Principal, resolve_principal
from app.auth.tokens import make_session_token, read_session_token
from app.config import get_settings
from app.deps import get_login_throttle, get_session_store, get_user_store
from app.monitoring import record_auth_failure
from app.schemas import LoginRequest, RevokeSessionsRequest
from app.sessions.base import Session

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, response: Response, request: Request = None):
    settings = get_settings()
    throttle = get_login_throttle()
    account_key = "account:" + body.email.strip().lower()
    client_ip = client_ip_from_request(
        request,
        trusted_proxy_cidrs=getattr(settings, "trusted_proxy_cidrs", ""),
        trusted_proxy_hops=getattr(settings, "trusted_proxy_hops", 0),
    )
    ip_key = "ip:" + client_ip

    # Reserve both budgets before expensive password verification. PostgreSQL
    # throttles make this atomic across replicas; local throttles retain the
    # same semantics for development/tests. The address comes from the peer
    # unless an explicit trusted proxy policy says otherwise.
    wait = throttle.reserve(account_key)
    if wait == 0:
        wait = throttle.reserve(ip_key)
        if wait > 0:
            # The account reservation succeeded, but the request was rejected
            # before any password work because this address is already locked.
            # Return that one reservation so a blocked IP cannot consume
            # arbitrary accounts' budgets and cause a denial of service.
            throttle.release_success(account_key)
    if wait > 0:
        record_auth_failure("login_locked")
        raise HTTPException(
            status_code=429, detail="Too many failed attempts. Please wait and try again.",
            headers={"Retry-After": str(wait)},
        )

    user = get_user_store().get_by_email(body.email)

    # Always run a hash comparison (dummy when the user is unknown) so timing
    # doesn't reveal whether an email exists.
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if not user or user.status != "active" or not ok:
        record_auth_failure("login_invalid")
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    throttle.record_success(account_key)
    # Keep other failures from this address, but return this verified user's
    # one reservation so successful traffic does not consume the IP budget.
    throttle.release_success(ip_key)
    ttl = settings.session_days * 86400

    # Create the server-side session first, then sign a token bound to it. The
    # token is worthless without a live session row, so revocation is immediate.
    now = datetime.now(timezone.utc)
    session = get_session_store().create(Session(
        id=uuid.uuid4().hex,
        user_id=user.id,
        tenant_id=user.tenant_id,
        created_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl)).isoformat(),
    ))
    token = make_session_token(user.id, session.id, settings.auth_secret, ttl)
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=ttl, httponly=True, samesite="lax",
        secure=settings.cookie_secure, path="/",
    )
    # H-10: surface the flag so the client redirects to a change-password screen.
    # The session is still issued — resolve_principal's gate blocks privileged
    # calls until the credential is rotated.
    return {"email": user.email, "display_name": user.display_name, "role_id": user.role_id,
            "must_change_password": user.must_change_password}


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, principal: Principal = Depends(resolve_principal)):
    """Self-service rotation (authenticated; acts only on the caller's own user).
    Clears must_change_password and burns every session so the OTP session cannot
    be replayed after the change (H-10)."""
    store = get_user_store()
    user = store.get(principal.user_id)
    if not user or not verify_password(body.current_password, user.password_hash):
        record_auth_failure("change_password_invalid")
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if body.new_password == body.current_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current one.")
    store.update_password(user.id, hash_password(body.new_password), must_change_password=False)
    revoked = get_session_store().revoke_all_for_user(user.id)
    return {"ok": True, "sessions_revoked": revoked}


@router.post("/logout")
def logout(response: Response, ob_session: str = Cookie(default="")):
    # Revoke the presented session so the token cannot be replayed after logout.
    parsed = read_session_token(ob_session, get_settings().auth_secret) if ob_session else None
    if parsed:
        get_session_store().revoke(parsed[1])
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.post("/sessions/revoke")
def revoke_sessions(body: RevokeSessionsRequest, principal: Principal = Depends(resolve_principal)):
    """Force-log-out every active session for a user — the offboarding lever.

    Admin-only, and confined to the caller's own tenant so one company's admin
    can never revoke another company's sessions.
    """
    if principal.role_id != "admin":
        raise HTTPException(status_code=403, detail="Only admin can revoke another user's sessions.")

    target = None
    if body.user_id:
        target = get_user_store().get(body.user_id)
    elif body.email:
        target = get_user_store().get_by_email(body.email)
    if not target or target.tenant_id != principal.tenant_id:
        # 404, not 403, so an admin can't enumerate users in other tenants.
        raise HTTPException(status_code=404, detail="No such user in this account.")

    revoked = get_session_store().revoke_all_for_user(target.id)
    return {"user_id": target.id, "sessions_revoked": revoked}
