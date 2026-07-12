"""P5-07 7a: pin the registry-allowlist default. `release_registry_allowlist`
defaults to an ORG-scoped prefix ("ghcr.io/proark1"), NOT a bare host — a bare
"ghcr.io" would allowlist every GHCR tenant, making the compromised-signed-image
backstop porous. This test pins that security default against a future loosening
edit; there is no runtime change.
"""

from __future__ import annotations

from app.config import Settings
from app.trust.release import parse_registry_allowlist, verify_images

_DIGEST = "@sha256:" + "a" * 64


def test_registry_allowlist_default_is_org_prefixed():
    default = Settings().release_registry_allowlist
    parsed = parse_registry_allowlist(default)

    # Exactly one entry, and it is ORG-scoped (contains a '/') — never a bare host that
    # would allowlist every tenant on a multi-tenant registry.
    assert len(parsed) == 1
    (entry,) = tuple(parsed)
    assert "/" in entry, f"default allowlist entry {entry!r} is a bare host (allowlists every tenant)"

    # An image under the allowlisted org verifies...
    assert verify_images({"onebrain-api": f"{entry}/onebrain-api{_DIGEST}"}, parsed) == []
    # ...but a SAME-HOST, DIFFERENT-ORG ref is rejected under the default (the org boundary bites).
    rejected = verify_images({"onebrain-api": f"ghcr.io/someone-else/img{_DIGEST}"}, parsed)
    assert rejected != []
