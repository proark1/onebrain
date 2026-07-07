"""No-API answer synthesiser: extractive summary of the retrieved context.

It proves the retrieval + permission path end-to-end without spending a token —
if nothing was retrieved (e.g. the caller isn't allowed to see it), it says so.
Swap in `LiteLLMLLM` for fluent generation in production.
"""

from __future__ import annotations

import re
from typing import Iterator, List

from app.store.base import Hit


def _sentences(text: str) -> List[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


# Common words that shouldn't count as a topical match between question and text.
_STOP = frozenset(
    "a an the is are was were be been of to in on for and or i you do does did how "
    "what when where which who with at by as it its their my me we they this that "
    "these those from about can could would should will may might get have has had "
    "please tell show me our your all any".split()
)


def _terms(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP}


class LocalLLM:
    name = "local-extractive"

    model = "local-extractive"  # used for (free) cost lookup

    def stream(self, question, hits, tenant_id="nft_gym", stats=None, history=None):
        if not hits:
            yield ("I couldn't find anything you have access to about that. "
                   "It may be restricted to another role, scoped to another location, "
                   "or simply not uploaded yet.")
            return

        q_terms = _terms(question)
        scored = []
        for idx, hit in enumerate(hits, 1):
            for sentence in _sentences(hit.chunk.text):
                overlap = len(q_terms & _terms(sentence))
                scored.append((overlap, idx, sentence))
        scored.sort(key=lambda item: -item[0])
        relevant = [s for s in scored if s[0] > 0]

        # Only answer when the accessible context actually addresses the question.
        # (This is the offline fallback; a real model handles phrasing gracefully.)
        if not relevant:
            yield ("I couldn't find anything relevant in the documents you can access "
                   "about that. It may be restricted to another role, scoped to another "
                   "location, or simply not uploaded yet.")
            return

        yield "Here's what I found in the documents you can access:\n\n"
        for _, idx, sentence in relevant[:3]:
            for word in f"• {sentence} [{idx}]\n".split(" "):
                yield word + " "
