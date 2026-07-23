"""Vision extractor: JSON parse, multimodal message, sovereign gate, availability."""

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.accounting.extraction import (
    FakeInvoiceExtractor,
    InvoiceExtractionError,
    InvoiceExtractorUnavailable,
    LiteLLMInvoiceExtractor,
    _build_messages,
    _to_images,
    build_invoice_extractor,
)
from app.accounting.model import ExtractedInvoice
from app.ai_employees.backends.base import BackendEvent


class _FakeBackend:
    def __init__(self, events):
        self._events = events
        self.captured = None

    def stream(self, request):
        self.captured = request
        yield from self._events


def _settings(**overrides):
    base = dict(
        llm_provider="litellm",
        litellm_model="gemini/gemini-2.5-flash",
        invoice_recognition_model="",
        sovereign_min_tier="confidential",
        sovereign_llm_model="",
        sovereign_required=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _text_events(payload):
    return [BackendEvent(type="text", text=json.dumps(payload)), BackendEvent(type="done")]


def test_extractor_parses_backend_json_and_sends_an_image_block():
    payload = {
        "issuer_name": "ACME GmbH", "invoice_number": "R1",
        "total_net": "100.00", "total_tax": "19.00", "total_gross": "119.00",
    }
    backend = _FakeBackend(_text_events(payload))
    extractor = LiteLLMInvoiceExtractor(_settings(), backend=backend)
    invoice = extractor.extract(content=b"\x89PNGfake", media_type="image/png", filename="r.png")

    assert invoice.issuer_name == "ACME GmbH"
    assert invoice.total_gross == Decimal("119.00")
    # A multimodal user message with a base64 data-URI image block was sent.
    user_message = backend.captured.messages[1]
    assert user_message["role"] == "user"
    blocks = user_message["content"]
    assert any(block["type"] == "image_url" for block in blocks)
    assert any(block["type"] == "text" for block in blocks)
    assert backend.captured.response_schema  # json_schema requested


def test_confidential_without_sovereign_model_fails_closed():
    extractor = LiteLLMInvoiceExtractor(_settings(sovereign_required=True), backend=_FakeBackend([]))
    with pytest.raises(InvoiceExtractorUnavailable):
        extractor.extract(content=b"x", media_type="image/png", classification="confidential")


def test_confidential_routes_to_the_sovereign_model():
    extractor = LiteLLMInvoiceExtractor(
        _settings(sovereign_llm_model="mistral/mistral-small-latest"), backend=_FakeBackend([]),
    )
    assert extractor._resolve_model("confidential") == "mistral/mistral-small-latest"
    # Public data may use the cheap default.
    assert extractor._resolve_model("public") == "gemini/gemini-2.5-flash"


def test_local_provider_is_visibly_unavailable():
    extractor = LiteLLMInvoiceExtractor(_settings(llm_provider="local"), backend=_FakeBackend([]))
    assert extractor.available is False
    assert extractor.unavailable_reason
    with pytest.raises(InvoiceExtractorUnavailable):
        extractor.extract(content=b"x", media_type="image/png")


def test_backend_error_event_raises_extraction_error():
    backend = _FakeBackend([BackendEvent(type="error", text="model exploded")])
    extractor = LiteLLMInvoiceExtractor(_settings(), backend=backend)
    with pytest.raises(InvoiceExtractionError):
        extractor.extract(content=b"x", media_type="image/png")


def test_salvages_json_from_a_chatty_response():
    backend = _FakeBackend([
        BackendEvent(type="text", text='Sure: {"issuer_name": "X"} — hope that helps'),
        BackendEvent(type="done"),
    ])
    extractor = LiteLLMInvoiceExtractor(_settings(), backend=backend)
    assert extractor.extract(content=b"x", media_type="image/png").issuer_name == "X"


def test_unsupported_media_type_is_rejected():
    with pytest.raises(InvoiceExtractionError):
        _to_images(b"data", "application/zip", "archive.zip")


def test_build_messages_encodes_a_data_uri():
    _system, user = _build_messages([("image/png", b"abc")])
    image_block = [block for block in user["content"] if block["type"] == "image_url"][0]
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


def test_fake_extractor_returns_canned_result():
    canned = ExtractedInvoice(issuer_name="Canned Co")
    assert FakeInvoiceExtractor(canned).extract(content=b"", media_type="image/png").issuer_name == "Canned Co"


def test_build_invoice_extractor_factory():
    assert isinstance(build_invoice_extractor(_settings()), LiteLLMInvoiceExtractor)
