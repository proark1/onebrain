"""Paragraph-aware character chunking with overlap.

Small and predictable: pack whole paragraphs up to `size`, and only hard-split a
paragraph that is itself larger than a chunk. Overlap keeps context across the
boundary so retrieval doesn't lose a sentence that straddles two chunks.
"""

from __future__ import annotations

from typing import List


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> List[str]:
    text = text.strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buffer = ""

    for para in paragraphs:
        if len(buffer) + len(para) + 2 <= size:
            buffer = f"{buffer}\n\n{para}".strip()
        else:
            if buffer:
                chunks.append(buffer)
            if len(para) <= size:
                buffer = para
            else:
                chunks.extend(_split_long(para, size, overlap))
                buffer = ""
    if buffer:
        chunks.append(buffer)
    return chunks


def _split_long(para: str, size: int, overlap: int) -> List[str]:
    out: List[str] = []
    start = 0
    while start < len(para):
        out.append(para[start:start + size])
        start += size - overlap
    return out
