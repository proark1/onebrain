"""Fail if an approved direct dependency is absent from a hash-locked output.

The production and development lock files are generated as universal Python 3.12
locks, so the supported Windows development environment and Linux production
images resolve the same approved versions:

    uv pip compile requirements.in --python-version 3.12 --universal \
      --generate-hashes \
      --no-emit-index-url --resolution highest --output-file requirements.txt
    uv pip compile requirements-dev.in --python-version 3.12 --universal \
      --generate-hashes \
      --no-emit-index-url --resolution highest --output-file requirements-dev.txt

Run this script after regenerating.  It deliberately uses only the standard
library so CI can run it immediately after a hash-verified install.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_PAIRS = (
    (ROOT / "requirements.in", ROOT / "requirements.txt"),
    (ROOT / "requirements-dev.in", ROOT / "requirements-dev.txt"),
)
_PIN = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^\s\\]+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _direct_pins(path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    seen = seen or set()
    resolved = path.resolve()
    if resolved in seen:
        raise ValueError(f"recursive requirements include: {path}")
    seen.add(resolved)

    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            pins.update(_direct_pins(path.parent / line[3:].strip(), seen))
            continue
        match = _PIN.match(line)
        if not match:
            raise ValueError(f"{path.name}: direct dependencies must use exact == pins: {line}")
        pins[_normalize(match.group(1))] = match.group(2)
    return pins


def _locked_pins(path: Path) -> tuple[dict[str, str], set[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    pins: dict[str, str] = {}
    hashed: set[str] = set()
    starts: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        match = _PIN.match(line)
        if match:
            starts.append((index, match))

    for position, (start, match) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        name = _normalize(match.group(1))
        pins[name] = match.group(2)
        if any("--hash=sha256:" in line for line in lines[start:end]):
            hashed.add(name)
    return pins, hashed


def main() -> int:
    errors: list[str] = []
    for source, lock in LOCK_PAIRS:
        direct = _direct_pins(source)
        locked, hashed = _locked_pins(lock)
        for name, version in sorted(direct.items()):
            actual = locked.get(name)
            if actual != version:
                errors.append(
                    f"{lock.name}: {name} is {actual or 'missing'}, expected {version} from {source.name}"
                )
            elif name not in hashed:
                errors.append(f"{lock.name}: {name} has no SHA-256 artifact hash")

        unhashed = sorted(set(locked) - hashed)
        if unhashed:
            errors.append(f"{lock.name}: unhashed locked packages: {', '.join(unhashed)}")

    if errors:
        print("requirements lock verification failed:", file=sys.stderr)
        print("\n".join(f"- {error}" for error in errors), file=sys.stderr)
        return 1
    print("requirements locks are exact and hash-complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
