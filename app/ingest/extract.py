"""Text extraction from uploaded files.

Supported: PDF (with OCR fallback for scanned pages), Word (.docx), Excel
(.xlsx/.xlsm), PowerPoint (.pptx), RTF, images via OCR (.png/.jpg/...), and any
plain-text format (.txt/.md/.csv/.json/.html/...). Each format's library is
imported lazily and a missing one raises a clear RuntimeError (-> 422), not a 500.
"""

from __future__ import annotations

import io

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp")


def extract_text(filename: str, data: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        return _pdf(data)
    if name.endswith(".docx"):
        return _docx(data)
    if name.endswith((".xlsx", ".xlsm")):
        return _xlsx(data)
    if name.endswith(".pptx"):
        return _pptx(data)
    if name.endswith(".rtf"):
        return _rtf(data)
    if name.endswith(_IMAGE_EXTS):
        return _ocr_image(data)
    return _plain_text(data)


def _plain_text(data: bytes) -> str:
    for encoding in ("utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF support needs pypdf") from exc
    reader = PdfReader(io.BytesIO(data))
    text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) >= 20:
        return text
    ocr = _ocr_pdf(data)   # likely a scanned/image PDF — try OCR
    return ocr if ocr.strip() else text


def _docx(data: bytes) -> str:
    try:
        import docx
    except ImportError as exc:
        raise RuntimeError("Word (.docx) support needs python-docx") from exc
    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _xlsx(data: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise RuntimeError("Excel (.xlsx) support needs openpyxl") from exc
    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts: list[str] = []
    for sheet in workbook.worksheets:
        parts.append(f"# Sheet: {sheet.title}")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    workbook.close()
    return "\n".join(parts)


def _pptx(data: bytes) -> str:
    try:
        from pptx import Presentation
    except ImportError as exc:
        raise RuntimeError("PowerPoint (.pptx) support needs python-pptx") from exc
    presentation = Presentation(io.BytesIO(data))
    parts: list[str] = []
    for i, slide in enumerate(presentation.slides, 1):
        parts.append(f"# Slide {i}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    parts.append(text)
            if shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
    return "\n".join(parts)


def _rtf(data: bytes) -> str:
    try:
        from striprtf.striprtf import rtf_to_text
    except ImportError as exc:
        raise RuntimeError("RTF support needs striprtf") from exc
    return rtf_to_text(_plain_text(data))


def _ocr_image(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Image OCR needs pytesseract + Pillow") from exc
    try:
        return pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    except Exception as exc:  # e.g. Tesseract binary not installed
        raise RuntimeError(f"Image OCR failed (is Tesseract installed?): {exc}") from exc


def _ocr_pdf(data: bytes) -> str:
    """Render pages and OCR them — for scanned PDFs with no text layer.
    Best-effort: returns '' if the OCR toolchain isn't available."""
    try:
        import fitz  # PyMuPDF
        import pytesseract
        from PIL import Image
    except ImportError:
        return ""
    try:
        out = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                pixmap = page.get_pixmap(dpi=200)
                image = Image.open(io.BytesIO(pixmap.tobytes("png")))
                out.append(pytesseract.image_to_string(image))
        return "\n\n".join(out)
    except Exception:
        return ""
