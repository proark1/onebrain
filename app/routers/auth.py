"""Login / logout — issues and clears the signed session cookie."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.auth.passwords import DUMMY_HASH, verify_password
from app.auth.principal import SESSION_COOKIE
from app.auth.tokens import make_token
from app.config import get_settings
from app.deps import get_login_throttle, get_user_store
from app.monitoring import record_auth_failure
from app.schemas import LoginRequest

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(body: LoginRequest, response: Response):
    settings = get_settings()
    throttle = get_login_throttle()
    key = "email:" + body.email.strip().lower()

    # Lock out repeated failures for this account before touching the password.
    wait = throttle.retry_after(key)
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
        throttle.record_failure(key)
        record_auth_failure("login_invalid")
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    throttle.record_success(key)
    ttl = settings.session_days * 86400
    token = make_token(user.id, settings.auth_secret, ttl)
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=ttl, httponly=True, samesite="lax",
        secure=settings.cookie_secure, path="/",
    )
    return {"email": user.email, "display_name": user.display_name, "role_id": user.role_id}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}
