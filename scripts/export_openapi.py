"""Export the FastAPI OpenAPI schema for generated clients."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ.setdefault("ONEBRAIN_AUTH_SECRET", "openapi-export-secret-openapi-export-secret")
    os.environ.setdefault("ONEBRAIN_COOKIE_SECURE", "false")
    os.environ.setdefault("ONEBRAIN_SEED_SAMPLE_DATA", "false")
    os.environ.setdefault("ONEBRAIN_SEED_DEMO_USERS", "false")

    from app.main import app

    schema = app.openapi()
    if len(sys.argv) > 1:
        target = Path(sys.argv[1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        json.dump(schema, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
