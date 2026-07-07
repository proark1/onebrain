"""Per-tier LLM routing: sensitive context must go to the sovereign endpoint,
and fail closed when one isn't configured. Classification ints: PUBLIC=0,
INTERNAL=1, CONFIDENTIAL=2, RESTRICTED=3.
"""

from __future__ import annotations

from app.llm.tiered import TieredLLM
from app.security.policy import Classification
from app.store.base import Chunk, Hit


class _Fake:
    def __init__(self, tag: str):
        self.name = tag
        self._tag = tag

    def stream(self, question, hits, tenant_id="nft_gym", stats=None, history=None):
        yield self._tag


def _hit(classification: int) -> Hit:
    return Hit(chunk=Chunk(id="c", doc_id="d", text="x", meta={"classification": classification}), score=1.0)


def _run(llm, hits) -> str:
    return "".join(llm.stream("q", hits))


def _tiered(sovereign=_Fake("SOV"), require=True):
    return TieredLLM(_Fake("DEFAULT"), sovereign, Classification.CONFIDENTIAL, require_sovereign=require)


def test_public_and_internal_use_default():
    t = _tiered()
    assert _run(t, [_hit(0)]) == "DEFAULT"          # PUBLIC
    assert _run(t, [_hit(1)]) == "DEFAULT"          # INTERNAL
    assert _run(t, []) == "DEFAULT"                 # no accessible context


def test_confidential_and_restricted_route_sovereign():
    t = _tiered()
    assert _run(t, [_hit(2)]) == "SOV"              # CONFIDENTIAL
    assert _run(t, [_hit(3)]) == "SOV"              # RESTRICTED
    assert _run(t, [_hit(0), _hit(3)]) == "SOV"     # mixed -> highest wins


def test_fail_closed_when_sensitive_and_no_sovereign():
    t = _tiered(sovereign=None, require=True)
    out = _run(t, [_hit(3)])
    assert "DEFAULT" not in out and "sovereign" in out.lower()


def test_optional_fallback_when_not_required():
    t = _tiered(sovereign=None, require=False)
    assert _run(t, [_hit(3)]) == "DEFAULT"          # falls back when not required


def test_missing_label_is_treated_as_restricted():
    t = _tiered()
    h = Hit(chunk=Chunk(id="c", doc_id="d", text="x", meta={}), score=1.0)  # no classification
    assert _run(t, [h]) == "SOV"                     # fail closed -> sovereign
