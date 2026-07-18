from __future__ import annotations

import pytest

from app.ingest.extract import (
    ExtractionLimits,
    UnsupportedDocumentError,
    extract_text,
    supports_filename,
)


def test_extraction_is_an_explicit_allowlist_without_binary_text_fallback():
    assert supports_filename("handbook.txt")
    assert supports_filename("deck.PPTX")
    assert not supports_filename("installer.exe")
    assert not supports_filename("archive.zip")

    with pytest.raises(UnsupportedDocumentError, match="unsupported"):
        extract_text("installer.exe", b"MZ arbitrary binary")
    with pytest.raises(UnsupportedDocumentError, match="Binary"):
        extract_text("forged.txt", b"text\x00binary")


@pytest.mark.parametrize(
    "filename,payload,match",
    [
        ("forged.pdf", b"not a pdf", "PDF"),
        ("forged.docx", b"not a zip", "Office"),
        ("forged.xlsx", b"not a zip", "Office"),
        ("forged.pptx", b"not a zip", "Office"),
        ("forged.rtf", b"not rich text", "RTF"),
    ],
)
def test_extension_content_mismatches_fail_closed(filename, payload, match):
    with pytest.raises(UnsupportedDocumentError, match=match):
        extract_text(filename, payload)


def test_source_and_extracted_text_limits_are_checked_before_indexing():
    with pytest.raises(ValueError, match="source-size"):
        extract_text("large.txt", b"1234", ExtractionLimits(max_source_bytes=3))
    with pytest.raises(ValueError, match="extracted-text"):
        extract_text("large.txt", b"1234", ExtractionLimits(max_extracted_characters=3))
