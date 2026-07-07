"""Extraction of Office / RTF / text formats (OCR paths need Tesseract, skipped)."""

from __future__ import annotations

import io

import pytest

from app.ingest.extract import extract_text


def test_plain_text_and_csv():
    assert "hello" in extract_text("a.txt", b"hello world")
    assert "munich" in extract_text("data.csv", b"city,hours\nmunich,06:00").lower()


def test_docx():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("NFT Gym membership is 49 EUR per month.")
    table = d.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Munich"
    table.rows[0].cells[1].text = "06:00-23:00"
    buf = io.BytesIO()
    d.save(buf)
    text = extract_text("plan.docx", buf.getvalue())
    assert "membership is 49 EUR" in text
    assert "Munich" in text and "06:00-23:00" in text


def test_xlsx():
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["location", "revenue"])
    ws.append(["Munich", 214000])
    buf = io.BytesIO()
    wb.save(buf)
    text = extract_text("q1.xlsx", buf.getvalue())
    assert "revenue" in text and "Munich" in text and "214000" in text


def test_pptx():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Q1 Marketing Plan"
    buf = io.BytesIO()
    prs.save(buf)
    text = extract_text("deck.pptx", buf.getvalue())
    assert "Q1 Marketing Plan" in text


def test_rtf():
    pytest.importorskip("striprtf")
    rtf = rb"{\rtf1\ansi Trainer salary is 3000 EUR.\par}"
    assert "Trainer salary is 3000 EUR" in extract_text("note.rtf", rtf)
