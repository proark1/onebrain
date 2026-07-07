"""FastAPI application assembly."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.deps import get_pipeline, get_store
from app.routers import chat, conversations, documents, session
from app.seed import seed_if_empty

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="onebrain", version="0.1.0")

    app.include_router(session.router)
    app.include_router(documents.router)
    app.include_router(conversations.router)
    app.include_router(chat.router)

    @app.get("/health")
    def health():
        return {"status": "ok", "chunks": get_store().count()}

    if settings.seed_sample_data:
        try:
            seed_if_empty(get_pipeline(), get_store())
        except Exception as exc:  # a provider/key misconfig shouldn't brick startup
            logging.getLogger("onebrain").warning("Sample-data seeding skipped: %s", exc)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
