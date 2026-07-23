"""FastAPI application assembly."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import get_settings
from app.deps import (
    get_pipeline,
    get_platform_store,
    get_service_key_store,
    get_session_store,
    get_store,
    get_user_store,
)
from app.deploy.runtime import validate_embedding_runtime_contract, validate_runtime_safety
from app.http_limits import RequestBodyTooLargeError, limited_receive, request_body_limit
from app.monitoring import record_api_error
from app.provisioning.customer_bootstrap import (
    decode_customer_bootstrap,
    reconcile_customer_bootstrap,
)
from app.routers import accounting, ai_employees, assistant, auth, chat, conversations, documents, drive, fleet, jobs, kpis, operator, platform, privacy, provisioning, rollouts, service, session, user_management
from app.seed import seed_if_empty
from app.users.seed import seed_admin_from_env, seed_users_if_empty


def _route_template(request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", "") or "unmatched"


def create_app() -> FastAPI:
    settings = get_settings()
    customer_bootstrap = decode_customer_bootstrap(settings.customer_bootstrap)
    if customer_bootstrap and settings.operator_mode:
        raise RuntimeError("Customer bootstrap cannot run on Mission Control.")
    validate_runtime_safety(settings)
    validate_embedding_runtime_contract(settings)
    settings.assert_production_mission_control_ready()

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
        body_limit = request_body_limit(
            request.method,
            request.url.path,
            default_bytes=settings.max_body_bytes,
            drive_file_bytes=settings.drive_max_file_bytes,
        )
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > body_limit:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
        request._receive = limited_receive(request._receive, max_body_bytes=body_limit)
        try:
            response = await call_next(request)
        except RequestBodyTooLargeError:
            return JSONResponse({"detail": "Payload too large"}, status_code=413)
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
    # Drive is a standard, always-mounted customer-suite capability. Mission
    # Control remains a fleet control plane and deliberately receives no file
    # surface or customer-original storage path.
    if not settings.operator_mode:
        app.include_router(drive.router)
    app.include_router(jobs.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)
    app.include_router(platform.router)
    app.include_router(kpis.router)
    app.include_router(ai_employees.router)
    # Accounting (Buchhaltung) is an optional per-workspace product on the same
    # template as KPI Dashboard / AI Employees: no separate container, mounted
    # unconditionally, and gated to 403 by its AppInstallation when not enabled.
    app.include_router(accounting.router)
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
        app.include_router(user_management.agent_router)
        app.include_router(user_management.operator_router)

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

        # Metadata-only fleet alerts (heartbeat, version drift, and host
        # capacity). The watchdog never dispatches or changes deployments; it
        # can be disabled explicitly with ONEBRAIN_FLEET_WATCHDOG_SECONDS=0.
        try:
            from app.fleet.watchdog_scheduler import start_fleet_watchdog

            if start_fleet_watchdog(settings):
                logging.getLogger("onebrain").info("Fleet watchdog enabled.")
        except Exception as exc:  # never fatal
            logging.getLogger("onebrain").warning("Fleet watchdog not started: %s", exc)

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
        if seed_admin_from_env(
            get_user_store(), settings,
            tenant=customer_bootstrap.account_id if customer_bootstrap else "nft_gym",
        ):
            logging.getLogger("onebrain").info("Admin account bootstrapped from ONEBRAIN_ADMIN_*.")
    except Exception as exc:
        if customer_bootstrap:
            raise
        logging.getLogger("onebrain").warning("Admin bootstrap skipped: %s", exc)

    if customer_bootstrap:
        result = reconcile_customer_bootstrap(
            customer_bootstrap,
            platform_store=get_platform_store(),
            service_key_store=get_service_key_store(),
            user_store=get_user_store(),
            session_store=get_session_store(),
            administrator_email=settings.admin_email,
            integration_keys={
                "assistant": settings.assistant_service_key,
                "communication": settings.communication_service_key,
            },
        )
        logging.getLogger("onebrain").info(
            "Customer bootstrap reconciled account=%s spaces=%s apps=%s integration_keys=%s.",
            result.account_id, result.spaces, result.apps, result.integration_keys,
        )

    # A deployment configured with a Mission Control URL + fleet key reports its
    # own metadata-only heartbeat on a timer. Never fatal — a reporting failure
    # must not disturb serving.
    try:
        from app.fleet.reporter import start_reporter

        if settings.fleet_reporter_enabled and start_reporter(settings):
            logging.getLogger("onebrain").info("Fleet reporter enabled.")
    except Exception as exc:
        logging.getLogger("onebrain").warning("Fleet reporter not started: %s", exc)

    # Data-retention sweep. OFF by default (retention_sweep_seconds=0): a
    # configured policy is only enforced once an operator sets a positive
    # interval, so landing this cannot start deleting customer records on a
    # deploy. No-op on Mission Control, which stores no customer content.
    try:
        from app.retention.scheduler import start_retention_scheduler

        if start_retention_scheduler(settings):
            logging.getLogger("onebrain").info("Retention sweep scheduler enabled.")
    except Exception as exc:  # never fatal
        logging.getLogger("onebrain").warning("Retention sweep scheduler not started: %s", exc)

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
