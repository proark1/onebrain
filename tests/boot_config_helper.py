"""Shared helper for the box BOOT-CONFIG VALIDATION tests (MC + customer).

Reconstructs the Settings the onebrain-api process ACTUALLY boots with on a rendered
Hetzner box, by simulating docker-compose's env resolution:

  * the per-service ``env/onebrain-api.env`` file supplies the process env, but its secret
    values are ``${VAR}`` references (never plaintext);
  * compose interpolates each ``${VAR}`` from the box's ``/opt/onebrain/.env`` (the bundle
    dotenv the customer box FETCHES via /bootstrap, or the MC box BAKES via cloud-init) —
    the same mechanism that fills ONEBRAIN_ADMIN_PASSWORD / POSTGRES_PASSWORD.

Resolving those refs against the real ``.env`` and rebuilding a Settings is the only way to
prove the box actually satisfies the app's OWN boot requirements (the app/main.py cookie
guard + validate_runtime_safety), rather than merely asserting on rendered text. Both the MC
and the customer test drive the SAME resolution path through here so neither box can drift.

Not collected by pytest (no ``test_`` prefix); imported as ``tests.boot_config_helper``.
"""

from __future__ import annotations

import base64
import gzip
import io
import re
import tarfile
from typing import Dict, List, Tuple

from app.config import Settings

_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def parse_dotenv(text: str) -> Dict[str, str]:
    """Parse ``KEY=value`` lines (blank/``#`` lines skipped) into a dict. Values are taken
    verbatim (the box dotenv is unquoted; compose interpolates it as-is)."""
    out: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value
    return out


def extract_cloud_init_file(cloud_init: str, path: str) -> str:
    """Return the body of the cloud-init ``write_files`` entry for ``path``.

    Mirrors ``render._write_file_entry`` / ``_yaml_block``: the entry is
    ``  - path: <path>`` then ``    content: |`` then the body indented by six spaces
    (blank body lines rendered as truly empty). Collection stops at the next entry
    (``  - path:`` at two-space indent) or any other dedent."""
    marker = f"  - path: {path}\n"
    if marker not in cloud_init:
        # Full customer stacks place non-secret assets in the deterministic
        # gz+b64 tar to stay below Hetzner's cloud-init size limit. box.env and
        # a baked MC .env remain direct 0600 entries, so this is a fallback only.
        archive_match = re.search(
            r"  - path: /opt/onebrain/onebrain-assets\.tar\n"
            r"    permissions: '[0-7]+'\n"
            r"    encoding: gz\+b64\n"
            r"    content: (?P<blob>\S+)\n",
            cloud_init,
        )
        if not archive_match:
            raise ValueError(f"cloud-init file not found: {path}")
        archive = gzip.decompress(base64.b64decode(archive_match.group("blob")))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            handle = tar.extractfile(path.lstrip("/"))
            if handle is None:
                raise ValueError(f"cloud-init asset not found: {path}")
            return handle.read().decode("utf-8")
    start = cloud_init.index(marker)
    lines = cloud_init[start + len(marker):].split("\n")
    body: List[str] = []
    in_body = False
    for line in lines:
        if not in_body:
            if line.strip() == "content: |":
                in_body = True
            continue
        if line.startswith("      "):
            body.append(line[6:])
        elif line == "":
            body.append("")            # a blank body line (rendered without indent)
        else:
            break                      # next write_files entry / dedent -> body ends
    return "\n".join(body)


def _api_pairs(api_env_text: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for line in api_env_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs.append((key.strip(), value))
    return pairs


def resolve_box_api_settings(api_env_text: str, dotenv_text: str) -> Settings:
    """Simulate docker-compose env interpolation and rebuild onebrain-api's Settings.

    ``api_env_text``  the rendered ``env/onebrain-api.env`` body (process env, with ``${VAR}``
                      secret refs).
    ``dotenv_text``   the box's resolved ``/opt/onebrain/.env`` (bundle dotenv + any operator
                      overlay) that compose interpolates the refs from.

    Every ``${VAR}`` occurrence in an api value is replaced with its ``.env`` value (empty if
    absent, matching compose), then ONEBRAIN_-prefixed keys are mapped to Settings fields and
    a real ``Settings`` is constructed — exactly what onebrain-api sees at boot."""
    env = parse_dotenv(dotenv_text)
    resolved: Dict[str, str] = {}
    for key, value in _api_pairs(api_env_text):
        resolved[key] = _REF.sub(lambda m: env.get(m.group(1), ""), value)

    fields = set(Settings.model_fields)
    kwargs: Dict[str, str] = {}
    for key, value in resolved.items():
        if not key.startswith("ONEBRAIN_"):
            continue
        field = key[len("ONEBRAIN_"):].lower()
        if field in fields:            # drop non-Settings env (e.g. ONEBRAIN_API_BASE_URL, the read-only surface flag)
            kwargs[field] = value
    return Settings(**kwargs)
