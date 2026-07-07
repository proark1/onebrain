"""Route each answer to an LLM by the sensitivity of its retrieved context.

PUBLIC/INTERNAL answers may use the default (cheap, easy-to-swap) model; the
moment a CONFIDENTIAL/RESTRICTED chunk is in the context, the answer is routed to
an EU-sovereign endpoint instead. This closes the Schrems II footgun where a
single env var pointing the whole app at a US model would silently ship internal
data outside the EU — the decision is made per request, in owned code, from the
SAME classification labels AccessFilter enforces, so routing can never disagree
with access. Fails closed: if sensitive data would otherwise use a non-sovereign
model, it refuses rather than leak.
"""

from __future__ import annotations

from typing import Iterator, List

from app.security.policy import Classification
from app.store.base import Hit


class TieredLLM:
    name = "tiered"

    def __init__(self, default_llm, sovereign_llm, threshold: Classification, require_sovereign: bool):
        self._default = default_llm
        self._sovereign = sovereign_llm            # may be None (not configured)
        self._threshold = threshold                # min classification that must route sovereign
        self._require_sovereign = require_sovereign

    @staticmethod
    def _max_classification(hits: List[Hit]) -> Classification:
        highest = Classification.PUBLIC
        for h in hits:
            # A missing/unknown label is treated as RESTRICTED (fail closed),
            # exactly like AccessFilter — an unlabelled chunk never routes cheap.
            c = Classification.parse(h.chunk.meta.get("classification", Classification.RESTRICTED))
            if c > highest:
                highest = c
        return highest

    def stream(self, question, hits, tenant_id="nft_gym", stats=None, history=None) -> Iterator[str]:
        if self._max_classification(hits) >= self._threshold:
            if self._sovereign is not None:
                yield from self._sovereign.stream(question, hits, tenant_id, stats, history)
                return
            if self._require_sovereign:
                yield (
                    "I can't answer that here: it draws on data that must be processed on "
                    "EU-sovereign infrastructure, which isn't configured for this deployment. "
                    "Please contact an administrator."
                )
                return
        yield from self._default.stream(question, hits, tenant_id, stats, history)
