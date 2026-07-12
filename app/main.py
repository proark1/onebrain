"""FastAPI application assembly."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.deps import get_pipeline, get_store, get_user_store
from app.deploy.runtime import validate_runtime_safety
from app.monitoring import record_api_error
from app.routers import assistant, auth, chat, conversations, documents, fleet, jobs, operator, platform, privacy, provisioning, rollouts, service, session
from app.seed import seed_if_empty
from app.users.seed import seed_admin_from_env, seed_users_if_empty

STATIC_DIR = Path(__file__).parent / "static"


def _route_template(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "") or "unmatched"


def create_app() -> FastAPI:
    settings = get_settings()
    validate_runtime_safety(settings)

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
        try:
            response = await call_next(request)
        except Exception:
            record_api_error(route=_route_template(request), status_code=500)
            raise
        if response.status_code >= 500:
            record_api_error(route=_route_template(request), status_code=response.status_code)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.cookie_secure:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    app.include_router(auth.router)
    app.include_router(session.router)
    app.include_router(documents.router)
    app.include_router(jobs.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(platform.router)
    # The operator + provisioning control plane exposes cross-account state and can
    # spend money / create infrastructure. A pure customer-serving deployment sets
    # operator_console=false (and operator_mode=false) so neither surface is even
    # mounted; Mission Control (or a stack explicitly opting in) keeps them on.
    if settings.is_operator_surface:
        app.include_router(operator.router)
        app.include_router(provisioning.router)
        app.include_router(rollouts.router)
    app.include_router(privacy.router)
    app.include_router(assistant.router)
    app.include_router(service.service_router)
    app.include_router(service.keys_router)

    # Mission Control only: ingest fleet heartbeats and serve the fleet surface.
    if settings.operator_mode:
        # G1-1 rotation interlock (fail-fast): when MC is configured to sign desired-
        # state, the active wrapper public key MUST be in the set delivered to boxes,
        # or a mis-ordered key rotation would strand the whole fleet at
        # envelope_signature_invalid (and permanently brick it if the set never regains
        # the active key). Refuse to boot on an excluding config rather than silently
        # signing with a key no box accepts. Inert when emission is off (no private
        # key) — the dormant MC and every non-signing operator instance skip it.
        from app.controlplane.desired_state import active_signer_in_served_set

        if not active_signer_in_served_set(settings):
            raise RuntimeError(
                "ONEBRAIN_FLEET_DESIRED_STATE_PRIVATE_KEY's derived public key is not in the "
                "served wrapper-key set (ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEYS / "
                "ONEBRAIN_FLEET_DESIRED_STATE_PUBLIC_KEY). This would strand the fleet at "
                "envelope_signature_invalid. Add the active signer's public key to the set "
                "before booting (see scripts/rotate_desired_state_key.py overlap-set)."
            )
        app.include_router(fleet.router)

        # G3-2 (P5-06): idempotent operator first-boot self-seed. Create MC's OWN
        # deployment row + a fleet key matching the baked ONEBRAIN_FLEET_KEY so the
        # reporter's self-heartbeat (start_reporter, below) authenticates without a manual
        # enroll. Runs BEFORE start_reporter; never fatal. No-op off operator_mode / on a
        # second boot (the row already exists).
        try:
            from app.controlplane.self_seed import seed_operator_self_deployment
            from app.deps import get_control_plane_store, get_fleet_store

            if seed_operator_self_deployment(settings, get_control_plane_store(), get_fleet_store()):
                logging.getLogger("onebrain").info("Operator self-seed complete (mc deployment + fleet key).")
        except Exception as exc:  # never fatal
            logging.getLogger("onebrain").warning("Operator self-seed skipped: %s", exc)

        try:
            from app.fleet.retention import start_fleet_retention

            start_fleet_retention(settings)
        except Exception as exc:  # never fatal
            logging.getLogger("onebrain").warning("Fleet retention not started: %s", exc)

        # Pull-path reconcile daemon (P5-04). OFF by default (fleet_reconcile_seconds=0,
        # G3-4) — starts only when the operator explicitly opts in with a positive
        # interval, so landing this does NOT flip auto-advance on the dormant MC. Never
        # fatal; each tick's failure is isolated inside reconcile_once.
        try:
            from app.controlplane.reconcile_scheduler import start_reconcile_scheduler

            if start_reconcile_scheduler(settings):
                logging.getLogger("onebrain").info("Fleet reconcile scheduler enabled.")
        except Exception as exc:  # never fatal
            logging.getLogger("onebrain").warning("Fleet reconcile scheduler not started: %s", exc)

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

    # A deployment configured with a Mission Control URL + fleet key reports its
    # own metadata-only heartbeat on a timer. Never fatal — a reporting failure
    # must not disturb serving.
    try:
        from app.fleet.reporter import start_reporter

        if start_reporter(settings):
            logging.getLogger("onebrain").info("Fleet reporter enabled.")
    except Exception as exc:
        logging.getLogger("onebrain").warning("Fleet reporter not started: %s", exc)

    if settings.legacy_static_ui_enabled:
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        admin_ui_url = settings.admin_ui_url.strip()
        if admin_ui_url:
            return RedirectResponse(admin_ui_url, status_code=307)
        return {
            "service": "onebrain-api",
            "status": "ok",
            "ui": "nextjs",
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
