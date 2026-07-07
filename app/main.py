"""FastAPI application assembly."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.deps import get_pipeline, get_store, get_user_store
from app.routers import auth, chat, conversations, documents, operator, platform, service, session
from app.seed import seed_if_empty
from app.users.seed import seed_admin_from_env, seed_users_if_empty

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()

    # Fail closed: a weak/default cookie-signing secret means anyone can forge a
    # session token for any user. Refuse to start rather than run insecurely.
    if settings.auth_secret == "dev-insecure-change-me" or len(settings.auth_secret) < 32:
        raise RuntimeError(
            "ONEBRAIN_AUTH_SECRET must be a strong random secret (>=32 chars). "
            'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
        )

    app = FastAPI(title="onebrain", version="0.1.0")

    @app.middleware("http")
    async def _harden(request, call_next):
        # Cheap edge guards: reject oversized bodies, set conservative security headers.
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    app.include_router(auth.router)
    app.include_router(session.router)
    app.include_router(documents.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(platform.router)
    app.include_router(operator.router)
    app.include_router(service.service_router)
    app.include_router(service.keys_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "chunks": get_store().count()}

    if settings.seed_sample_data:
        try:
            seed_if_empty(get_pipeline(), get_store())
        except Exception as exc:  # a provider/key misconfig shouldn't brick startup
            logging.getLogger("onebrain").warning("Sample-data seeding skipped: %s", exc)

    # Shared-password demo accounts seed ONLY on a fully-local stack (or explicit
    # opt-in) — on a real deployment they would be a standing credential exposure.
    if settings.seed_demo_users and (settings.is_local_stack or settings.allow_demo_users):
        try:
            seed_users_if_empty(get_user_store())
        except Exception as exc:
            logging.getLogger("onebrain").warning("Demo-user seeding skipped: %s", exc)

    # The safe login path on any stack: a real admin from ONEBRAIN_ADMIN_*.
    try:
        if seed_admin_from_env(get_user_store(), settings):
            logging.getLogger("onebrain").info("Admin account bootstrapped from ONEBRAIN_ADMIN_*.")
    except Exception as exc:
        logging.getLogger("onebrain").warning("Admin bootstrap skipped: %s", exc)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
