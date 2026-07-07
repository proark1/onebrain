"""Retrieval respects the boundary: the model can't be handed forbidden chunks."""

from __future__ import annotations

from tests.conftest import principal_for


def categories(hits):
    return {h.chunk.meta["category"] for h in hits}


def test_front_desk_asking_about_salaries_gets_no_hr_chunks(service):
    hits = service.retrieve(principal_for("front_desk"), "What are the trainer salary bands?")
    assert "hr" not in categories(hits)  # restricted HR content is never retrieved


def test_hr_asking_about_salaries_does_get_hr_chunks(service):
    hits = service.retrieve(principal_for("hr"), "What are the trainer salary bands?")
    assert any(h.chunk.meta["category"] == "hr" for h in hits)


def test_public_gets_public_answer(service):
    hits = service.retrieve(principal_for("public"), "What are the opening hours?")
    assert hits
    assert all(h.chunk.meta["classification_label"] == "public" for h in hits)


def test_answer_stream_reports_efficiency_meta(service):
    events = list(service.answer_stream(principal_for("hr"), "What are the salary bands?"))
    meta = next(e for e in events if e["type"] == "meta")
    assert meta["chunks_used"] <= 8          # bounded context, not the whole corpus
    assert events[-1]["type"] == "done"


def test_front_desk_salary_question_answers_with_no_access_message(service):
    events = list(service.answer_stream(principal_for("front_desk"), "trainer salary bands"))
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert "access" in answer.lower()
