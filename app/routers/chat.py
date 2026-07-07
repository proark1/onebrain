"""Chat endpoint — streams the answer as server-sent events."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth.principal import Principal, resolve_principal
from app.deps import get_retrieval_service
from app.schemas import AskRequest

router = APIRouter(prefix="/api", tags=["chat"])


@router.post("/ask")
def ask(body: AskRequest, principal: Principal = Depends(resolve_principal)):
    service = get_retrieval_service()

    def event_stream():
        for event in service.answer_stream(principal, body.question):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
