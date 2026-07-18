"""Export the FastAPI OpenAPI schema for generated clients."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


OPENAPI_SURFACES = frozenset({"operator", "customer"})


def _schema_json(surface: str = "operator") -> str:
    if surface not in OPENAPI_SURFACES:
        raise ValueError(f"Unknown OpenAPI surface: {surface}")
    # Schema generation must never inherit a developer's production-like .env:
    # importing app.main would otherwise probe a live embedding provider or
    # database before writing a static contract. Force a deterministic, fully
    # local configuration, then select one real deployment surface explicitly.
    export_environment = {
        "ONEBRAIN_AUTH_SECRET": "openapi-export-secret-openapi-export-secret",
        "ONEBRAIN_COOKIE_SECURE": "false",
        "ONEBRAIN_SEED_SAMPLE_DATA": "false",
        "ONEBRAIN_SEED_DEMO_USERS": "false",
        "ONEBRAIN_ENVIRONMENT": "local",
        "ONEBRAIN_VECTOR_STORE": "memory",
        "ONEBRAIN_EMBEDDINGS_PROVIDER": "local",
        "ONEBRAIN_LLM_PROVIDER": "local",
        "ONEBRAIN_OPERATOR_MODE": "true" if surface == "operator" else "false",
        "ONEBRAIN_OPERATOR_CONSOLE": "false",
        "ONEBRAIN_FLEET_REPORTER_ENABLED": "false",
        "ONEBRAIN_PROVISIONER_BACKEND": "disabled",
        "ONEBRAIN_LEGACY_STATIC_UI_ENABLED": "false",
    }
    previous_environment = {
        name: os.environ.get(name) for name in export_environment
    }

    from app.config import get_settings

    try:
        os.environ.update(export_environment)
        # Tests and tooling may export both surfaces in one interpreter. Rebuild
        # settings and application assembly so route inclusion never leaks from
        # the first export into the second.
        get_settings.cache_clear()
        from app.main import create_app

        schema = create_app().openapi()
        return json.dumps(schema, indent=2, sort_keys=True) + "\n"
    finally:
        for name, previous in previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
        # Do not let the temporary exporter configuration escape into callers
        # that share this interpreter (notably the full pytest suite).
        get_settings.cache_clear()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    if check:
        args.remove("--check")
    surface = "operator"
    if "--surface" in args:
        index = args.index("--surface")
        if index + 1 >= len(args):
            raise SystemExit("--surface requires operator or customer")
        surface = args[index + 1]
        del args[index:index + 2]
    if surface not in OPENAPI_SURFACES:
        raise SystemExit("--surface must be operator or customer")
    if len(args) > 1:
        raise SystemExit(
            "usage: export_openapi.py [TARGET] [--check] "
            "[--surface operator|customer]"
        )
    if check and not args:
        raise SystemExit("--check requires a TARGET path")

    output = _schema_json(surface)
    if args:
        target = Path(args[0])
        if check:
            current = target.read_text(encoding="utf-8") if target.is_file() else ""
            if current != output:
                print(f"OpenAPI schema is stale: {target}", file=sys.stderr)
                return 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
