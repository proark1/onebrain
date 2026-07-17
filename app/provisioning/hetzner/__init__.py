"""Hetzner P1 provisioner seam (Phase 4).

The token-isolating broker (`broker.py`) + the transport-agnostic `HetznerClient`
Protocol (`client.py`) with a real stdlib-urllib implementation (`urllib_client.py`)
and an in-memory `FakeHetznerClient` (`fake.py`), plus the pure render layer
(`render.py`) and the `HetznerProvisioner` executor (`provisioner.py`).

Everything here is dormant until `provisioner_backend="hetzner"` is selected and a
token is configured; nothing changes any retired workflow path. No module
here makes a live API call at import time — the real client only talks to
api.hetzner.cloud when explicitly invoked (Phase 5), and every Phase-4 test runs
against the fake."""
