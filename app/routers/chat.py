"""Chat endpoint — streams the answer as SSE and persists the conversation."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.auth.principal import Principal, resolve_principal
from app.deps import get_conversation_store, get_platform_store, get_retrieval_service
from app.platform.scope import scoped_human_principal
from app.routers.conversations import scope_of
from app.schemas import AskRequest

router = APIRouter(prefix="/api", tags=["chat"])

HISTORY_TURNS = 6  # how many prior messages to feed back as context


def _title(question: str) -> str:
    words = question.strip().split()
    title = " ".join(words[:8])
    return (title + "…") if len(words) > 8 else title


@router.post("/ask")
def ask(body: AskRequest, principal: Principal = Depends(resolve_principal)):
    principal = scoped_human_principal(body.account_id or "", body.space_id or "", principal, get_platform_store())
    service = get_retrieval_service()
    convs = get_conversation_store()
    scope = scope_of(principal)

    conv = convs.get(body.conversation_id, scope) if body.conversation_id else None
    if conv is None:
        conv = convs.create(scope, _title(body.question))

    # Load prior turns BEFORE recording the new question.
    history = [{"role": m.role, "content": m.content}
               for m in convs.get_messages(conv.id, limit=HISTORY_TURNS)]
    convs.add_message(conv.id, "user", body.question)

    def event_stream():
        yield f"data: {json.dumps({'type': 'conversation', 'id': conv.id, 'title': conv.title})}\n\n"

        answer_parts: list[str] = []
        sources: list = []
        meta: dict = {}
        for event in service.answer_stream(principal, body.question, history=history):
            if event["type"] == "token":
                answer_parts.append(event["text"])
            elif event["type"] == "sources":
                sources = event["sources"]
            elif event["type"] == "meta":
                meta = event
            yield f"data: {json.dumps(event)}\n\n"

        convs.add_message(conv.id, "assistant", "".join(answer_parts), meta={
            "sources": sources,
            "chunks_used": meta.get("chunks_used"),
            "total_tokens": meta.get("total_tokens"),
            "cost_usd": meta.get("cost_usd"),
            "estimated": meta.get("estimated"),
            "llm": meta.get("llm"),
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
