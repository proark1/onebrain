"""Vision invoice extractor — pure-vision, no OCR (plan §5).

The image (or each rasterised PDF page) goes straight to a multimodal model with
the §14 JSON schema; there is deliberately no OCR fallback, so the data-residency
lever is the *model choice*. This path therefore enforces the sovereign-routing
rule itself (mirroring ``app.llm.tiered.TieredLLM``): a confidential invoice must
run on the EU-sovereign model when one is required, and fails **closed** — a
visible error, never a silent downgrade — rather than shipping the image to a
non-sovereign endpoint.

Two implementations:
- ``LiteLLMInvoiceExtractor`` — real path, reuses the AI-employee LiteLLM backend
  (its ``response_format`` json_schema plumbing already exists) with an image
  message the backend forwards untouched.
- ``FakeInvoiceExtractor`` — deterministic, for tests and offline boxes; never
  touches a model.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Callable, Optional, Protocol, Union

from app.accounting.extraction_schema import (
    EXTRACTION_SYSTEM_PROMPT,
    EXTRACTION_USER_PROMPT,
    INVOICE_JSON_SCHEMA,
    parse_invoice_json,
)
from app.accounting.model import ExtractedInvoice
from app.ai_employees.backends.base import AgentBackendRequest
from app.security.policy import Classification


logger = logging.getLogger(__name__)

# Invoices are short; cap pages/pixels/bytes so a hostile PDF can't exhaust memory.
MAX_PAGES = 12
MAX_IMAGE_PIXELS = 40_000_000
MAX_SOURCE_BYTES = 25 * 1024 * 1024
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp")

# Invoices are confidential by policy — the extractor always routes as such,
# independent of whatever classification the Drive file happens to carry.
ACCOUNTING_CLASSIFICATION = "confidential"


class InvoiceExtractionError(RuntimeError):
    """Extraction was attempted but failed (bad file, model error, unparseable)."""


class InvoiceExtractorUnavailable(InvoiceExtractionError):
    """No usable model is configured (provider off, or sovereign required + absent).

    Distinct from a per-document failure: the service surfaces this as "extraction
    is switched off / needs configuration", not as a broken document.
    """


class InvoiceExtractor(Protocol):
    available: bool
    unavailable_reason: str

    def extract(
        self, *, content: bytes, media_type: str, filename: str = "",
        classification: str = ACCOUNTING_CLASSIFICATION,
    ) -> ExtractedInvoice: ...


def _rasterize_pdf(content: bytes) -> list[tuple[str, bytes]]:
    try:
        import fitz  # PyMuPDF, already a dependency
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise InvoiceExtractorUnavailable("PDF rendering needs PyMuPDF.") from exc
    images: list[tuple[str, bytes]] = []
    with fitz.open(stream=content, filetype="pdf") as document:
        for index, page in enumerate(document):
            if index >= MAX_PAGES:
                break
            pixmap = page.get_pixmap(dpi=200)
            if pixmap.width * pixmap.height > MAX_IMAGE_PIXELS:
                pixmap = page.get_pixmap(dpi=110)
            images.append(("image/png", pixmap.tobytes("png")))
    if not images:
        raise InvoiceExtractionError("PDF had no rasterizable pages.")
    return images


def _to_images(content: bytes, media_type: str, filename: str) -> list[tuple[str, bytes]]:
    if not content:
        raise InvoiceExtractionError("Empty document.")
    if len(content) > MAX_SOURCE_BYTES:
        raise InvoiceExtractionError("Document exceeds the extraction size limit.")
    media = (media_type or "").lower()
    name = (filename or "").lower()
    if "pdf" in media or name.endswith(".pdf"):
        return _rasterize_pdf(content)
    if media.startswith("image/") or name.endswith(_IMAGE_EXTS):
        return [(media_type or "image/png", content)]
    raise InvoiceExtractionError(f"Unsupported document type for extraction: {media_type or filename}")


def _build_messages(images: list[tuple[str, bytes]]) -> tuple[dict, dict]:
    blocks: list[dict] = [{"type": "text", "text": EXTRACTION_USER_PROMPT}]
    for media_type, data in images:
        encoded = base64.b64encode(data).decode("ascii")
        blocks.append({
            "type": "image_url",
            "image_url": {"url": f"data:{media_type};base64,{encoded}"},
        })
    return (
        {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
        {"role": "user", "content": blocks},
    )


def _salvage_json(raw: str) -> dict:
    """Pull the first balanced JSON object out of a chatty response."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise InvoiceExtractionError("Extraction result contained no JSON object.")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise InvoiceExtractionError("Extraction result was not valid JSON.") from exc


class LiteLLMInvoiceExtractor:
    def __init__(self, settings, *, backend=None):
        self._settings = settings
        self._default_model = (
            getattr(settings, "invoice_recognition_model", "") or settings.litellm_model or ""
        ).strip()
        self._backend = backend  # injectable AgentBackend for tests
        self.available = bool(settings.llm_provider == "litellm" and self._default_model)
        self.unavailable_reason = "" if self.available else (
            "Invoice extraction needs a vision model: set ONEBRAIN_LLM_PROVIDER=litellm "
            "and a multimodal ONEBRAIN_LITELLM_MODEL (or ONEBRAIN_INVOICE_RECOGNITION_MODEL)."
        )

    def _resolve_model(self, classification: str) -> str:
        """Sovereign gate, fail-closed — mirrors TieredLLM.stream."""
        threshold = Classification.parse(self._settings.sovereign_min_tier)
        if Classification.parse(classification) >= threshold:
            sovereign = (self._settings.sovereign_llm_model or "").strip()
            if sovereign:
                return sovereign
            if self._settings.sovereign_required:
                raise InvoiceExtractorUnavailable(
                    "This invoice is confidential and must run on an EU-sovereign model, "
                    "which is not configured for this deployment."
                )
        return self._default_model

    def _resolve_backend(self):
        if self._backend is None:
            from app.ai_employees.backends.litellm import LiteLLMAgentBackend

            self._backend = LiteLLMAgentBackend("gemini", available=True)
        return self._backend

    def extract(
        self, *, content: bytes, media_type: str, filename: str = "",
        classification: str = ACCOUNTING_CLASSIFICATION,
    ) -> ExtractedInvoice:
        if not self.available:
            raise InvoiceExtractorUnavailable(self.unavailable_reason)
        model = self._resolve_model(classification)  # may raise InvoiceExtractorUnavailable
        messages = _build_messages(_to_images(content, media_type, filename))
        request = AgentBackendRequest(
            model=model,
            messages=messages,
            max_output_tokens=8192,
            response_schema=INVOICE_JSON_SCHEMA,
            temperature=0.0,
            timeout_seconds=120.0,
        )
        chunks: list[str] = []
        try:
            for event in self._resolve_backend().stream(request):
                if event.type == "text":
                    chunks.append(event.text)
                elif event.type == "error":
                    raise InvoiceExtractionError(event.text or "Model returned an error.")
        except InvoiceExtractionError:
            raise
        except Exception as exc:  # network/provider failure — fail visibly, never swallow
            raise InvoiceExtractionError(f"Invoice extraction call failed: {exc}") from exc
        raw = "".join(chunks).strip()
        if not raw:
            raise InvoiceExtractionError("Model returned an empty extraction.")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = _salvage_json(raw)
        return parse_invoice_json(payload)


class FakeInvoiceExtractor:
    """Deterministic extractor for tests/offline use. Never calls a model."""

    available = True
    unavailable_reason = ""

    def __init__(
        self,
        result: Union[ExtractedInvoice, Callable[..., ExtractedInvoice]],
        *,
        available: bool = True,
        unavailable_reason: str = "",
    ):
        self._result = result
        self.available = available
        self.unavailable_reason = unavailable_reason

    def extract(
        self, *, content: bytes, media_type: str, filename: str = "",
        classification: str = ACCOUNTING_CLASSIFICATION,
    ) -> ExtractedInvoice:
        if not self.available:
            raise InvoiceExtractorUnavailable(self.unavailable_reason or "Extractor unavailable.")
        if callable(self._result):
            return self._result(
                content=content, media_type=media_type,
                filename=filename, classification=classification,
            )
        return self._result


def build_invoice_extractor(settings, *, backend=None) -> InvoiceExtractor:
    return LiteLLMInvoiceExtractor(settings, backend=backend)
