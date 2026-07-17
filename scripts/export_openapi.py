"""Export the FastAPI OpenAPI schema for generated clients."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _schema_json() -> str:
    # Schema generation must never inherit a developer's production-like .env:
    # importing app.main would otherwise probe a live embedding provider or
    # database before writing a static contract.  Force a deterministic, fully
    # local configuration while retaining the complete operator API surface.
    os.environ.update({
        "ONEBRAIN_AUTH_SECRET": "openapi-export-secret-openapi-export-secret",
        "ONEBRAIN_COOKIE_SECURE": "false",
        "ONEBRAIN_SEED_SAMPLE_DATA": "false",
        "ONEBRAIN_SEED_DEMO_USERS": "false",
        "ONEBRAIN_ENVIRONMENT": "local",
        "ONEBRAIN_VECTOR_STORE": "memory",
        "ONEBRAIN_EMBEDDINGS_PROVIDER": "local",
        "ONEBRAIN_LLM_PROVIDER": "local",
        "ONEBRAIN_OPERATOR_MODE": "true",
        "ONEBRAIN_OPERATOR_CONSOLE": "false",
        "ONEBRAIN_FLEET_REPORTER_ENABLED": "false",
        "ONEBRAIN_PROVISIONER_BACKEND": "disabled",
        "ONEBRAIN_LEGACY_STATIC_UI_ENABLED": "false",
    })

    from app.main import app

    schema = app.openapi()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check = "--check" in args
    if check:
        args.remove("--check")
    if len(args) > 1:
        raise SystemExit("usage: export_openapi.py [TARGET] [--check]")
    if check and not args:
        raise SystemExit("--check requires a TARGET path")

    output = _schema_json()
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
